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

"""Gate Reducer 单元测试（简化版 - 聚焦 fail-closed 行为）"""

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


class TestGateReducerFailClosed(unittest.TestCase):
    """Gate Reducer Fail-Closed 行为测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.registry = ArtifactRegistry(self.tmpdir)
        self.reducer = GateReducer(self.registry)
        os.makedirs(os.path.join(self.tmpdir, "results"), exist_ok=True)

    def test_accuracy_gate_fail_closed_no_artifacts(self):
        """精度 Gate fail-closed（无 Artifact）"""
        gate = self.reducer.evaluate_accuracy_gate("v3", datasets=["gpqa_diamond"])

        # 无 Artifact → Gate 失败
        self.assertEqual(gate.status, "failed")
        self.assertIn("no accuracy artifact", gate.reason.lower())

    def test_v3_established_gate_fail_closed_no_accuracy(self):
        """V3 established Gate fail-closed（无精度 Artifact）"""
        # 注册 revision（有算子），但无精度 Artifact
        revision = {
            "revision_id": "v3-final",
            "enabled_ops": ["op1", "op2"],
        }

        result_file = os.path.join(self.tmpdir, "results", "v3_revision.json")
        with open(result_file, "w") as f:
            json.dump(revision, f)

        self.registry.register_artifact(
            artifact_type="operator-revision",
            content=revision,
            file_path="results/v3_revision.json",
            generated_by="test",
            tags={"revision_id": "v3-final"},
        )

        gate = self.reducer.evaluate_v3_established_gate(v3_final_revision_id="v3-final")

        # 无精度 Artifact → Gate 失败
        self.assertEqual(gate.status, "failed")
        self.assertIn("accuracy not qualified", gate.reason.lower())

    def test_v4_established_gate_fail_closed_missing_artifacts(self):
        """V4 established Gate fail-closed（缺失 Artifact）"""
        gate = self.reducer.evaluate_v4_established_gate(
            v4_final_revision_id="v4-final",
            v3_final_revision_id="v3-final",
        )

        # 缺失必需 Artifact（精度或性能）→ Gate 失败
        self.assertEqual(gate.status, "failed")
        # 可能是精度或性能缺失，只要失败即可
        self.assertTrue(len(gate.reason) > 0)

    def test_gate_status_values(self):
        """验证 Gate status 的有效值"""
        gate = self.reducer.evaluate_accuracy_gate("v3", datasets=["gpqa_diamond"])

        # status 应该是 pending/passed/failed/unresolved 之一
        self.assertIn(gate.status, ["pending", "passed", "failed", "unresolved"])

    def test_gate_has_required_fields(self):
        """验证 Gate 包含必需字段"""
        gate = self.reducer.evaluate_accuracy_gate("v3", datasets=["gpqa_diamond"])

        # 必需字段
        self.assertTrue(hasattr(gate, "gate_id"))
        self.assertTrue(hasattr(gate, "status"))
        self.assertTrue(hasattr(gate, "reason"))
        self.assertTrue(hasattr(gate, "criteria"))

        # gate_id 应该有意义
        self.assertIn("accuracy", gate.gate_id)


if __name__ == "__main__":
    unittest.main()
