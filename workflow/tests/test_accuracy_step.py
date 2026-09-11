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

"""V3 精度评测：判定权在 accuracy_compare.py 退出码（M1a 核心「降权」）

外加长任务协议带来的四条可靠性行为：
- 工具错误/缺 NV 基线 → assessed=False（不能伪装成"精度退化"）
- 结果文件题数不足 → 判无效（防截断结果被当成小样本通过）
- state 仍 running 但进程消失 → 静默死亡可识别
- 启动前发现已有同任务在跑 → 接管等待，不重复启动
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.domain.v3_accuracy import V3AccuracyEvaluation
from workflow.engine.command_executor import FakeExecutor
from workflow.schemas.context_v2 import OperatorRevision

from workflow.tests.test_engine_e2e import (
    poll_payload, script_eval_task, script_service_tools, write_eval_result,
)


def _fake(compare_rc=0, compare_json=None, polls=None):
    fake = FakeExecutor()
    script_eval_task(fake, polls)  # 评测走 detached + state 轮询
    fake.when(
        "accuracy_compare",
        returncode=compare_rc,
        stdout=json.dumps(compare_json if compare_json is not None
                          else {"nv": {"score": 66.8}, "rel_drop": 0.02}),
    )
    return fake


class TestV3AccuracyJudgment(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.rev = OperatorRevision(revision_id="v3-discovered")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _evaluator(self, fake):
        # poll_interval=0：测试不真的 sleep（默认 60s 是生产值）
        return V3AccuracyEvaluation(
            workspace_root=self.tmpdir,
            container_name="ctr",
            executor=fake,
            poll_interval=0,
        )

    def test_exit0_qualified(self):
        write_eval_result(self.tmpdir, total_questions=30)
        ev = self._evaluator(_fake(compare_rc=0))
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertTrue(ok)
        r = results["gpqa_diamond"]
        self.assertTrue(r["qualified"])
        self.assertTrue(r["assessed"])
        self.assertEqual(r["exit_code"], 0)
        # artifact 键对齐（reducer 需要）
        self.assertEqual(r["nv_reference_value"], 66.8)
        self.assertEqual(r["relative_drop"], 0.02)

    def test_exit1_not_qualified_even_if_reldrop_small(self):
        """退出码=1 判不达标，即使 stdout 的 rel_drop 很小——证明判定来自退出码而非内联计算"""
        write_eval_result(self.tmpdir, total_questions=30)
        ev = self._evaluator(_fake(compare_rc=1,
                                   compare_json={"nv": {"score": 66.8}, "rel_drop": 0.01}))
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertFalse(ok)
        r = results["gpqa_diamond"]
        self.assertFalse(r["qualified"])
        self.assertEqual(r["exit_code"], 1)
        # 真实的"不达标"仍是可评估的（区别于工具错误）
        self.assertTrue(r["assessed"])

    def test_exit3_unassessed_not_a_regression(self):
        """退出码=3（缺 NV 基线）→ 无法评估，而不是"精度退化"（plan: NV 缺失 → unassessed）"""
        write_eval_result(self.tmpdir, total_questions=30)
        ev = self._evaluator(_fake(compare_rc=3, compare_json={"missing_nv": True}))
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertFalse(ok)
        r = results["gpqa_diamond"]
        self.assertFalse(r["qualified"])
        self.assertFalse(r["assessed"], "缺 NV 基线必须标为无法评估")
        self.assertIn("NV", r["unassessed_reason"])

    def test_exit2_tool_error_unassessed(self):
        """退出码=2（脚本错误）→ 同样无法评估，不能伪装成模型精度问题"""
        write_eval_result(self.tmpdir, total_questions=30)
        ev = self._evaluator(_fake(compare_rc=2))
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertFalse(ok)
        self.assertFalse(results["gpqa_diamond"]["assessed"])

    def test_eval_failure_skips_compare(self):
        """评测任务失败 → 该数据集判失败、标无法评估，且不调用 accuracy_compare"""
        write_eval_result(self.tmpdir, total_questions=30)
        fake = _fake(polls=[
            poll_payload(),
            poll_payload("running", pid=1),
            poll_payload("error", pid=1, exit_code=1, log="评测崩溃"),
        ])
        ev = self._evaluator(fake)
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertFalse(ok)
        self.assertFalse(results["gpqa_diamond"]["success"])
        self.assertFalse(results["gpqa_diamond"]["assessed"])
        self.assertEqual(len(fake.calls_containing("accuracy_compare")), 0)

    def test_multi_dataset_all_must_pass(self):
        """多数据集：一个不达标则整体不达标"""
        write_eval_result(self.tmpdir, "gpqa_diamond", total_questions=30)
        write_eval_result(self.tmpdir, "mmlu", total_questions=1140)
        # 注意：判定规则按注册顺序**先匹配者生效**，故不注册通用规则，
        # 只注册按 --metric 区分的两条（通用规则会遮住它们）
        fake = FakeExecutor()
        script_eval_task(fake)
        fake.when("--metric gpqa_diamond", returncode=0,
                  stdout=json.dumps({"nv": {"score": 66.8}, "rel_drop": 0.01}))
        fake.when("--metric mmlu", returncode=1,
                  stdout=json.dumps({"nv": {"score": 69.1}, "rel_drop": 0.2}))
        ev = self._evaluator(fake)
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond", "mmlu"], "Qwen3-8B")
        self.assertFalse(ok)
        self.assertTrue(results["gpqa_diamond"]["qualified"])
        self.assertFalse(results["mmlu"]["qualified"])


class TestEvalCompleteness(unittest.TestCase):
    """结果完整性校验：截断的评测不能被当成"小样本达标"放过去"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.rev = OperatorRevision(revision_id="v3-discovered")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, total_questions, limit_dataset="gpqa_diamond"):
        write_eval_result(self.tmpdir, limit_dataset, total_questions=total_questions)
        ev = V3AccuracyEvaluation(workspace_root=self.tmpdir, container_name="ctr",
                                  executor=_fake(), poll_interval=0)
        return ev.evaluate_accuracy("v3", self.rev, [limit_dataset], "Qwen3-8B")

    def test_truncated_result_rejected(self):
        """要求 30 题却只跑了 5 题（且 compare 判达标）→ 仍判失败，不采纳"""
        ok, results = self._run(total_questions=5)
        self.assertFalse(ok, "截断结果不得判达标")
        r = results["gpqa_diamond"]
        self.assertFalse(r["success"])
        self.assertIn("截断", r["error"])

    def test_missing_result_file_rejected(self):
        """结果文件缺失 → 判失败（不能拿文件里的旧数据凑）"""
        ev = V3AccuracyEvaluation(workspace_root=self.tmpdir, container_name="ctr",
                                  executor=_fake(), poll_interval=0)
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertFalse(ok)
        self.assertIn("不存在", results["gpqa_diamond"]["error"])

    def test_complete_result_accepted(self):
        ok, results = self._run(total_questions=30)
        self.assertTrue(ok)

    def test_wrong_producer_rejected(self):
        """不是 fast_gpqa 产出的结果不算数（防别的工具/手工文件冒充）"""
        write_eval_result(self.tmpdir, total_questions=30, producer="some_other_tool.py")
        ev = V3AccuracyEvaluation(workspace_root=self.tmpdir, container_name="ctr",
                                  executor=_fake(), poll_interval=0)
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertFalse(ok)
        self.assertIn("_producer", results["gpqa_diamond"]["error"])

    def test_missing_score_rejected(self):
        """空分数不参与判定"""
        write_eval_result(self.tmpdir, total_questions=30, score=None)
        ev = V3AccuracyEvaluation(workspace_root=self.tmpdir, container_name="ctr",
                                  executor=_fake(), poll_interval=0)
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertFalse(ok)
        self.assertIn("score", results["gpqa_diamond"]["error"])

    def test_stale_timestamp_rejected(self):
        """上一轮残留的结果（时间戳早于本次评测）不算数"""
        write_eval_result(self.tmpdir, total_questions=30,
                          timestamp="2020-01-01T00:00:00")
        ev = V3AccuracyEvaluation(workspace_root=self.tmpdir, container_name="ctr",
                                  executor=_fake(), poll_interval=0)
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertFalse(ok)
        self.assertIn("上一轮残留", results["gpqa_diamond"]["error"])

    def test_evidence_snapshot_is_per_revision(self):
        """评测证据按 revision 快照留存（否则会被下一个候选覆盖）"""
        write_eval_result(self.tmpdir, total_questions=30)
        ev = V3AccuracyEvaluation(workspace_root=self.tmpdir, container_name="ctr",
                                  executor=_fake(), poll_interval=0)
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertTrue(ok)
        evidence = (results["gpqa_diamond"]["details"] or {}).get("evidence_file", "")
        self.assertIn(self.rev.revision_id, evidence)
        self.assertTrue((Path(self.tmpdir) / evidence).exists(), f"证据文件不存在：{evidence}")


