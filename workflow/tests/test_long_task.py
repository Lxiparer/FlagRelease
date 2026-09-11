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

"""LongTaskRunner：detached 启动 / state 轮询 / 存活判定 / 接管 / 放弃清理

它是"任务静默死亡"这一整类问题的唯一防线，因此单独测。
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.engine.command_executor import ExecResult, FakeExecutor, SubprocessExecutor
from workflow.engine.long_task import LongTaskRunner
from workflow.tests.test_engine_e2e import poll_payload


def _runner(polls):
    fake = FakeExecutor()
    fake.when_sequence("FLAGOS-TASK-LOG", [ExecResult(0, p) for p in polls])
    return LongTaskRunner(fake, "ctr", poll_interval=0), fake


class TestLongTaskRunner(unittest.TestCase):
    def test_success_path(self):
        runner, fake = _runner([poll_payload(), poll_payload("running", pid=1),
                                poll_payload("done", pid=1, exit_code=0)])
        r = runner.run("t", "echo hi", timeout=60)
        self.assertTrue(r.ok)
        self.assertEqual(r.exit_code, 0)
        self.assertFalse(r.adopted)
        # 启动是 detached 的（docker exec -d），不是前台阻塞
        self.assertTrue(fake.calls_containing("task_runner.py"))
        self.assertTrue(fake.calls_containing("-d"))

    def test_error_status_surfaces_exit_code_and_log(self):
        runner, _ = _runner([poll_payload(), poll_payload("error", pid=1, exit_code=3,
                                                          log="评测失败：连接被拒绝")])
        r = runner.run("t", "echo hi", timeout=60)
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "error")
        self.assertEqual(r.exit_code, 3)
        self.assertIn("连接被拒绝", r.summary())

    def test_silent_death_detected_without_terminal_state(self):
        """state 仍 running 但进程消失——前台阻塞方案发现不了的静默死亡"""
        runner, _ = _runner([poll_payload(),
                             poll_payload("running", pid=1),
                             poll_payload("running")])  # pid 空 = 进程没了
        r = runner.run("t", "echo hi", timeout=60)
        self.assertEqual(r.status, "died")
        self.assertIn("静默死亡", r.summary())

    def test_launch_grace_avoids_false_death(self):
        """detached 启动后 state/进程都可能还没就绪，宽限期内不能误判死亡"""
        runner, _ = _runner([
            poll_payload(), poll_payload(), poll_payload(), poll_payload(),  # 宽限期内三次空
            poll_payload("running", pid=1), poll_payload("done", pid=1, exit_code=0),
        ])
        r = runner.run("t", "echo hi", timeout=60)
        self.assertEqual(r.status, "done")

    def test_existing_running_task_is_adopted(self):
        """已有同任务在跑 → 接管等待，不重复启动（防双跑抢 GPU）"""
        runner, fake = _runner([poll_payload("running", pid=777),
                                poll_payload("done", pid=777, exit_code=0)])
        r = runner.run("t", "echo hi", timeout=60)
        self.assertTrue(r.ok)
        self.assertTrue(r.adopted)
        self.assertFalse(fake.calls_containing("task_runner.py"), "不应重复启动")

    def test_existing_terminal_task_is_not_rerun(self):
        """已有终态 → 直接采用，不重跑（断点续跑时省掉整轮评测）"""
        runner, fake = _runner([poll_payload("done", pid=1, exit_code=0)])
        r = runner.run("t", "echo hi", timeout=60)
        self.assertTrue(r.ok)
        self.assertFalse(fake.calls_containing("task_runner.py"))

    def test_gives_up_and_kills_container_process(self):
        """轮询超限 → 放弃并清理容器内进程，避免无主任务继续占 GPU"""
        runner, fake = _runner([poll_payload(), poll_payload("running", pid=1)])
        r = runner.run("t", "echo hi", timeout=60, max_polls=3)
        self.assertEqual(r.status, "engine_gave_up")
        self.assertTrue(fake.calls_containing("kill -TERM"), "放弃时必须清理容器内进程")

    def test_dead_process_with_terminal_state_still_reported(self):
        """进程已退出但 state 已到终态 → 正常返回终态（不是"死亡"）"""
        runner, _ = _runner([poll_payload(), poll_payload("done", exit_code=0)])
        r = runner.run("t", "echo hi", timeout=60)
        self.assertTrue(r.ok)


class TestPidMatchingRealSemantics(unittest.TestCase):
    """用**真实进程**验证存活判定——FakeExecutor 的假 pid 测不出"自匹配"这类语义错误

    背景：早前用 `<task_id>.state` 作特征，而轮询命令自身就含 `cat <state>`，
    于是 pgrep 恒命中轮询者、`alive` 恒真，静默死亡永远检不出来（真机实测确认）。
    这个测试在宿主机上起一个 argv 含 `cmd_path` 的真实进程，验证：
    1) 真正属于任务的进程能被找到；2) 执行轮询的 shell 及其父进程**不会**被算进去。
    """

    def _pids(self, cmd_path: str) -> list:
        runner = LongTaskRunner(SubprocessExecutor(), "unused", poll_interval=0)
        out = subprocess.run(["bash", "-lc", runner._ps_pids_cmd(cmd_path)],
                             capture_output=True, text=True, timeout=30)
        return [int(p) for p in out.stdout.split() if p.strip().isdigit()]

    def test_finds_real_task_and_excludes_poller(self):
        # 任务命令文件路径（真实存在的文件，模拟 `bash <cmd>` 那种 argv）
        tmp = tempfile.mkdtemp()
        cmd_path = os.path.join(tmp, "real_task.cmd")
        Path(cmd_path).write_text("sleep 30\n")

        # 起一个 argv 含 cmd_path 的真实进程（模拟 task_runner/执行体）
        task = subprocess.Popen(["bash", cmd_path])
        try:
            pids = self._pids(cmd_path)
            self.assertIn(task.pid, pids, f"应识别到真实任务进程，实际 {pids}")
            # 轮询 shell 的父进程就是本测试进程；若 $PPID 排除失效，它会误入列表
            self.assertNotIn(os.getpid(), pids, f"轮询者父进程被自匹配了：{pids}")
        finally:
            task.kill()
            task.wait(timeout=10)

    def test_dead_task_yields_no_pids(self):
        """任务已死 → 找不到任何 pid（这是"静默死亡"能被判出来的前提）"""
        tmp = tempfile.mkdtemp()
        cmd_path = os.path.join(tmp, "gone.cmd")
        Path(cmd_path).write_text("sleep 1\n")
        self.assertEqual(self._pids(cmd_path), [])


if __name__ == "__main__":
    unittest.main()
