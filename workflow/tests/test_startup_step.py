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

"""V3 发现启动竖片：真实起服务/抽 oplist（经 executor）+ 发现算子集（M1b）"""

import unittest
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.domain.v3_startup import V3DiscoveryStartup
from workflow.engine.command_executor import FakeExecutor


class TestV3DiscoveryVertical(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _startup(self, fake):
        return V3DiscoveryStartup(
            workspace_root=self.tmpdir, container_name="ctr", executor=fake
        )

    def test_discover_success(self):
        """健康检查 ok + oplist 返回 80 算子 → 发现成功"""
        fake = FakeExecutor()  # 默认 returncode 0：rm/start/health 均 ok
        fake.when("flaggems_enable_oplist",
                  stdout="\n".join(f"op_{i}" for i in range(80)))
        s = self._startup(fake)
        ok, err, oplist = s.start_service_and_discover(model_path="/models/m", flaggems_version="")
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(len(oplist), 80)
        # 命令确实经 executor（起服务发给 vllm、抽 oplist 用 cat）
        self.assertTrue(fake.calls_containing("vllm.entrypoints"))
        self.assertTrue(fake.calls_containing("flaggems_enable_oplist"))

    def test_discover_no_oplist_fails(self):
        """健康 ok 但所有 oplist 文件为空 → 发现失败（无算子）"""
        fake = FakeExecutor()  # cat 默认返回空 stdout → 无算子
        s = self._startup(fake)
        ok, err, oplist = s.start_service_and_discover(model_path="/models/m", flaggems_version="")
        self.assertFalse(ok)
        self.assertIsNone(oplist)
        self.assertIn("oplist", err.lower())

    def test_service_start_failure_fails(self):
        """起服务命令失败 → 发现失败，不进入等待"""
        fake = FakeExecutor()
        fake.when("vllm.entrypoints", returncode=1, stderr="OOM")
        s = self._startup(fake)
        ok, err, oplist = s.start_service_and_discover(model_path="/models/m", flaggems_version="")
        self.assertFalse(ok)
        self.assertIn("start service", err.lower())

    def test_wait_ready_timeout_zero_returns_false(self):
        """timeout=0 立即返回 False，不进入 sleep 循环"""
        fake = FakeExecutor(default=None)
        # 让 health 返回非 0
        fake.when("curl", returncode=1)
        s = self._startup(fake)
        self.assertFalse(s._wait_for_service_ready(timeout=0))


if __name__ == "__main__":
    unittest.main()
