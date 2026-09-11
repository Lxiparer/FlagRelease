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

"""V4 减算子竖片（步骤11/12/13，M1b）

覆盖：阶段1 性能搜索（仅绝对提升才推进）→ 阶段2 精度回溯（判定走 accuracy_compare
退出码）→ v4-final 建立/回退 → 条件发布（docker commit/push）。
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

V4_OPS = ["op_a", "op_b", "op_c"]


def _bench(throughput: float) -> str:
    return json.dumps({
        "throughput_tokens_per_sec": throughput,
        "ttft_ms": 42.0,
        "tpot_ms": 8.0,
    })


def make_v4_fake(throughputs, accuracy_exit: int = 0, commit_ok: bool = True) -> FakeExecutor:
    """V4 场景 fake：benchmark 按调用次序返回递变吞吐；精度判定返回指定退出码。"""
    fake = FakeExecutor()
    fake.when_sequence("benchmark_runner", [ExecResult(0, _bench(t)) for t in throughputs])
    fake.when("fast_gpqa", returncode=0, stdout=json.dumps({"score": 65.5}))
    fake.when(
        "accuracy_compare",
        returncode=accuracy_exit,
        stdout=json.dumps({"nv": {"score": 66.8}, "rel_drop": 0.02,
                           "aligned": accuracy_exit == 0}),
    )
    if commit_ok:
        fake.when("docker commit", returncode=0)
        fake.when("docker push", returncode=0)
    else:
        fake.when("docker commit", returncode=1, stderr="no space left on device")
    return fake


class V4TestBase(unittest.TestCase):
    """预置 01-10 已成功 + v3-final 已冻结，只跑 V4 三连。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _engine(self, fake: FakeExecutor, v3_ops=None) -> WorkflowEngine:
        eng = WorkflowEngine(self.tmpdir, executor=fake)
        eng.context.runtime.container_name = "test_ctr"
        eng.context.runtime.model_name = "TestModel"
        for sid, _ in WORKFLOW_STEPS[:10]:
            eng.context.steps[sid].status = "success"
        if v3_ops is not None:
            eng.create_operator_revision(
                "v3-final", parent_revision_id=None, enabled_ops=list(v3_ops),
            )
            eng.freeze_revision("v3-final")
        eng.context.current_step_id = "11_v4_reduction"
        eng._save_context()
        return eng

    def _art(self, engine, artifact_type):
        art_id = engine.artifact_registry.get_latest_artifact(artifact_type)
        self.assertIsNotNone(art_id, f"缺 artifact: {artifact_type}")
        return engine.artifact_registry.load_artifact_content(art_id)

    def _results(self, name):
        return Path(self.tmpdir) / "results" / name


class TestV4Search(V4TestBase):
    """步骤11：性能搜索"""

    def test_no_improvement_skips_and_falls_back(self):
        """吞吐恒定 → 无合法提升 → 11/12/13 全 skipped，落回退记录，无 v4-final"""
        fake = make_v4_fake([500.0])  # 序列末项重复：所有测量都返回 500
        engine = self._engine(fake, v3_ops=V4_OPS)
        ctx = engine.run()

        self.assertEqual(ctx.steps["11_v4_reduction"].status, "skipped")
        self.assertIn("no_valid_improvement",
                      ctx.steps["11_v4_reduction"].skip_reason)
        self.assertEqual(ctx.gates["v4.established"].status, "failed")
        self.assertNotIn("v4-final", ctx.operator_revisions)
        # 回退记录：不产出 V4，V3 为最终交付版
        fallback = json.loads(self._results("v4_fallback_record.json").read_text())
        self.assertTrue(fallback["fallback_to_v3"])
        self.assertFalse(fallback["established"])
        # 未发 docker commit/push
        self.assertFalse(fake.calls_containing("docker commit"))

    def test_only_absolute_improvement_advances_baseline(self):
        """基线动态推进：仅当试禁用后吞吐 > 当前最优才产生候选"""
        # call1 基线 500 → t1 600（提交）→ t2 600（不提）→ t3 600（不提）
        fake = make_v4_fake([500.0, 600.0, 600.0, 600.0])
        engine = self._engine(fake, v3_ops=V4_OPS)
        engine.execute_step("11_v4_reduction")

        search = self._art(engine, "v4-search-result")
        self.assertEqual(search["baseline_throughput"], 500.0)
        self.assertEqual(search["trials"], 3)
        self.assertEqual(search["candidate_count"], 1)
        cand = search["candidates"][0]
        self.assertEqual(cand["throughput"], 600.0)
        self.assertEqual(cand["enabled_ops"], ["op_b", "op_c"])
        self.assertEqual(cand["disabled_ops"], {"op_a": cand["disabled_ops"]["op_a"]})
        # 候选 revision 已由引擎写入 context（唯一写入者），且未冻结
        self.assertIn(cand["revision_id"], engine.context.operator_revisions)
        self.assertFalse(engine.context.operator_revisions[cand["revision_id"]].frozen)

    def test_missing_v3_final_skips(self):
        """v3-final 未建立 → V4 三连 skipped，gate 原因指向 v3-final"""
        fake = make_v4_fake([500.0, 600.0])
        engine = self._engine(fake, v3_ops=None)
        ctx = engine.run()

        self.assertEqual(ctx.steps["11_v4_reduction"].status, "skipped")
        self.assertEqual(ctx.steps["13_v4_release"].status, "skipped")
        self.assertIn("v3-final", ctx.gates["v4.established"].reason)


