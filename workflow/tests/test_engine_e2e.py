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

"""WorkflowEngine 端到端骨架测试（M0）

验证引擎能确定性驱动 15 步空跑、状态经 YAML 往返、非法/篡改写入被拦、
中断可从断点恢复。domain 执行器为 stub，不接容器 / 不接 Claude。
"""

import unittest
import sys
import tempfile
import shutil
import json
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.engine.workflow_engine import WorkflowEngine, WORKFLOW_STEPS
from workflow.engine.state_store import YamlStateStore
from workflow.engine.command_executor import FakeExecutor
from workflow.schemas.context_v2 import (
    ContextSchemaV2,
    ContextValidationError,
)


FULL_CAPS = {
    "flaggems_installed": True, "flaggems_version": "5.1.0",
    "vllm_plugin_installed": True, "plugin_version": "0.1",
    "vllm_version": "0.7.3", "flagtree": {"installed": True, "version": "0.5.0"},
}


def make_fake(admitted: bool = True, accuracy_exit: int = 0) -> FakeExecutor:
    """构造脚本化 FakeExecutor：满足步骤02 准入 + 步骤06 精度。"""
    fake = FakeExecutor()
    caps = dict(FULL_CAPS)
    if not admitted:
        caps["vllm_plugin_installed"] = False
    fake.when("inspect_env", stdout=json.dumps(caps))
    fake.when("stat -c %Y", stdout=str(int(time.time())))  # freshness（须先于 oplist 规则）
    fake.when("flaggems_enable_oplist",
              stdout="\n".join(f"op_{i}" for i in range(80)))  # 步骤03 oplist 发现
    fake.when("fast_gpqa", returncode=0, stdout=json.dumps({"score": 65.5}))
    fake.when(
        "accuracy_compare",
        returncode=accuracy_exit,
        stdout=json.dumps({"nv": {"score": 66.8}, "rel_drop": 0.02,
                           "aligned": accuracy_exit == 0}),
    )
    fake.when(
        "benchmark_runner",
        returncode=0,
        stdout=json.dumps({"throughput_tokens_per_sec": 1234.5, "ttft_ms": 42.0,
                           "tpot_ms": 8.0}),
    )
    fake.when("docker commit", returncode=0)  # 步骤10 打包
    fake.when("docker push", returncode=0)    # 步骤10 上传
    return fake



