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

"""Gate Reducer 单元测试"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.gates.reducer import GateReducer
from workflow.artifacts.registry import ArtifactRegistry


class TestGateReducer(unittest.TestCase):
    """Gate Reducer 测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.registry = ArtifactRegistry(self.tmpdir)
        self.reducer = GateReducer(self.registry)

        # 创建必要的目录结构
        os.makedirs(os.path.join(self.tmpdir, "results"), exist_ok=True)

    def test_accuracy_gate_pass_with_nv_reference(self):
        """精度 Gate 通过（外部 NV reference 达标）"""
        # 注册 V3 精度结果 Artifact
        accuracy_result = {
            "candidate": "v3",
            "dataset": "gpqa_diamond",
            "accuracy": 0.48,
            "nv_reference": 0.50,
            "relative_drop": 0.04,  # 4% < 5%
            "qualified": True,
        }

        result_file = os.path.join(self.tmpdir, "results", "v3_gpqa_diamond.json")
        with open(result_file, "w") as f:
            json.dump(accuracy_result, f)

        self.registry.register_artifact(
            artifact_type="accuracy-result",
            content=accuracy_result,
            file_path="results/v3_gpqa_diamond.json",
            generated_by="test",
            tags={"candidate": "v3", "qualified": "True"},
        )

        # 评估 Gate
        gate = self.reducer.evaluate_accuracy_gate("v3", datasets=["gpqa_diamond"])

        self.assertTrue(gate.status == "passed")
        self.assertIn("qualified", gate.reason)

    def test_accuracy_gate_fail_missing_artifact(self):
        """精度 Gate 失败（缺失 Artifact）- fail-closed"""
        gate = self.reducer.evaluate_accuracy_gate("v3", datasets=["gpqa_diamond"])

        self.assertFalse(gate.status == "passed")
        self.assertIn("missing", gate.reason.lower())

    def test_accuracy_gate_fail_not_qualified(self):
        """精度 Gate 失败（精度不达标）"""
        # 注册不达标的精度结果
        accuracy_result = {
            "candidate": "v3",
            "dataset": "gpqa_diamond",
            "accuracy": 0.40,
            "nv_reference": 0.50,
            "relative_drop": 0.20,  # 20% > 5%
            "qualified": False,
        }

        result_file = os.path.join(self.tmpdir, "results", "v3_gpqa_diamond.json")
        with open(result_file, "w") as f:
            json.dump(accuracy_result, f)

        self.registry.register_artifact(
            artifact_type="accuracy-result",
            content=accuracy_result,
            file_path="results/v3_gpqa_diamond.json",
            generated_by="test",
            tags={"candidate": "v3", "qualified": "False"},
        )

        gate = self.reducer.evaluate_accuracy_gate("v3", datasets=["gpqa_diamond"])

        self.assertFalse(gate.status == "passed")
        self.assertIn("not qualified", gate.reason.lower())

    def test_v3_established_gate_pass(self):
        """V3 established Gate 通过"""
        # 注册精度达标 Artifact
        accuracy_result = {
            "candidate": "v3",
            "dataset": "gpqa_diamond",
            "accuracy": 0.48,
            "nv_reference": 0.50,
            "relative_drop": 0.04,
            "qualified": True,
        }

        result_file = os.path.join(self.tmpdir, "results", "v3_gpqa_diamond.json")
        with open(result_file, "w") as f:
            json.dump(accuracy_result, f)

        self.registry.register_artifact(
            artifact_type="accuracy-result",
            content=accuracy_result,
            file_path="results/v3_gpqa_diamond.json",
            generated_by="test",
            tags={"candidate": "v3", "qualified": "True"},
        )

        # 注册算子 revision Artifact（至少 1 个算子）
        revision = {
            "revision_id": "v3-final",
            "enabled_ops": ["op1", "op2", "op3"],
            "disabled_ops": [],
        }

        revision_file = os.path.join(self.tmpdir, "results", "v3_final_revision.json")
        with open(revision_file, "w") as f:
            json.dump(revision, f)

        self.registry.register_artifact(
            artifact_type="operator-revision",
            content=revision,
            file_path="results/v3_final_revision.json",
            generated_by="test",
            tags={"revision_id": "v3-final"},
        )

        # 评估 V3 established Gate
        gate = self.reducer.evaluate_v3_established_gate(v3_final_revision_id="v3-final")

        self.assertTrue(gate.status == "passed")
        self.assertIn("accuracy qualified", gate.reason)
        self.assertIn("ops >= 1", gate.reason)

    def test_v3_established_gate_fail_no_operators(self):
        """V3 established Gate 失败（无算子）"""
        # 注册精度达标但算子为空的 revision
        accuracy_result = {
            "candidate": "v3",
            "dataset": "gpqa_diamond",
            "qualified": True,
        }

        self.registry.register_artifact(
            artifact_type="accuracy-result",
            content=accuracy_result,
            file_path="results/v3_accuracy.json",
            generated_by="test",
            tags={"candidate": "v3", "qualified": "True"},
        )

        revision = {
            "revision_id": "v3-final",
            "enabled_ops": [],  # 空算子列表
            "disabled_ops": [],
        }

        self.registry.register_artifact(
            artifact_type="operator-revision",
            content=revision,
            file_path="results/v3_revision.json",
            generated_by="test",
            tags={"revision_id": "v3-final"},
        )

        gate = self.reducer.evaluate_v3_established_gate(v3_final_revision_id="v3-final")

        self.assertFalse(gate.status == "passed")
        self.assertIn("0 operators", gate.reason.lower())

    def test_v4_established_gate_pass(self):
        """V4 established Gate 通过（性能超越 V3 + 精度达标 + ≥1算子）"""
        # 注册 V3 性能 Artifact
        v3_perf = {
            "candidate": "v3",
            "throughput_tokens_per_sec": 1000.0,
        }

        self.registry.register_artifact(
            artifact_type="performance-result",
            content=v3_perf,
            file_path="results/v3_perf.json",
            generated_by="test",
            tags={"candidate": "v3"},
        )

        # 注册 V4 性能 Artifact（超越 V3）
        v4_perf = {
            "candidate": "v4",
            "throughput_tokens_per_sec": 1200.0,
        }

        self.registry.register_artifact(
            artifact_type="performance-result",
            content=v4_perf,
            file_path="results/v4_perf.json",
            generated_by="test",
            tags={"candidate": "v4"},
        )

        # 注册 V4 精度 Artifact（达标）
        v4_accuracy = {
            "candidate": "v4",
            "dataset": "gpqa_diamond",
            "qualified": True,
        }

        self.registry.register_artifact(
            artifact_type="accuracy-result",
            content=v4_accuracy,
            file_path="results/v4_accuracy.json",
            generated_by="test",
            tags={"candidate": "v4", "qualified": "True"},
        )

        # 注册 V4 revision（≥1算子）
        v4_revision = {
            "revision_id": "v4-final",
            "enabled_ops": ["op1", "op2"],
        }

        self.registry.register_artifact(
            artifact_type="operator-revision",
            content=v4_revision,
            file_path="results/v4_revision.json",
            generated_by="test",
            tags={"revision_id": "v4-final"},
        )

        gate = self.reducer.evaluate_v4_established_gate(
            v4_final_revision_id="v4-final",
            v3_final_revision_id="v3-final",
        )

        self.assertTrue(gate.status == "passed")
        self.assertIn("performance improved", gate.reason)

    def test_v4_established_gate_fail_performance_not_improved(self):
        """V4 established Gate 失败（性能未超越 V3）"""
        # V3 性能
        v3_perf = {"candidate": "v3", "throughput_tokens_per_sec": 1000.0}
        self.registry.register_artifact(
            artifact_type="performance-result",
            content=v3_perf,
            file_path="results/v3_perf.json",
            generated_by="test",
            tags={"candidate": "v3"},
        )

        # V4 性能（未超越）
        v4_perf = {"candidate": "v4", "throughput_tokens_per_sec": 950.0}
        self.registry.register_artifact(
            artifact_type="performance-result",
            content=v4_perf,
            file_path="results/v4_perf.json",
            generated_by="test",
            tags={"candidate": "v4"},
        )

        gate = self.reducer.evaluate_v4_established_gate(
            v4_final_revision_id="v4-final",
            v3_final_revision_id="v3-final",
        )

        self.assertFalse(gate.status == "passed")
        self.assertIn("not improved", gate.reason.lower())

    def test_gate_fail_closed_on_artifact_corruption(self):
        """Gate fail-closed（Artifact 损坏）"""
        # 创建损坏的 JSON 文件
        corrupt_file = os.path.join(self.tmpdir, "results", "corrupt.json")
        with open(corrupt_file, "w") as f:
            f.write("{invalid json")

        # 尝试注册会失败或返回 None
        # Gate 评估时应 fail-closed
        gate = self.reducer.evaluate_accuracy_gate("v3", datasets=["gpqa_diamond"])

        self.assertFalse(gate.status == "passed")


if __name__ == "__main__":
    unittest.main()
