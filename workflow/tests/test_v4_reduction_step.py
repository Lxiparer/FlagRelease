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

搜索策略对齐 CLAUDE.md / operator_reduction.py：每轮从 V3 算子集里**随机选 1~3 个
算子只开这几个**，只测两轮，两轮都无性能提升 → 回退 V3。
覆盖：随机子集 + 每轮重启 + 运行时 oplist 核验 → 精度回溯（accuracy_compare 退出码）
→ v4-final 建立/回退 → 条件发布（docker commit/push）。全部经 FakeExecutor。
"""

import json
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.engine.workflow_engine import WorkflowEngine, WORKFLOW_STEPS
from workflow.engine.command_executor import FakeExecutor, ExecResult
from workflow.domain.v4_reduction import pick_random_subset
from workflow.tests.test_engine_e2e import (
    script_service_tools, script_task_blocks, write_eval_result,
)

V4_OPS = ["op_a", "op_b", "op_c"]
SEED = 0


def _bench(throughput: float) -> str:
    return json.dumps({
        "throughput_tokens_per_sec": throughput,
        "ttft_ms": 42.0,
        "tpot_ms": 8.0,
    })


def expected_samples(pool, rounds=2, seed=SEED):
    """复现引擎将要抽到的随机子集（同 seed 同序列）"""
    rng = random.Random(seed)
    return [pick_random_subset(list(pool), rng) for _ in range(rounds)]


_WHITELIST_OK = json.dumps({
    "success": True, "env_inline": "USE_FLAGGEMS=1 VLLM_FL_PREFER_ENABLED=true",
})


def make_v4_fake(throughputs, accuracy_exit: int = 0, commit_ok: bool = True,
                 whitelist_results=None) -> FakeExecutor:
    """V4 场景 fake：benchmark 按调用次序返回递变吞吐（首次为基线测量）。

    whitelist_results: 自定义每轮算子白名单下发结果（序列）；缺省全部成功。
    """
    fake = FakeExecutor()
    script_service_tools(fake)
    # V4：两轮探针各一次重启 + 精度回溯每个候选一次重启与评测 → 序列给足
    script_task_blocks(fake, ["ok"] * 8)
    fake.when_sequence("benchmark_runner", [ExecResult(0, _bench(t)) for t in throughputs])
    if whitelist_results is not None:
        fake.when_sequence("apply_op_config", list(whitelist_results))
    else:
        fake.when("apply_op_config", returncode=0, stdout=_WHITELIST_OK)
    fake.when("fast_gpqa", returncode=0, stdout=json.dumps({"score": 65.5}))
    fake.when(
        "accuracy_compare",
        returncode=accuracy_exit,
        stdout=json.dumps({"nv": {"score": 66.8}, "rel_drop": 0.02}),
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
        eng.context.runtime.model_path = "/models/TestModel"
        eng.startup_tuning_timeout = 0
        eng.startup_tuning_poll_interval = 0
        eng.long_task_poll_interval = 0
        write_eval_result(self.tmpdir)  # 评测结果文件（完整性校验会读）
        eng.v4_seed = SEED
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

    def _applied_whitelists(self, fake) -> list:
        """从 apply_op_config 调用中提取每轮实际下发的算子白名单"""
        out = []
        for call in fake.calls_containing("apply_op_config"):
            joined = " ".join(call)
            marker = "--flagos-whitelist '"
            start = joined.find(marker)
            if start >= 0:
                out.append(joined[start + len(marker):].split("'")[0].split(","))
        return out


class TestV4Search(V4TestBase):
    """步骤11：随机子集性能搜索（两轮）"""

    def test_two_rounds_no_improvement_falls_back(self):
        """两轮都不超基线 → 无候选 → 11/12/13 全 skipped、回退 V3、不发 docker"""
        # 首次是基线测量，之后两轮 trial 都不超
        fake = make_v4_fake([500.0, 480.0, 490.0])
        engine = self._engine(fake, v3_ops=V4_OPS)
        ctx = engine.run()

        self.assertEqual(ctx.steps["11_v4_reduction"].status, "skipped")
        self.assertIn("no_valid_improvement", ctx.steps["11_v4_reduction"].skip_reason)
        self.assertEqual(ctx.gates["v4.established"].status, "failed")
        self.assertNotIn("v4-final", ctx.operator_revisions)
        search = self._art(engine, "v4-search-result")
        self.assertEqual(search["rounds_probed"], 2)  # 只测两轮
        self.assertEqual(search["max_rounds"], 2)
        self.assertEqual(search["candidate_count"], 0)
        self.assertEqual(search["baseline_source"], "measured")
        self.assertEqual(search["seed"], SEED)  # 随机带种子，可复现
        # 回退记录
        fallback = json.loads(self._results("v4_fallback_record.json").read_text())
        self.assertTrue(fallback["fallback_to_v3"])
        self.assertFalse(fake.calls_containing("docker commit"))

    def test_only_sampled_subset_is_enabled(self):
        """V4 采样：每轮只开 1~3 个算子（其余全关），且白名单真的是采样子集"""
        fake = make_v4_fake([500.0, 600.0, 600.0])
        engine = self._engine(fake, v3_ops=V4_OPS)
        engine.execute_step("11_v4_reduction")

        search = self._art(engine, "v4-search-result")
        samples = [t["sampled_ops"] for t in search["trials"]]
        self.assertEqual(samples, expected_samples(V4_OPS))  # 同 seed → 同序列
        for s in samples:
            self.assertTrue(1 <= len(s) <= 3)
            self.assertTrue(set(s).issubset(set(V4_OPS)))

        # 每轮 trial revision：enabled 只有采样集，其余进 disabled 且归 v4_performance
        for t in search["trials"]:
            rev = engine.context.operator_revisions[t["revision_id"]]
            self.assertEqual(list(rev.enabled_ops), t["sampled_ops"])
            self.assertEqual(set(rev.disabled_ops), set(V4_OPS) - set(t["sampled_ops"]))
            self.assertEqual(
                set(rev.disable_reason_categories["v4_performance"]),
                set(V4_OPS) - set(t["sampled_ops"]),
            )

        # 下发到容器的白名单 = 采样集（基线测量 + 2 轮）
        whitelists = self._applied_whitelists(fake)
        self.assertEqual(whitelists, [V4_OPS] + samples)

    def test_restart_and_oplist_verified_each_round(self):
        """每轮都要重启服务（清缓存）并核验运行时 oplist（约束27）"""
        fake = make_v4_fake([500.0, 600.0, 600.0])
        fake.when("flaggems_enable_oplist", stdout="op_a")  # 运行时权威来源
        engine = self._engine(fake, v3_ops=V4_OPS)
        engine.execute_step("11_v4_reduction")

        # 基线 + 2 轮 = 3 次重启（清缓存改由 start_service.sh 内部完成，引擎侧不再单独发 rm）
        self.assertEqual(len(fake.calls_containing("start_service.sh")), 3)
        self.assertEqual(len(self._applied_whitelists(fake)), 3)
        # 核验真的读了运行时 txt
        self.assertTrue(fake.calls_containing("flaggems_enable_oplist"))

    def test_improved_rounds_become_ranked_candidates(self):
        """超基线的轮次成为候选，按吞吐降序；两轮都提升则两个候选"""
        fake = make_v4_fake([500.0, 600.0, 650.0])
        engine = self._engine(fake, v3_ops=V4_OPS)
        engine.execute_step("11_v4_reduction")

        search = self._art(engine, "v4-search-result")
        self.assertEqual(search["candidate_count"], 2)
        self.assertEqual([c["throughput"] for c in search["candidates"]], [650.0, 600.0])
        self.assertEqual(search["baseline_throughput"], 500.0)
        self.assertEqual([c["outcome"] for c in search["trials"]], ["improved", "improved"])
        # 候选 revision 已由引擎写入 context，且未冻结（尚未验证）
        for c in search["candidates"]:
            self.assertIn(c["revision_id"], engine.context.operator_revisions)
            self.assertFalse(engine.context.operator_revisions[c["revision_id"]].frozen)

    def test_service_fail_round_is_discarded(self):
        """某轮服务起不来 → 该轮作废（不计候选），不影响其他轮"""
        fake = make_v4_fake([500.0, 600.0, 600.0], whitelist_results=[
            ExecResult(0, _WHITELIST_OK),   # 基线重启 ok
            ExecResult(0, _WHITELIST_OK),   # 第1轮 ok
            ExecResult(2, "", "bad whitelist"),  # 第2轮起不来
        ])
        engine = self._engine(fake, v3_ops=V4_OPS)
        engine.execute_step("11_v4_reduction")

        search = self._art(engine, "v4-search-result")
        self.assertEqual([t["outcome"] for t in search["trials"]][1], "service_fail")
        self.assertEqual(search["candidate_count"], 1)

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
        fake = make_v4_fake([500.0, 600.0, 600.0], accuracy_exit=0)
        engine = self._engine(fake, v3_ops=V4_OPS)
        engine.run()

        ctx = engine.context
        self.assertEqual(ctx.steps["12_v4_accuracy_check"].status, "success")
        self.assertIn("v4-final", ctx.operator_revisions)
        self.assertTrue(ctx.operator_revisions["v4-final"].frozen)
        self.assertEqual(ctx.gates["accuracy.v4.qualified"].status, "passed")
        self.assertEqual(ctx.gates["v4.established"].status, "passed")
        # v4-final 继承选中候选的算子集（只开采样到的算子）
        search = self._art(engine, "v4-search-result")
        self.assertEqual(
            ctx.operator_revisions["v4-final"].enabled_ops,
            search["candidates"][0]["enabled_ops"],
        )
        # 终检前重启了服务并跑了评测与判定脚本
        self.assertTrue(fake.calls_containing("fast_gpqa"))
        self.assertTrue(fake.calls_containing("accuracy_compare"))
        self.assertTrue(engine.artifact_registry.query_artifacts(
            artifact_type="accuracy-result", tags={"candidate": "v4"},
        ))

    def test_all_candidates_fail_accuracy_falls_back(self):
        """候选全部精度不达标 → 不建立 v4-final、gate failed、回退记录"""
        fake = make_v4_fake([500.0, 600.0, 650.0], accuracy_exit=1)
        engine = self._engine(fake, v3_ops=V4_OPS)
        ctx = engine.run()

        self.assertEqual(ctx.steps["12_v4_accuracy_check"].status, "skipped")
        self.assertIn("精度不达标", ctx.steps["12_v4_accuracy_check"].skip_reason)
        self.assertNotIn("v4-final", ctx.operator_revisions)
        self.assertEqual(ctx.gates["accuracy.v4.qualified"].status, "failed")
        self.assertEqual(ctx.gates["v4.established"].status, "failed")
        report = self._art(engine, "v4-optimization-report")
        self.assertEqual(report["reason"], "accuracy_not_met")
        self.assertEqual(report["phase2_tested"], 2)  # 两个候选都试过
        self.assertTrue(self._results("v4_fallback_record.json").exists())
        self.assertFalse(fake.calls_containing("docker commit"))


class TestV4Release(V4TestBase):
    """步骤13：条件发布"""

    def test_publishes_v4_when_established(self):
        """V4 成立 → docker commit/push 到 flagrelease-public，发布记录落盘"""
        fake = make_v4_fake([500.0, 600.0, 600.0], accuracy_exit=0)
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
        self.assertTrue(engine.artifact_registry.query_artifacts(
            artifact_type="v4-release-decision",
        ))

    def test_commit_failure_blocks_step(self):
        """docker commit 失败 → 步骤13 失败（真实失败，非回退）"""
        fake = make_v4_fake([500.0, 600.0, 600.0], accuracy_exit=0, commit_ok=False)
        engine = self._engine(fake, v3_ops=V4_OPS)
        ctx = engine.run()

        self.assertEqual(ctx.steps["13_v4_release"].status, "failed")
        self.assertIn("image_packaging_failed", ctx.steps["13_v4_release"].fail_reason)
        self.assertFalse(fake.calls_containing("docker push"))


if __name__ == "__main__":
    unittest.main()
