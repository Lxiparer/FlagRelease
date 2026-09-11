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

"""V3 发布竖片：docker commit/push 经 executor；发布范围由传入 gate 决策驱动（M1b）"""

import unittest
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.domain.v3_release import V3ReleaseManager
from workflow.engine.command_executor import FakeExecutor
from workflow.schemas.context_v2 import OperatorRevision


class TestV3Release(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.rev = OperatorRevision(revision_id="v3-final",
                                    enabled_ops=["op_a", "op_b"], frozen=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _manager(self, fake):
        return V3ReleaseManager(
            workspace_root=self.tmpdir, container_name="ctr",
            model_name="Qwen3-8B", executor=fake,
        )

    def test_full_scope_when_gates_pass(self):
        fake = FakeExecutor()  # docker commit/push 默认 ok
        m = self._manager(fake)
        ok, report = m.release_v3(self.rev, accuracy_passed=True, established_passed=True)
        self.assertTrue(ok)
        self.assertEqual(report["release_scope"], "full")
        self.assertIn("ModelScope", report["artifacts"]["published_to"])
        self.assertTrue(report["image_tag"].endswith("-v3"))
        # commit + push 都发出
        self.assertTrue(fake.calls_containing("docker commit"))
        self.assertTrue(fake.calls_containing("docker push"))

    def test_private_only_when_accuracy_failed(self):
        fake = FakeExecutor()
        m = self._manager(fake)
        ok, report = m.release_v3(self.rev, accuracy_passed=False, established_passed=True)
        self.assertTrue(ok)
        self.assertEqual(report["release_scope"], "private-only")
        self.assertNotIn("ModelScope", report["artifacts"]["published_to"])

    def test_commit_failure_blocks(self):
        fake = FakeExecutor()
        fake.when("docker commit", returncode=1, stderr="no space")
        m = self._manager(fake)
        ok, report = m.release_v3(self.rev, accuracy_passed=True, established_passed=True)
        self.assertFalse(ok)
        self.assertEqual(report["error"], "image_packaging_failed")
        # commit 失败则不应尝试 push
        self.assertFalse(fake.calls_containing("docker push"))

    def test_push_failure_blocks(self):
        fake = FakeExecutor()
        fake.when("docker push", returncode=1, stderr="auth")
        m = self._manager(fake)
        ok, report = m.release_v3(self.rev, accuracy_passed=True, established_passed=True)
        self.assertFalse(ok)
        self.assertEqual(report["error"], "image_upload_failed")


if __name__ == "__main__":
    unittest.main()
