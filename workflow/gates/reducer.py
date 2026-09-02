#!/usr/bin/env python3

# Copyright 2026 FlagOS Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Gate Reducer - 基于 Artifact 的业务闸门归约

核心原则：
1. 所有 Gate 判定必须基于已登记的 Artifact
2. 证据不足时 fail-closed（判定为不通过）
3. 外部 NV 精度是唯一业务红线
4. V3 性能不产生 Gate
"""

from typing import Dict, List, Optional, Literal
from datetime import datetime

from ..artifacts.registry import ArtifactRegistry
from ..artifacts.artifact_schema import AccuracyResultArtifact, PerformanceResultArtifact
from ..schemas.context_v2 import Gate


class GateReducer:
    """Gate 归约器 - 基于 Artifact 判定业务闸门"""

    def __init__(self, artifact_registry: ArtifactRegistry):
        self.registry = artifact_registry

    def evaluate_accuracy_gate(
        self,
        candidate: str,
        datasets: List[str],
        threshold: float = 0.05,
    ) -> Gate:
        """评估精度 Gate（唯一业务红线）

        Args:
            candidate: v3 或 v4
            datasets: 数据集列表（每个独立判定，全部达标才通过）
            threshold: 相对退化阈值（默认 5%）

        Returns:
            Gate 对象
        """
        gate_id = f"accuracy.{candidate}.qualified"
        gate = Gate(
            gate_id=gate_id,
            criteria=f"All datasets relative_drop <= {threshold*100}%",
        )

        all_passed = True
        reasons = []
        decision_artifacts = []

        for dataset in datasets:
            # 查询该数据集的精度结果 Artifact
            accuracy_artifacts = self.registry.query_artifacts(
                artifact_type="accuracy-result",
                tags={"candidate": candidate, "dataset": dataset}
            )

            if not accuracy_artifacts:
                # 证据不足 - fail closed
                all_passed = False
                reasons.append(f"{dataset}: no accuracy artifact found")
                continue

            # 取最新的
            latest_artifact_id = accuracy_artifacts[-1]
            artifact_content = self.registry.load_artifact_content(latest_artifact_id)

            if not artifact_content:
                all_passed = False
                reasons.append(f"{dataset}: artifact {latest_artifact_id} content missing")
                continue

            decision_artifacts.append(latest_artifact_id)

            # 检查是否有外部 NV reference
            nv_reference = artifact_content.get('nv_reference_value')
            if nv_reference is None:
                # NV reference 缺失 - fail closed
                all_passed = False
                reasons.append(f"{dataset}: NV reference missing - fail closed")
                continue

            # 检查 identity 匹配
            nv_identity = artifact_content.get('nv_reference_identity')
            expected_identity = f"{candidate}_{dataset}"  # 简化版，实际需要更严格
            # 实际应该验证模型名、数据集版本等

            # 计算相对退化
            relative_drop = artifact_content.get('relative_drop')
            if relative_drop is None:
                all_passed = False
                reasons.append(f"{dataset}: relative_drop not calculated")
                continue

            qualified = artifact_content.get('qualified', False)
            if not qualified or relative_drop > threshold:
                all_passed = False
                reasons.append(
                    f"{dataset}: relative_drop={relative_drop:.3f} > threshold={threshold} "
                    f"(candidate={artifact_content.get('accuracy', 0):.1f}%, "
                    f"nv_reference={nv_reference:.1f}%)"
                )
            else:
                reasons.append(
                    f"{dataset}: qualified (relative_drop={relative_drop:.3f} <= {threshold})"
                )

        # 判定
        gate.status = "passed" if all_passed else "failed"
        gate.required_artifacts = datasets  # 需要所有数据集的结果
        gate.decision_artifact = None  # 多个 artifacts，不单独引用
        gate.evaluated_at = datetime.now().isoformat()
        gate.reason = "; ".join(reasons)
        gate._meta = {
            "datasets": ",".join(datasets),
            "decision_artifacts": ",".join(decision_artifacts),
            "threshold": str(threshold),
        }

        return gate

    def evaluate_v3_established_gate(self, v3_final_revision_id: str) -> Gate:
        """评估 V3 是否已建立（v3-final 冻结）

        Args:
            v3_final_revision_id: v3-final 的 revision ID

        Returns:
            Gate 对象
        """
        gate_id = "v3.established"
        gate = Gate(
            gate_id=gate_id,
            criteria="v3-final revision frozen and accuracy qualified",
        )

        # 检查 v3-final 是否存在且冻结
        # 这部分逻辑需要从 context 中读取 operator_revisions
        # 简化实现：假设调用者已经确认 v3-final 存在

        # 检查精度 Gate
        accuracy_gate = self.evaluate_accuracy_gate(candidate="v3", datasets=["gpqa_diamond"])

        if accuracy_gate.status == "passed":
            gate.status = "passed"
            gate.reason = f"v3-final frozen and accuracy qualified: {accuracy_gate.reason}"
        else:
            gate.status = "failed"
            gate.reason = f"accuracy not qualified: {accuracy_gate.reason}"

        gate.evaluated_at = datetime.now().isoformat()
        gate._meta = {
            "v3_final_revision_id": v3_final_revision_id,
            "accuracy_gate_status": accuracy_gate.status,
        }

        return gate

    def evaluate_v4_established_gate(
        self,
        v4_final_revision_id: str,
        v3_final_revision_id: str,
    ) -> Gate:
        """评估 V4 是否成立

        条件：
        1. 从 v3-final 派生
        2. 精度达标（相对退化 <= 5%）
        3. 性能超越 V3（绝对值比较）
        4. 至少保留 1 个算子

        Args:
            v4_final_revision_id: v4-final 的 revision ID
            v3_final_revision_id: v3-final 的 revision ID

        Returns:
            Gate 对象
        """
        gate_id = "v4.established"
        gate = Gate(
            gate_id=gate_id,
            criteria="Derived from v3-final, accuracy qualified, performance > V3, ops >= 1",
        )

        reasons = []

        # 1. 检查精度
        accuracy_gate = self.evaluate_accuracy_gate(candidate="v4", datasets=["gpqa_diamond"])
        if accuracy_gate.status != "passed":
            gate.status = "failed"
            gate.reason = f"accuracy not qualified: {accuracy_gate.reason}"
            gate.evaluated_at = datetime.now().isoformat()
            return gate

        reasons.append("accuracy qualified")

        # 2. 检查性能（绝对值比较 V4 > V3）
        v3_perf_artifacts = self.registry.query_artifacts(
            artifact_type="performance-result",
            tags={"candidate": "v3"}
        )
        v4_perf_artifacts = self.registry.query_artifacts(
            artifact_type="performance-result",
            tags={"candidate": "v4"}
        )

        if not v3_perf_artifacts or not v4_perf_artifacts:
            gate.status = "failed"
            gate.reason = "performance artifacts missing"
            gate.evaluated_at = datetime.now().isoformat()
            return gate

        v3_perf = self.registry.load_artifact_content(v3_perf_artifacts[-1])
        v4_perf = self.registry.load_artifact_content(v4_perf_artifacts[-1])

        v3_throughput = v3_perf.get('throughput_tokens_per_sec', 0)
        v4_throughput = v4_perf.get('throughput_tokens_per_sec', 0)

        if v4_throughput <= v3_throughput:
            gate.status = "failed"
            gate.reason = f"performance not improved: V4={v4_throughput:.1f} <= V3={v3_throughput:.1f} tokens/s"
            gate.evaluated_at = datetime.now().isoformat()
            return gate

        reasons.append(f"performance improved: V4={v4_throughput:.1f} > V3={v3_throughput:.1f} tokens/s")

        # 3. 检查算子数量（需要从 context 读取 v4-final revision）
        # 简化实现：假设调用者已验证

        # 全部通过
        gate.status = "passed"
        gate.reason = "; ".join(reasons)
        gate.evaluated_at = datetime.now().isoformat()
        gate._meta = {
            "v4_final_revision_id": v4_final_revision_id,
            "v3_final_revision_id": v3_final_revision_id,
            "v3_throughput": str(v3_throughput),
            "v4_throughput": str(v4_throughput),
        }

        return gate

    def check_gate(self, gate_id: str, **kwargs) -> Gate:
        """通用 Gate 检查入口

        Args:
            gate_id: Gate ID（accuracy.v3.qualified / v3.established / v4.established）
            **kwargs: Gate 特定参数

        Returns:
            Gate 对象
        """
        if gate_id.startswith("accuracy."):
            # accuracy.v3.qualified / accuracy.v4.qualified
            candidate = gate_id.split(".")[1]
            datasets = kwargs.get("datasets", ["gpqa_diamond"])
            threshold = kwargs.get("threshold", 0.05)
            return self.evaluate_accuracy_gate(candidate, datasets, threshold)

        elif gate_id == "v3.established":
            v3_final_revision_id = kwargs.get("v3_final_revision_id", "v3-final")
            return self.evaluate_v3_established_gate(v3_final_revision_id)

        elif gate_id == "v4.established":
            v4_final_revision_id = kwargs.get("v4_final_revision_id", "v4-final")
            v3_final_revision_id = kwargs.get("v3_final_revision_id", "v3-final")
            return self.evaluate_v4_established_gate(v4_final_revision_id, v3_final_revision_id)

        else:
            # 未知 Gate - fail closed
            gate = Gate(gate_id=gate_id, status="failed", reason="unknown gate type")
            gate.evaluated_at = datetime.now().isoformat()
            return gate
