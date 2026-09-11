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

"""V3 精度算子调优竖片（步骤07，M1b）

覆盖：精度已达标 → 跳过；不达标 → 工具生成累积禁用候选组 → 重启 + 核验 + 重评 →
达标即提交；预算耗尽 → 流程继续但精度 gate 保持 failed；重启失败 → 记证据换下一组。
候选由 diagnose_ops.py accuracy-groups 生成（引擎不手拼搜索循环）。
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
from workflow.schemas.context_v2 import Gate

from workflow.tests.test_engine_e2e import (
    script_eval_task, script_service_tools, write_eval_result,
)

OPS = ["op_a", "op_b", "op_c", "op_d"]


def _compare(exit_code: int) -> ExecResult:
    return ExecResult(exit_code, json.dumps({
        "nv": {"score": 66.8}, "rel_drop": 0.02, "aligned": exit_code == 0,
    }))


def _groups(*specs) -> str:
    """构造 accuracy-groups 输出。specs: (组名, 累积禁用算子)"""
    return json.dumps({
        "groups": [
            {"name": name, "ops": list(ops), "cumulative_disabled_ops": list(ops),
             "cumulative_disabled_count": len(ops)}
            for name, ops in specs
        ],
    })


def _whitelist_env() -> str:
    return json.dumps({"success": True, "env_inline": "USE_FLAGGEMS=1"})


class AccuracyTuningBase(unittest.TestCase):
    """预置 01-06 已成功 + v3-startup-stable 冻结 + 精度 gate failed。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _engine(self, fake: FakeExecutor, gate_status: str = "failed") -> WorkflowEngine:
        eng = WorkflowEngine(self.tmpdir, executor=fake)
        eng.context.runtime.container_name = "test_ctr"
        eng.context.runtime.model_name = "TestModel"
        eng.context.runtime.model_path = "/models/TestModel"
        eng.startup_tuning_timeout = 0
        eng.startup_tuning_poll_interval = 0
        eng.long_task_poll_interval = 0
        write_eval_result(self.tmpdir)  # 评测结果文件（完整性校验会读）
        for sid, _ in WORKFLOW_STEPS[:6]:
            eng.context.steps[sid].status = "success"
        eng.create_operator_revision(
            "v3-startup-stable", parent_revision_id=None, enabled_ops=list(OPS),
        )
        eng.freeze_revision("v3-startup-stable")
        eng.context.gates["accuracy.v3.qualified"] = Gate(
            gate_id="accuracy.v3.qualified", status=gate_status,
        )
        eng.context.current_step_id = "07_v3_accuracy_tuning"
        eng._save_context()
        return eng


