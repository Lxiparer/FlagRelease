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

"""V3 启动和调优流程集成测试"""

import unittest
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.domain.v3_startup import V3DiscoveryStartup
from workflow.domain.v3_startup_tuning import V3StartupTuning
from workflow.schemas.context_v2 import OperatorRevision
from workflow.artifacts.registry import ArtifactRegistry


class TestV3DiscoveryStartup(unittest.TestCase):
    """V3 发现启动测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.startup = V3DiscoveryStartup(
            workspace_root=self.tmpdir,
            container_name="test_container",
        )

    def test_validate_freshness_fresh_oplist(self):
        """测试 freshness 校验 - 新鲜 oplist"""
        # Mock 最近修改的文件
        with patch('subprocess.run') as mock_run:
            import time
            mock_run.return_value = Mock(
                returncode=0,
                stdout=str(int(time.time())).encode()
            )

            ok, reason = self.startup._validate_freshness("/tmp/oplist.txt")

            self.assertTrue(ok)
            self.assertIn("fresh", reason.lower())

    def test_validate_freshness_stale_oplist(self):
        """测试 freshness 校验 - 陈旧 oplist"""
        with patch('subprocess.run') as mock_run:
            import time
            # 1小时前的文件
            mock_run.return_value = Mock(
                returncode=0,
                stdout=str(int(time.time() - 3600)).encode()
            )

            ok, reason = self.startup._validate_freshness("/tmp/oplist.txt")

            self.assertFalse(ok)
            self.assertIn("stale", reason.lower())

    def test_validate_identity_valid_count(self):
        """测试 identity 校验 - 合理算子数量"""
        operators = [f"op_{i}" for i in range(80)]  # 80 个算子

        ok, reason = self.startup._validate_identity(operators, "5.1.0")

        self.assertTrue(ok)
        self.assertIn("expected range", reason)

    def test_validate_identity_too_few(self):
        """测试 identity 校验 - 算子数量过少"""
        operators = [f"op_{i}" for i in range(30)]  # 只有 30 个

        ok, reason = self.startup._validate_identity(operators, "5.1.0")

        self.assertFalse(ok)
        self.assertIn("out of expected range", reason)

    def test_validate_identity_too_many(self):
        """测试 identity 校验 - 算子数量过多"""
        operators = [f"op_{i}" for i in range(200)]  # 200 个

        ok, reason = self.startup._validate_identity(operators, "5.1.0")

        self.assertFalse(ok)
        self.assertIn("out of expected range", reason)

    def test_create_v3_discovered_revision(self):
        """测试创建 v3-discovered revision"""
        operators = [f"op_{i}" for i in range(87)]
        artifact_id = "art-runtime-oplist-001"

        revision = self.startup.create_v3_discovered_revision(
            operators,
            artifact_id,
        )

        self.assertEqual(revision.revision_id, "v3-discovered")
        self.assertIsNone(revision.parent_revision_id)
        self.assertEqual(len(revision.enabled_ops), 87)
        self.assertEqual(len(revision.disabled_ops), 0)
        self.assertTrue(revision.verified)
        self.assertFalse(revision.frozen)
        self.assertIsNotNone(revision.source_artifact)
        self.assertEqual(revision.source_artifact.artifact_id, artifact_id)


class TestV3StartupTuning(unittest.TestCase):
    """V3 启动调优测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

        # Mock dependencies
        self.mock_agent = Mock()
        self.mock_policy_validator = Mock()
        self.mock_session_manager = Mock()

        self.tuning = V3StartupTuning(
            workspace_root=self.tmpdir,
            container_name="test_container",
            workflow_run_id="wf-test-001",
            agent=self.mock_agent,
            policy_validator=self.mock_policy_validator,
            session_manager=self.mock_session_manager,
        )

    def test_generate_child_revision_id_first_child(self):
        """测试生成第一个 child revision ID"""
        parent_id = "v3-discovered"
        child_id = self.tuning._generate_child_revision_id(parent_id)

        self.assertEqual(child_id, "v3-discovered-r1")

    def test_generate_child_revision_id_second_child(self):
        """测试生成第二个 child revision ID"""
        parent_id = "v3-startup-r1"
        child_id = self.tuning._generate_child_revision_id(parent_id)

        self.assertEqual(child_id, "v3-startup-r2")

    def test_create_child_revision_with_disabled(self):
        """测试创建禁用算子的 child revision"""
        # 先创建父 revision
        parent = OperatorRevision(
            revision_id="v3-discovered",
            enabled_ops=["op_a", "op_b", "op_c", "op_d"],
            disabled_ops={},
            disable_reason_categories={"startup": [], "accuracy": [], "v4_performance": []},
        )

        # 注册到 revision_store
        self.tuning.revision_store.revisions["v3-discovered"] = parent

        child = self.tuning._create_child_revision_with_disabled(
            parent,
            ["op_b", "op_c"],
            "startup crash: CUDA error",
        )

        self.assertEqual(child.revision_id, "v3-discovered-r1")
        self.assertEqual(child.parent_revision_id, "v3-discovered")
        self.assertEqual(set(child.enabled_ops), {"op_a", "op_d"})
        self.assertEqual(len(child.disabled_ops), 2)
        self.assertIn("op_b", child.disabled_ops)
        self.assertIn("op_c", child.disabled_ops)

    def test_deterministic_diagnosis_exhausted(self):
        """测试确定性诊断穷尽"""
        crash_info = {
            "error_type": "crash",
            "error_message": "Unknown error",
        }

        result = self.tuning._deterministic_diagnosis(crash_info)

        # 当前简化实现总是返回 None（穷尽）
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