class TestEngineEndToEnd(unittest.TestCase):
    """引擎端到端空跑"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _engine(self, fake=None) -> WorkflowEngine:
        """构造引擎并注入 fake executor + 容器/模型名（步骤02/06 需要）。"""
        eng = WorkflowEngine(self.tmpdir, executor=fake or make_fake())
        eng.context.runtime.container_name = "test_ctr"
        eng.context.runtime.model_name = "TestModel"
        return eng

    def test_run_all_15_steps(self):
        """run() 应驱动全部 15 步走完、终点停在 15_finalize（02/03/06/08/10 真实 handler 对 fake）

        V4 三连（11/12/13）在本 fake 下无性能提升（benchmark 恒定吞吐）→ skipped + 回退 V3，
        这是正确结论而非失败。
        """
        engine = self._engine()
        ctx = engine.run()

        self.assertEqual(len(WORKFLOW_STEPS), 15)
        for step_id, _ in WORKFLOW_STEPS:
            self.assertIn(
                ctx.steps[step_id].status, ("success", "skipped"),
                f"step {step_id} 未走完: {ctx.steps[step_id].status}",
            )
        self.assertEqual(ctx.current_step_id, "15_finalize")
        # V4 无合法提升 → 三连 skipped，v4.established 失败，无 v4-final
        for sid in ("11_v4_reduction", "12_v4_accuracy_check", "13_v4_release"):
            self.assertEqual(ctx.steps[sid].status, "skipped", f"{sid} 应 skipped")
        self.assertEqual(ctx.gates["v4.established"].status, "failed")
        self.assertNotIn("v4-final", ctx.operator_revisions)
        # 回退记录落盘
        self.assertTrue((Path(self.tmpdir) / "results" / "v4_fallback_record.json").exists())
        # 冻结类步骤应产生冻结的 revision
        self.assertTrue(ctx.operator_revisions["v3-discovered"].frozen)
        self.assertTrue(ctx.operator_revisions["v3-final"].frozen)
        # 步骤03 应把真实发现的 oplist（80 算子）灌入 v3-discovered
        self.assertEqual(len(ctx.operator_revisions["v3-discovered"].enabled_ops), 80)
        # finalize 应记录结束时间
        self.assertTrue(ctx.runtime.finished_at)
        # 真实 handler 应落 gate
        self.assertEqual(ctx.gates["admission"].status, "passed")
        self.assertEqual(ctx.gates["accuracy.v3.qualified"].status, "passed")
        # 步骤08 性能测量应登记 artifact（纯测量、无 gate）
        self.assertTrue(ctx.steps["08_v3_performance"].output_artifacts)

    def test_admission_fail_closed_stops_at_02(self):
        """缺组件 → 准入 fail-closed → run() 停在 02_admission"""
        engine = self._engine(make_fake(admitted=False))
        ctx = engine.run()

        self.assertEqual(ctx.current_step_id, "02_admission")
        self.assertEqual(ctx.steps["02_admission"].status, "failed")
        self.assertEqual(ctx.gates["admission"].status, "failed")
        # 后续步骤未执行
        self.assertEqual(ctx.steps["06_v3_accuracy"].status, "pending")

    def test_accuracy_not_qualified_continues(self):
        """精度不达标（accuracy_compare exit 1）→ gate failed 但流程继续（调优是步骤07）"""
        engine = self._engine(make_fake(accuracy_exit=1))
        ctx = engine.run()

        self.assertEqual(ctx.current_step_id, "15_finalize")
        self.assertEqual(ctx.steps["06_v3_accuracy"].status, "success")
        self.assertEqual(ctx.gates["accuracy.v3.qualified"].status, "failed")

    def test_context_yaml_roundtrip(self):
        """状态应落进 context.yaml 并能等价重建"""
        engine = self._engine()
        engine.run()

        context_file = Path(self.tmpdir) / "shared" / "context.yaml"
        self.assertTrue(context_file.exists())

        with open(context_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        rebuilt = ContextSchemaV2.from_dict(data)

        # from_dict ∘ (yaml) ∘ to_dict 应与引擎内存态等价
        self.assertEqual(rebuilt.to_dict(), engine.context.to_dict())

    def test_recovery_from_failed_step(self):
        """中断续跑：从失败步骤恢复，不从头重跑"""
        engine = WorkflowEngine(self.tmpdir, executor=make_fake())
        engine.context.runtime.container_name = "test_ctr"
        engine.context.runtime.model_name = "TestModel"
        # 模拟：01-04 成功、05 失败、其余 pending
        for sid in ["01_container_preparation", "02_admission",
                    "03_v3_discovery_startup", "04_v3_discovered"]:
            engine.context.steps[sid].status = "success"
        engine.context.steps["05_v3_startup_tuning"].status = "failed"
        engine.context.current_step_id = "05_v3_startup_tuning"
        engine._save_context()

        # 新引擎从磁盘加载并续跑
        engine2 = WorkflowEngine(self.tmpdir, executor=make_fake())
        self.assertEqual(engine2.detect_recovery_point(), "05_v3_startup_tuning")
        ctx = engine2.run()

        for step_id, _ in WORKFLOW_STEPS:
            self.assertIn(ctx.steps[step_id].status, ("success", "skipped"))
        self.assertEqual(ctx.current_step_id, "15_finalize")


class TestStateStoreValidation(unittest.TestCase):
    """StateStore 写入校验（字段权限 + 篡改防护）"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = YamlStateStore(Path(self.tmpdir) / "context.yaml")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_accept_valid_minimal(self):
        """合法最小 context（字段是 schema 子集）应通过"""
        self.store.save({"schema_version": "2.0", "steps": {}})
        self.assertEqual(self.store.load()["schema_version"], "2.0")

    def test_reject_unknown_toplevel_field(self):
        """schema 外的顶层字段应被拒绝"""
        with self.assertRaises(ContextValidationError):
            self.store.save({"schema_version": "2.0", "bogus_field": 1})

    def test_reject_wrong_schema_version(self):
        """错误 schema_version 应被拒绝"""
        with self.assertRaises(ContextValidationError):
            self.store.save({"schema_version": "1.0"})

    def test_reject_frozen_revision_tamper(self):
        """篡改已冻结 revision 应被拒绝"""
        base = {
            "schema_version": "2.0",
            "operator_revisions": {
                "r1": {"revision_id": "r1", "frozen": True, "enabled_ops": []},
            },
        }
        self.store.save(base)  # 首次写入，old=None
        tampered = {
            "schema_version": "2.0",
            "operator_revisions": {
                "r1": {"revision_id": "r1", "frozen": True, "enabled_ops": ["x"]},
            },
        }
        with self.assertRaises(ContextValidationError):
            self.store.save(tampered)

    def test_reject_success_step_regression(self):
        """已 success 的步骤回退状态应被拒绝"""
        base = {
            "schema_version": "2.0",
            "steps": {"s": {"step_id": "s", "status": "success"}},
        }
        self.store.save(base)
        regressed = {
            "schema_version": "2.0",
            "steps": {"s": {"step_id": "s", "status": "pending"}},
        }
        with self.assertRaises(ContextValidationError):
            self.store.save(regressed)


if __name__ == "__main__":
    unittest.main()