class TestLongTaskReliability(unittest.TestCase):
    """长任务可靠性：静默死亡可识别 / 已运行任务被接管"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.rev = OperatorRevision(revision_id="v3-discovered")
        write_eval_result(self.tmpdir, total_questions=30)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _ev(self, fake):
        return V3AccuracyEvaluation(workspace_root=self.tmpdir, container_name="ctr",
                                    executor=fake, poll_interval=0)

    def test_silent_death_detected(self):
        """state 仍为 running 但进程消失 = 静默死亡 → 判失败并说明（前台阻塞方案发现不了）"""
        fake = _fake(polls=[
            poll_payload(),
            poll_payload("running", pid=1, log="[EVAL] 5/30"),
            poll_payload("running", log="[EVAL] 5/30"),  # pid 空 = 进程没了
        ])
        ok, results = self._ev(fake).evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertFalse(ok)
        self.assertIn("静默死亡", results["gpqa_diamond"]["error"])

    def test_running_task_is_adopted_not_relaunched(self):
        """启动前发现同任务在跑 → 接管等待，不重复启动（防双跑抢 GPU）"""
        fake = _fake(polls=[
            poll_payload("running", pid=777),            # 预检：已在跑
            poll_payload("done", pid=777, exit_code=0),  # 轮询到完成
        ])
        ok, _ = self._ev(fake).evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertTrue(ok)
        self.assertEqual(len(fake.calls_containing("task_runner.py")), 0,
                         "不应重复启动已在运行的任务")

    def test_task_timeout_is_reported(self):
        """task_runner 总闸超时 → 判失败并带上日志尾（不再靠前台阻塞的退出码猜）"""
        fake = _fake(polls=[
            poll_payload(),
            poll_payload("running", pid=1, log="[EVAL] 12/30"),
            poll_payload("timeout", pid=1, log="超过 --max-timeout 总闸"),
        ])
        ok, results = self._ev(fake).evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertFalse(ok)
        err = results["gpqa_diamond"]["error"]
        self.assertIn("timeout", err)
        self.assertIn("总闸", err)  # 日志尾一并带出，便于诊断


if __name__ == "__main__":
    unittest.main()