class TestAccuracyTuning(AccuracyTuningBase):
    def test_skipped_when_already_qualified(self):
        """精度已达标 → 步骤07 跳过，不做任何评测"""
        fake = FakeExecutor()
        engine = self._engine(fake, gate_status="passed")
        result = engine.execute_step("07_v3_accuracy_tuning")

        self.assertEqual(result.status, "skipped")
        self.assertIn("已达标", result.skip_reason)
        self.assertFalse(fake.calls_containing("accuracy_compare"))

    def test_qualifies_on_first_group(self):
        """不达标 → 第1组候选达标 → 提交该 revision、gate 置 passed"""
        fake = FakeExecutor()
        script_service_tools(fake)
        script_eval_task(fake)
        fake.when("fast_gpqa", returncode=0, stdout=json.dumps({"score": 65.5}))
        # 基线评测不达标，第1组候选达标
        fake.when_sequence("accuracy_compare", [_compare(1), _compare(0)])
        fake.when("apply_op_config", returncode=0, stdout=_whitelist_env())
        fake.when("accuracy-groups", returncode=0, stdout=_groups(("other", ["op_d"])))
        engine = self._engine(fake)
        result = engine.execute_step("07_v3_accuracy_tuning")

        ctx = engine.context
        self.assertEqual(result.status, "success")
        self.assertEqual(ctx.gates["accuracy.v3.qualified"].status, "passed")
        # 达标 revision 成为当前 revision，且已剔除 op_d
        self.assertEqual(ctx.current_revision_id, "v3-accuracy-r1")
        tuned = ctx.operator_revisions["v3-accuracy-r1"]
        self.assertEqual(set(tuned.enabled_ops), {"op_a", "op_b", "op_c"})
        self.assertIn("op_d", tuned.disabled_ops)
        self.assertIn("accuracy", tuned.disabled_ops["op_d"])  # 归入 accuracy 类别
        self.assertEqual(tuned.disable_reason_categories["accuracy"], ["op_d"])
        # 候选生成走工具，重启走启动调优机制
        self.assertTrue(fake.calls_containing("accuracy-groups"))
        self.assertTrue(fake.calls_containing("apply_op_config"))

        report = json.loads(
            (Path(self.tmpdir) / "results" / "accuracy-tuning-result.json").read_text()
        )
        self.assertEqual(report["reason"], "qualified")
        self.assertEqual(report["attempts"][0]["group"], "baseline")
        self.assertFalse(report["attempts"][0]["qualified"])
        self.assertTrue(report["attempts"][-1]["qualified"])

    def test_budget_exhausted_keeps_gate_failed(self):
        """所有候选组都不达标 → 步骤仍 success（流程继续），精度 gate 保持 failed"""
        fake = FakeExecutor()
        script_service_tools(fake)
        script_eval_task(fake)
        fake.when("fast_gpqa", returncode=0, stdout=json.dumps({"score": 65.5}))
        fake.when("accuracy_compare", returncode=1)  # 全程不达标
        fake.when("apply_op_config", returncode=0, stdout=_whitelist_env())
        fake.when("accuracy-groups", returncode=0, stdout=_groups(
            ("g1", ["op_d"]), ("g2", ["op_d", "op_c"]),
        ))
        engine = self._engine(fake)
        result = engine.execute_step("07_v3_accuracy_tuning")

        ctx = engine.context
        self.assertEqual(result.status, "success")
        self.assertEqual(ctx.gates["accuracy.v3.qualified"].status, "failed")
        self.assertEqual(ctx.current_revision_id, "v3-startup-stable")
        report = json.loads(
            (Path(self.tmpdir) / "results" / "accuracy-tuning-result.json").read_text()
        )
        self.assertFalse(report["success"])
        self.assertEqual(report["reason"], "not_converged_agent_unavailable")
        self.assertEqual(report["rounds"], 2)  # 两组都试过

    def test_restart_failure_recorded_and_continues(self):
        """重启失败 → 记入证据并试下一组，不中断调优"""
        fake = FakeExecutor()
        script_service_tools(fake)
        script_eval_task(fake)
        fake.when("fast_gpqa", returncode=0, stdout=json.dumps({"score": 65.5}))
        fake.when_sequence("accuracy_compare", [_compare(1), _compare(0)])
        # 第1组白名单下发失败（重启不成功），第2组恢复
        fake.when_sequence("apply_op_config", [
            ExecResult(2, "", "bad whitelist"), ExecResult(0, _whitelist_env()),
        ])
        fake.when("accuracy-groups", returncode=0, stdout=_groups(
            ("g1", ["op_d"]), ("g2", ["op_d", "op_c"]),
        ))
        engine = self._engine(fake)
        result = engine.execute_step("07_v3_accuracy_tuning")

        self.assertEqual(result.status, "success")
        self.assertEqual(engine.context.gates["accuracy.v3.qualified"].status, "passed")
        report = json.loads(
            (Path(self.tmpdir) / "results" / "accuracy-tuning-result.json").read_text()
        )
        self.assertFalse(report["attempts"][1]["restart_ok"])
        self.assertTrue(report["attempts"][-1]["qualified"])

    def test_runtime_oplist_verification_narrows_enabled_set(self):
        """约束27：运行时 txt 是唯一权威来源——核验后以运行时生效集为准"""
        fake = FakeExecutor()
        script_service_tools(fake)
        script_eval_task(fake)
        fake.when("fast_gpqa", returncode=0, stdout=json.dumps({"score": 65.5}))
        fake.when_sequence("accuracy_compare", [_compare(1), _compare(0)])
        fake.when("apply_op_config", returncode=0, stdout=_whitelist_env())
        fake.when("accuracy-groups", returncode=0, stdout=_groups(("other", ["op_d"])))
        # 请求 {op_a,op_b,op_c}，运行时实际只生效 {op_a,op_b}
        fake.when("flaggems_enable_oplist", stdout="op_a\nop_b")
        engine = self._engine(fake)
        engine.execute_step("07_v3_accuracy_tuning")

        ctx = engine.context
        verified = ctx.operator_revisions.get("v3-accuracy-r1-verified")
        self.assertIsNotNone(verified, "运行时核验不一致时应派生 verified revision")
        self.assertEqual(set(verified.enabled_ops), {"op_a", "op_b"})
        self.assertEqual(ctx.current_revision_id, "v3-accuracy-r1-verified")

    def test_all_ops_disabled_candidate_is_skipped(self):
        """候选组会关掉全部算子 → 跳过该组（不能等价全关 FlagGems）"""
        fake = FakeExecutor()
        script_service_tools(fake)
        script_eval_task(fake)
        fake.when("fast_gpqa", returncode=0, stdout=json.dumps({"score": 65.5}))
        fake.when("accuracy_compare", returncode=1)
        fake.when("apply_op_config", returncode=0, stdout=_whitelist_env())
        fake.when("accuracy-groups", returncode=0, stdout=_groups(("all", list(OPS))))
        engine = self._engine(fake)
        engine.execute_step("07_v3_accuracy_tuning")

        # 该组被跳过 → 没有派生任何候选 revision
        self.assertNotIn("v3-accuracy-r1", engine.context.operator_revisions)
        self.assertEqual(engine.context.steps["07_v3_accuracy_tuning"].status, "success")


if __name__ == "__main__":
    unittest.main()
