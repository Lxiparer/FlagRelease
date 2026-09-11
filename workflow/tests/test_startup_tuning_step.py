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

"""V3 启动兼容性调优竖片（步骤05，M1b）

覆盖：崩溃 → 确定性诊断（diagnose_ops.py）→ 禁用问题算子 → 重启 → 稳定；
约束18 的 crashed_ops 空 → candidate_ops 兜底；诊断穷尽且未接 Agent → 停止。
全部经 FakeExecutor，不碰容器。
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.engine.workflow_engine import WorkflowEngine, WORKFLOW_STEPS
from workflow.engine.command_executor import FakeExecutor, ExecResult

OPS = ["op_a", "op_b", "op_c", "op_d"]

# 就绪探测命令（与「停服务」的端口探测 `curl -o /dev/null -w %{http_code}` 区分开，
# 否则 when_sequence("curl", ...) 会连停服务探测一起吃掉）
HEALTH_PROBE = "curl -s http://localhost:8000/health"


def _health_fail() -> ExecResult:
    return ExecResult(returncode=7, stderr="connection refused")


def _health_ok() -> ExecResult:
    return ExecResult(returncode=0, stdout="ok")


def _whitelist_env() -> str:
    return json.dumps({
        "success": True, "mode": "custom",
        "env_inline": "USE_FLAGGEMS=1 VLLM_FL_PREFER_ENABLED=true",
    })


def _diagnosis(crashed=(), candidates=()) -> str:
    return json.dumps({
        "crashed_ops": list(crashed),
        "candidate_ops": list(candidates),
        "evidence": [],
    })


class StartupTuningBase(unittest.TestCase):
    """预置 01-04 已成功 + v3-discovered 已建立，只跑步骤05。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _engine(self, fake: FakeExecutor, ops=None) -> WorkflowEngine:
        eng = WorkflowEngine(self.tmpdir, executor=fake)
        eng.context.runtime.container_name = "test_ctr"
        eng.context.runtime.model_name = "TestModel"
        eng.context.runtime.model_path = "/models/TestModel"
        # 探测只做一次，单测不睡（真实默认 300s / 5s）
        eng.startup_tuning_timeout = 0
        eng.startup_tuning_poll_interval = 0
        for sid, _ in WORKFLOW_STEPS[:4]:
            eng.context.steps[sid].status = "success"
        eng.create_operator_revision(
            "v3-discovered", parent_revision_id=None, enabled_ops=list(ops or OPS),
        )
        eng.context.steps["04_v3_discovered"].status = "success"
        eng.context.current_step_id = "05_v3_startup_tuning"
        eng._save_context()
        return eng

    def _whitelist_ops_per_call(self, fake) -> list:
        """从 apply_op_config 的调用参数中提取每轮的白名单（逗号分隔）"""
        out = []
        for call in fake.calls_containing("apply_op_config"):
            joined = " ".join(call)
            marker = "--flagos-whitelist '"
            start = joined.find(marker)
            if start >= 0:
                rest = joined[start + len(marker):]
                out.append(rest.split("'")[0].split(","))
        return out


