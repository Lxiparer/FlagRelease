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

"""V4 Release - V4 发布管理

职责：
1. 评估 V4 established Gate
2. 发布 V4（-v4 tag）
3. 处理回退场景（V4 不成立时不产出独立 V4，报告中说明回退到 V3）
"""

import json
import logging
import os
from typing import Dict, Optional, Tuple

from ..schemas.context_v2 import OperatorRevision, Gate
from ..artifacts.registry import ArtifactRegistry
from ..gates.reducer import GateReducer


class V4ReleaseManager:
    """V4 发布管理器"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        model_name: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
        gate_reducer: Optional[GateReducer] = None,
    ):
        self.workspace_root = workspace_root
        self.container_name = container_name
        self.model_name = model_name
        self.artifact_registry = artifact_registry or ArtifactRegistry(workspace_root)
        self.gate_reducer = gate_reducer or GateReducer(self.artifact_registry)
        self.logger = logging.getLogger("workflow.domain.v4_release")

    def release_v4(
        self,
        v4_final: Optional[OperatorRevision],
        optimization_report: Dict,
    ) -> Tuple[bool, Dict]:
        """发布 V4 版本（或处理回退）

        Args:
            v4_final: v4-final revision（None 表示 V4 不成立）
            optimization_report: V4 优化报告

        Returns:
            (是否成功, 发布信息)
        """
        if v4_final is None:
            # V4 不成立 - 回退到 V3
            self.logger.info("V4 not established, fallback to V3")
            return self._handle_v4_fallback(optimization_report)

        # V4 成立 - 评估 Gate 并发布
        self.logger.info(f"V4 established (revision={v4_final.revision_id})")

        # 评估 V4 established Gate
        v4_gate = self.gate_reducer.evaluate_v4_established_gate()
        self.logger.info(
            f"V4 Established Gate: passed={v4_gate.passed}, "
            f"reason={v4_gate.reason}"
        )

        if not v4_gate.passed:
            self.logger.warning(f"V4 Gate failed: {v4_gate.reason}")
            return False, {
                "success": False,
                "error": "v4_gate_failed",
                "reason": v4_gate.reason,
            }

        # 打包镜像（-v4 tag）
        image_success, image_tag = self._package_image(v4_final)

        if not image_success:
            self.logger.error("Image packaging failed")
            return False, {"error": "image_packaging_failed"}

        # 上传镜像（plugin 镜像模式，发布到 flagrelease-project）
        upload_success = self._upload_image(image_tag)

        if not upload_success:
            self.logger.error("Image upload failed")
            return False, {"error": "image_upload_failed"}

        # 生成发布报告
        report = self._generate_release_report(
            v4_final,
            v4_gate,
            optimization_report,
            image_tag,
        )

        # 保存发布记录
        self._save_release_record(report)

        return True, report

    def _handle_v4_fallback(self, optimization_report: Dict) -> Tuple[bool, Dict]:
        """处理 V4 回退场景

        Args:
            optimization_report: V4 优化报告

        Returns:
            (是否成功, 回退报告)
        """
        fallback_report = {
            "version": "v4",
            "success": False,
            "fallback_to_v3": True,
            "reason": optimization_report.get("reason", "unknown"),
            "optimization_attempted": True,
            "phase1_candidates": optimization_report.get("phase1_candidates", 0),
            "phase2_tested": optimization_report.get("phase2_tested", 0),
            "message": (
                "V4 optimization did not yield qualified improvements. "
                "V3 remains as the final delivery version."
            ),
        }

        # 保存回退记录
        record_file = os.path.join(
            self.workspace_root, "results", "v4_fallback_record.json"
        )

        with open(record_file, "w") as f:
            json.dump(fallback_report, f, indent=2, ensure_ascii=False)

        self.logger.info(f"V4 fallback record saved: {record_file}")

        return True, fallback_report

    def _package_image(
        self,
        revision: OperatorRevision,
    ) -> Tuple[bool, str]:
        """打包 V4 镜像

        Args:
            revision: v4-final revision

        Returns:
            (是否成功, image_tag)
        """
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d%H%M")
        # V4 发布到 flagrelease-project（plugin 镜像模式）
        image_tag = f"harbor.baai.ac.cn/flagrelease-project/{self.model_name}-flagos:{timestamp}-v4"

        self.logger.info(f"Packaging V4 image: {image_tag}")

        # 实际执行 docker commit + docker tag
        # success = self._execute_docker_commit(...)
        success = True  # 占位

        return success, image_tag

    def _upload_image(self, image_tag: str) -> bool:
        """上传 V4 镜像

        Args:
            image_tag: 镜像 tag

        Returns:
            是否成功
        """
        self.logger.info(f"Uploading V4 image: {image_tag}")

        # V4 上传到 Harbor flagrelease-project
        # success = self._execute_docker_push(image_tag)
        success = True  # 占位

        return success

    def _generate_release_report(
        self,
        revision: OperatorRevision,
        v4_gate: Gate,
        optimization_report: Dict,
        image_tag: str,
    ) -> Dict:
        """生成 V4 发布报告

        Returns:
            report dict
        """
        report = {
            "version": "v4",
            "model_name": self.model_name,
            "revision_id": revision.revision_id,
            "enabled_operators": revision.enabled_ops,
            "disabled_operators": revision.disabled_ops,
            "operator_count": len(revision.enabled_ops),
            "optimization": {
                "phase1_candidates": optimization_report.get("phase1_candidates", 0),
                "phase2_tested": optimization_report.get("phase2_tested", 0),
                "v4_throughput": optimization_report.get("v4_throughput", 0.0),
                "improvement_over_v3": True,
            },
            "gates": {
                "v4_established": {
                    "passed": v4_gate.passed,
                    "reason": v4_gate.reason,
                },
            },
            "image_tag": image_tag,
            "artifacts": {
                "image": image_tag,
                "published_to": ["harbor.baai.ac.cn/flagrelease-project"],
            },
        }

        return report

    def _save_release_record(self, report: Dict):
        """保存发布记录

        Args:
            report: 发布报告
        """
        record_file = os.path.join(
            self.workspace_root, "results", "v4_release_record.json"
        )

        with open(record_file, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        self.logger.info(f"V4 release record saved: {record_file}")