class TestV4AccuracyBacktrack(V4TestBase):
    """步骤12：精度回溯 + 终检"""

    def test_qualified_candidate_establishes_v4_final(self):
        """精度达标 → v4-final 冻结、双 gate passed、终检走 accuracy_compare"""
        fake = make_v4_fake([500.0, 600.0, 600.0, 600.0], accuracy_exit=0)
        engine = self._engine(fake, v3_ops=V4_OPS)
        engine.run()

        ctx = engine.context
        self.assertEqual(ctx.steps["12_v4_accuracy_check"].status, "success")
        self.assertIn("v4-final", ctx.operator_revisions)
        self.assertTrue(ctx.operator_revisions["v4-final"].frozen)
        self.assertEqual(ctx.gates["accuracy.v4.qualified"].status, "passed")
        self.assertEqual(ctx.gates["v4.established"].status, "passed")
        # 终检真的跑了精度评测与判定脚本
        self.assertTrue(fake.calls_containing("fast_gpqa"))
        self.assertTrue(fake.calls_containing("accuracy_compare"))
        # 精度证据以 candidate=v4 登记
        v4_acc = engine.artifact_registry.query_artifacts(
            artifact_type="accuracy-result", tags={"candidate": "v4"},
        )
        self.assertTrue(v4_acc)

    def test_all_candidates_fail_accuracy_falls_back(self):
        """候选全部精度不达标 → 不建立 v4-final、gate failed、回退记录"""
        fake = make_v4_fake([500.0, 600.0, 600.0, 600.0], accuracy_exit=1)
        engine = self._engine(fake, v3_ops=V4_OPS)
        ctx = engine.run()

        self.assertEqual(ctx.steps["12_v4_accuracy_check"].status, "skipped")
        self.assertIn("精度不达标", ctx.steps["12_v4_accuracy_check"].skip_reason)
        self.assertNotIn("v4-final", ctx.operator_revisions)
        self.assertEqual(ctx.gates["accuracy.v4.qualified"].status, "failed")
        self.assertEqual(ctx.gates["v4.established"].status, "failed")
        report = self._art(engine, "v4-optimization-report")
        self.assertEqual(report["reason"], "accuracy_not_met")
        self.assertTrue(self._results("v4_fallback_record.json").exists())
        self.assertFalse(fake.calls_containing("docker commit"))


class TestV4Release(V4TestBase):
    """步骤13：条件发布"""

    def test_publishes_v4_when_established(self):
        """V4 成立 → docker commit/push 到 flagrelease-public，发布记录落盘"""
        fake = make_v4_fake([500.0, 600.0, 600.0, 600.0], accuracy_exit=0)
        engine = self._engine(fake, v3_ops=V4_OPS)
        ctx = engine.run()

        self.assertEqual(ctx.steps["13_v4_release"].status, "success")
        commits = fake.calls_containing("docker commit")
        self.assertEqual(len(commits), 1)
        self.assertIn("-v4", " ".join(commits[0]))
        self.assertTrue(fake.calls_containing("docker push"))
        record = json.loads(self._results("v4_release_record.json").read_text())
        self.assertTrue(record["gates"]["v4_established"]["passed"])
        self.assertIn("harbor.baai.ac.cn/flagrelease-public",
                      record["artifacts"]["published_to"])
        self.assertEqual(record["operator_count"], len(V4_OPS) - 1)
        # 发布决策登记 artifact
        self.assertTrue(engine.artifact_registry.query_artifacts(
            artifact_type="v4-release-decision",
        ))

    def test_commit_failure_blocks_step(self):
        """docker commit 失败 → 步骤13 失败（真实失败，非回退）"""
        fake = make_v4_fake([500.0, 600.0, 600.0, 600.0], accuracy_exit=0, commit_ok=False)
        engine = self._engine(fake, v3_ops=V4_OPS)
        ctx = engine.run()

        self.assertEqual(ctx.steps["13_v4_release"].status, "failed")
        self.assertIn("image_packaging_failed", ctx.steps["13_v4_release"].fail_reason)
        self.assertFalse(fake.calls_containing("docker push"))


if __name__ == "__main__":
    unittest.main()
