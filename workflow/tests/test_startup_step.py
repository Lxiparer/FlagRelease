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
from workflow.domain.service_control import ServiceParams
from workflow.engine.command_executor import ExecResult, FakeExecutor

from workflow.tests.test_engine_e2e import script_eval_task, script_service_tools


class TestV3DiscoveryVertical(unittest.TestCase):
    """步骤03：起服务/等就绪**经既有工具**（start_service.sh / wait_for_service.sh）"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _startup(self, fake):
        return V3DiscoveryStartup(
            workspace_root=self.tmpdir, container_name="ctr", executor=fake,
            poll_interval=0,
        )

    def _params(self):
        return ServiceParams(
            model_path="/models/m", model_name="m", port=8000, tp_size=1,
            max_model_len=32768, thinking=False, cuda_visible_devices="1",
        )

    def _base_fake(self):
        """默认应答：起服务脚本 ok、等就绪走长任务（done）、oplist 80 个、freshness 新鲜"""
        import time as _t
        fake = FakeExecutor()
        script_service_tools(fake)           # detect_gpu / calc_tp_size
        script_eval_task(fake)               # wait_for_service.sh 走长任务协议
        fake.when("stat -c %Y", stdout=str(int(_t.time())))  # 须先于 oplist 规则
        fake.when("flaggems_enable_oplist",
                  stdout="\n".join(f"op_{i}" for i in range(80)))
        return fake

    def test_discover_success(self):
        """起服务经 start_service.sh + 等就绪经 wait_for_service.sh + oplist 80 算子"""
        fake = self._base_fake()
        ok, err, oplist = self._startup(fake).start_service_and_discover(
            model_path="/models/m", flaggems_version="", params=self._params(),
        )
        self.assertTrue(ok, err)
        self.assertIsNone(err)
        self.assertEqual(len(oplist), 80)
        # 不再内联拼 vllm 命令，而是调既有启动器
        self.assertTrue(fake.calls_containing("start_service.sh"))
        self.assertFalse(fake.calls_containing("vllm.entrypoints"),
                         "不应内联拼 vllm 命令——那会丢掉 TP/max_model_len/reasoning-parser")
        # 等就绪走既有 wait_for_service.sh（日志活动感知，不是 curl 轮询）
        self.assertTrue(fake.calls_containing("wait_for_service.sh"))
        self.assertTrue(fake.calls_containing("flaggems_enable_oplist"))

    def test_missing_params_fails_closed(self):
        """未注入 ServiceParams → fail-closed（不猜、不内联拼命令）"""
        fake = self._base_fake()
        ok, err, oplist = self._startup(fake).start_service_and_discover(
            model_path="/models/m", flaggems_version="",
        )
        self.assertFalse(ok)
        self.assertIn("ServiceParams", err)

    def test_discover_no_oplist_fails(self):
        """起服务成功但所有 oplist 文件为空 → 发现失败（无算子）"""
        import time as _t
        fake = FakeExecutor()
        script_service_tools(fake)
        script_eval_task(fake)
        fake.when("stat -c %Y", stdout=str(int(_t.time())))
        fake.when("flaggems_enable_oplist", stdout="")   # 读不到任何算子
        s = self._startup(fake)
        ok, err, oplist = s.start_service_and_discover(
            model_path="/models/m", flaggems_version="", params=self._params(),
        )
        self.assertFalse(ok)
        self.assertIsNone(oplist)
        self.assertIn("oplist", err.lower())

    def test_service_start_failure_fails(self):
        """start_service.sh 失败 → 发现失败，不进入等待"""
        fake = self._base_fake()
        fake.when("start_service.sh", returncode=1, stderr="OOM")
        ok, err, oplist = self._startup(fake).start_service_and_discover(
            model_path="/models/m", flaggems_version="", params=self._params(),
        )
        self.assertFalse(ok)
        self.assertIn("start_service", err)

    def test_service_not_ready_fails(self):
        """服务起不来（等就绪失败）→ 发现失败，且不抽 oplist"""
        fake = self._base_fake()
        # 长任务轮询序列改成"一直 running"→ 等就绪失败
        fake2 = FakeExecutor()
        script_service_tools(fake2)
        from workflow.tests.test_engine_e2e import poll_payload
        # running → 进程消失 = 静默死亡（终态，不会无限轮询）
        fake2.when_sequence("FLAGOS-TASK-LOG", [
            ExecResult(0, poll_payload()),
            ExecResult(0, poll_payload("running", pid=1)),
            ExecResult(0, poll_payload("running")),
        ])
        fake2.when("flaggems_enable_oplist", stdout="op_a")
        ok, err, oplist = self._startup(fake2).start_service_and_discover(
            model_path="/models/m", flaggems_version="", params=self._params(),
        )
        self.assertFalse(ok)
        self.assertIn("未就绪", err)
        self.assertFalse(fake2.calls_containing("flaggems_enable_oplist"), "未就绪不应抽 oplist")

    def test_stale_oplist_is_rejected(self):
        """oplist 早于本次启动 → 阻断（否则会拿上一个服务的算子集继续跑）"""
        import time as _t
        fake = FakeExecutor()
        script_service_tools(fake)
        script_eval_task(fake)
        # 先注册（规则按注册顺序匹配）：mtime 为 1 小时前 → 早于本次启动
        fake.when("stat -c %Y", stdout=str(int(_t.time()) - 3600))
        fake.when("flaggems_enable_oplist", stdout="op_a\nop_b")
        ok, err, oplist = self._startup(fake).start_service_and_discover(
            model_path="/models/m", flaggems_version="", params=self._params(),
        )
        self.assertFalse(ok)
        self.assertIn("不是本次启动产出", err)


if __name__ == "__main__":
    unittest.main()