class TestStartupTuning(StartupTuningBase):
    def test_crash_then_diagnose_then_stable(self):
        """第1轮崩溃 → 诊断出 op_b → 禁用重建 → 第2轮就绪 → 冻结 v3-startup-stable"""
        fake = FakeExecutor()
        fake.when_sequence(HEALTH_PROBE, [_health_fail(), _health_ok()])
        fake.when("apply_op_config", returncode=0, stdout=_whitelist_env())
        fake.when("diagnose_ops", returncode=0, stdout=_diagnosis(crashed=["op_b"]))
        engine = self._engine(fake)
        result = engine.execute_step("05_v3_startup_tuning")

        ctx = engine.context
        self.assertEqual(result.status, "success")
        self.assertEqual(ctx.steps["05_v3_startup_tuning"].status, "success")
        # 启动稳定集合已冻结且不含 op_b
        stable = ctx.operator_revisions["v3-startup-stable"]
        self.assertTrue(stable.frozen)
        self.assertEqual(set(stable.enabled_ops), {"op_a", "op_c", "op_d"})
        self.assertIn("op_b", stable.disabled_ops)
        self.assertEqual(ctx.current_revision_id, "v3-startup-stable")
        # 调优过程中的 child revision 由引擎写入 context（唯一写入者）
        self.assertIn("v3-discovered-r1", ctx.operator_revisions)
        # 每轮都重新下发白名单：第1轮全量、第2轮已剔除 op_b
        whitelists = self._whitelist_ops_per_call(fake)
        self.assertEqual(whitelists[0], OPS)
        self.assertEqual(whitelists[1], ["op_a", "op_c", "op_d"])
        # 清缓存按约束25每轮都做
        self.assertGreaterEqual(len(fake.calls_containing("rm -rf /root/.triton/cache/")), 2)

        report = json.loads(
            (Path(self.tmpdir) / "results" / "startup-tuning-result.json").read_text()
        )
        self.assertEqual(report["reason"], "stable")
        self.assertEqual(report["rounds"], 2)
        self.assertEqual(report["disabled_by_round"], {"1": ["op_b"]})

    def test_candidate_ops_fallback_when_no_crashed_ops(self):
        """约束18：crashed_ops 空时用 candidate_ops 兜底（低置信候选不能直接判无算子）"""
        fake = FakeExecutor()
        fake.when_sequence(HEALTH_PROBE, [_health_fail(), _health_ok()])
        fake.when("apply_op_config", returncode=0, stdout=_whitelist_env())
        fake.when("diagnose_ops", returncode=1,  # crashed_ops 空 → 脚本 exit 1
                  stdout=_diagnosis(crashed=[], candidates=["op_c"]))
        engine = self._engine(fake)
        engine.execute_step("05_v3_startup_tuning")

        stable = engine.context.operator_revisions["v3-startup-stable"]
        self.assertEqual(set(stable.enabled_ops), {"op_a", "op_b", "op_d"})
        self.assertIn("op_c", stable.disabled_ops)
        self.assertEqual(engine.context.steps["05_v3_startup_tuning"].status, "success")

    def test_already_disabled_ops_are_filtered(self):
        """诊断结果里已禁用的算子不重复禁用（只保留当前启用集合内的）"""
        fake = FakeExecutor()
        fake.when_sequence(HEALTH_PROBE, [_health_fail(), _health_ok()])
        fake.when("apply_op_config", returncode=0, stdout=_whitelist_env())
        # 诊断报告含已不在启用集的 op_zz
        fake.when("diagnose_ops", returncode=0,
                  stdout=_diagnosis(crashed=["op_a"], candidates=["op_zz"]))
        engine = self._engine(fake)
        engine.execute_step("05_v3_startup_tuning")

        stable = engine.context.operator_revisions["v3-startup-stable"]
        self.assertEqual(set(stable.enabled_ops), {"op_b", "op_c", "op_d"})

    def test_diagnosis_exhausted_without_agent_stops(self):
        """诊断穷尽且未接 Agent（M3）→ 步骤失败并给出明确原因"""
        fake = FakeExecutor()
        fake.when(HEALTH_PROBE, returncode=7, stderr="connection refused")
        fake.when("apply_op_config", returncode=0, stdout=_whitelist_env())
        fake.when("diagnose_ops", returncode=1, stdout=_diagnosis())  # 无任何算子
        engine = self._engine(fake)
        result = engine.execute_step("05_v3_startup_tuning")

        self.assertEqual(result.status, "failed")
        step = engine.context.steps["05_v3_startup_tuning"]
        self.assertEqual(step.status, "failed")
        self.assertIn("diagnosis_exhausted_agent_unavailable", step.fail_reason)
        self.assertNotIn("v3-startup-stable", engine.context.operator_revisions)
        report = json.loads(
            (Path(self.tmpdir) / "results" / "startup-tuning-result.json").read_text()
        )
        self.assertEqual(report["reason"], "diagnosis_exhausted_agent_unavailable")

    def test_op_config_failure_blocks_without_starting(self):
        """算子白名单下发失败 → 不起服务、不诊断，直接记录配置错误"""
        fake = FakeExecutor()
        fake.when("apply_op_config", returncode=2, stderr="bad whitelist")
        engine = self._engine(fake)
        engine.execute_step("05_v3_startup_tuning")

        step = engine.context.steps["05_v3_startup_tuning"]
        self.assertEqual(step.status, "failed")
        report = json.loads(
            (Path(self.tmpdir) / "results" / "startup-tuning-result.json").read_text()
        )
        self.assertEqual(report["attempts"][0]["error_type"], "op_config")
        # 未发服务启动命令、未做诊断
        self.assertFalse(fake.calls_containing("diagnose_ops"))


if __name__ == "__main__":
    unittest.main()
