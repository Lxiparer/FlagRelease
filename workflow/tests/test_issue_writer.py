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

"""Issue 出口：确定性故障事实 → logs/issues_*.log（+ 经既有 issue_reporter 生成 markdown）

引擎接管后，启动崩溃/精度异常/性能异常在引擎模式下原本**没有任何出口**
（legacy 至少还能靠会话写），这里锁住"必须留痕"这条。
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.domain.issue_writer import write_issue
from workflow.engine.command_executor import FakeExecutor
from workflow.engine.workflow_engine import WorkflowEngine, WORKFLOW_STEPS

from workflow.tests.test_engine_e2e import (
    make_fake, script_service_tools, script_task_blocks, write_eval_result,
)


class TestIssueWriter(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read(self, name):
        return (Path(self.tmpdir) / "logs" / name).read_text(encoding="utf-8")

    def test_appends_in_documented_format(self):
        """日志格式对齐 CLAUDE.md（[时间] 版本 | 摘要 + 详情/操作/结果）"""
        write_issue(self.tmpdir, "startup", "服务起不来",
                    detail="crash_log=/logs/service.log", action="禁用算子重试", result="未收敛")
        text = self._read("issues_startup.log")
        self.assertIn("| 服务起不来", text)
        for field in ("详情:", "操作:", "结果:"):
            self.assertIn(field, text)

    def test_each_category_has_its_own_file(self):
        write_issue(self.tmpdir, "accuracy", "精度退化")
        write_issue(self.tmpdir, "performance", "V4 无提升")
        write_issue(self.tmpdir, "analysis", "未解决")
        for name in ("issues_accuracy.log", "issues_performance.log", "issues_analysis.log"):
            self.assertTrue((Path(self.tmpdir) / "logs" / name).exists(), name)

    def test_unknown_category_rejected(self):
        with self.assertRaises(ValueError):
            write_issue(self.tmpdir, "bogus", "x")

    def test_markdown_goes_through_issue_reporter(self):
        """约束4：issue 只能经既有 issue_reporter.py 生成（不手工拼 gh issue create）"""
        fake = FakeExecutor()
        fake.when("issue_reporter", returncode=0, stdout="issue 已保存: results/issue_x.md")
        out = write_issue(self.tmpdir, "startup", "服务崩溃",
                          executor=fake, container="ctr", model_name="M")
        self.assertTrue(out["reporter_ok"])
        calls = fake.calls_containing("issue_reporter")
        self.assertEqual(len(calls), 1)
        self.assertIn("--type operator-crash", " ".join(calls[0]))

    def test_reporter_failure_does_not_break(self):
        """issue_reporter 失败不能影响主流程（只告警）"""
        fake = FakeExecutor()
        fake.when("issue_reporter", returncode=1, stderr="boom")
        out = write_issue(self.tmpdir, "accuracy", "精度异常",
                          executor=fake, container="ctr")
        self.assertFalse(out["reporter_ok"])
        self.assertTrue(Path(out["log_file"]).exists())  # 日志仍然落盘


class TestEngineIssueExits(unittest.TestCase):
    """引擎在关键故障点必须留下 issue（引擎模式下唯一的对外故障出口）"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        for sub in ("shared", "results", "logs"):
            (Path(self.tmpdir) / sub).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _issue_text(self, name):
        p = Path(self.tmpdir) / "logs" / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_startup_failure_writes_startup_issue(self):
        """启动不可恢复 → issues_startup.log（并降级）"""
        fake = make_fake()
        script_task_blocks(fake, ["fail"])          # 服务起不来
        fake.when("diagnose_ops", returncode=1, stdout=json.dumps(
            {"crashed_ops": [], "candidate_ops": [], "evidence": []}))
        eng = WorkflowEngine(self.tmpdir, executor=fake)
        eng.context.runtime.container_name = "ctr"
        eng.context.runtime.model_name = "M"
        eng.context.runtime.model_path = "/models/M"
        eng.long_task_poll_interval = 0
        # 发现阶段（步骤03）已在上一轮成功，本轮从启动调优开始
        for sid, _ in WORKFLOW_STEPS[:4]:
            eng.context.steps[sid].status = "success"
        eng.create_operator_revision("v3-discovered", parent_revision_id=None,
                                     enabled_ops=["op_a", "op_b"])
        eng.context.current_step_id = "05_v3_startup_tuning"
        eng._save_context()
        eng.execute_step("05_v3_startup_tuning")

        self.assertIn("启动未收敛", self._issue_text("issues_startup.log"))

    def test_accuracy_failure_writes_accuracy_issue(self):
        """精度不达标 → issues_accuracy.log"""
        fake = make_fake(accuracy_exit=1)
        eng = WorkflowEngine(self.tmpdir, executor=fake)
        eng.context.runtime.container_name = "ctr"
        eng.context.runtime.model_name = "M"
        eng.context.runtime.model_path = "/models/M"
        eng.long_task_poll_interval = 0
        write_eval_result(self.tmpdir)
        eng.run()

        self.assertIn("精度未达标", self._issue_text("issues_accuracy.log"))

    def test_v4_fallback_writes_performance_issue(self):
        """V4 无合法提升 → issues_performance.log"""
        fake = make_fake()   # benchmark 恒定吞吐 → V4 无提升
        eng = WorkflowEngine(self.tmpdir, executor=fake)
        eng.context.runtime.container_name = "ctr"
        eng.context.runtime.model_name = "M"
        eng.context.runtime.model_path = "/models/M"
        eng.long_task_poll_interval = 0
        write_eval_result(self.tmpdir)
        eng.run()

        self.assertIn("V4 搜索无合法提升", self._issue_text("issues_performance.log"))

    def test_no_issue_on_clean_run(self):
        """全流程顺利 → 不应产生任何 issue（防噪声）"""
        fake = make_fake()
        eng = WorkflowEngine(self.tmpdir, executor=fake)
        eng.context.runtime.container_name = "ctr"
        eng.context.runtime.model_name = "M"
        eng.context.runtime.model_path = "/models/M"
        eng.long_task_poll_interval = 0
        write_eval_result(self.tmpdir)
        eng.run()

        self.assertEqual(self._issue_text("issues_startup.log"), "")
        self.assertEqual(self._issue_text("issues_accuracy.log"), "")


if __name__ == "__main__":
    unittest.main()
