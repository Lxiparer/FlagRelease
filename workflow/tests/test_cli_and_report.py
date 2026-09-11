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

"""M4a 竖片：CLI 入口 / 步骤01 前置校验 / 步骤14 报告 / 状态与容器路径正确性

覆盖 plan 工作段 2（薄入口）与 §9（报告必备字段）的可离线验证部分。
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.cli.main import (
    EXIT_OK, EXIT_PRECONDITION, EXIT_STEP_FAILED, build_parser, exit_code_for_context, main,
)
from workflow.engine.workflow_engine import WorkflowEngine, WORKFLOW_STEPS
from workflow.engine.command_executor import FakeExecutor
from workflow.schemas.context_v2 import ContextSchemaV2, RuntimeInfo, WorkflowStep

from workflow.tests.test_engine_e2e import make_fake


def _ctx_with(statuses: dict) -> ContextSchemaV2:
    """构造只含 steps 的最小 context（用于退出码映射单测）"""
    ctx = ContextSchemaV2()
    ctx.runtime = RuntimeInfo(workflow_run_id="wf-test")
    for step_id, name in WORKFLOW_STEPS:
        ctx.steps[step_id] = WorkflowStep(
            step_id=step_id, step_name=name, status=statuses.get(step_id, "success"),
        )
    return ctx


class TestCliExitCodes(unittest.TestCase):
    """CLI 退出码语义（补上旧 shell 缺失的阶段级语义）"""

    def test_all_success_is_zero(self):
        self.assertEqual(exit_code_for_context(_ctx_with({})), EXIT_OK)

    def test_skipped_counts_as_walked_through(self):
        """skipped 是正常终态（V4 未成立回退、精度已达标跳过调优）→ 退出码 0"""
        ctx = _ctx_with({"11_v4_reduction": "skipped", "12_v4_accuracy_check": "skipped"})
        self.assertEqual(exit_code_for_context(ctx), EXIT_OK)

    def test_step_failure_maps_to_precondition_for_step01(self):
        """步骤01 是引擎侧前置校验 → 归前置条件失败（2）"""
        ctx = _ctx_with({"01_container_preparation": "failed"})
        self.assertEqual(exit_code_for_context(ctx), EXIT_PRECONDITION)

    def test_mid_step_failure_maps_to_step_failed(self):
        ctx = _ctx_with({"06_v3_accuracy": "failed"})
        self.assertEqual(exit_code_for_context(ctx), EXIT_STEP_FAILED)

    def test_gate_failed_is_not_process_failure(self):
        """Gate 没通过（精度不达标）不是流程失败——设计上继续走私有发布"""
        ctx = _ctx_with({})
        ctx.gates = {}
        self.assertEqual(exit_code_for_context(ctx), EXIT_OK)

    def test_parser_requires_workspace_container_model(self):
        parser = build_parser()
        with self.assertRaises(SystemExit) as cm:  # argparse 缺必填 → 退出码 2
            parser.parse_args([])
        self.assertEqual(cm.exception.code, 2)

    def test_bad_workspace_returns_precondition(self):
        rc = main([
            "--workspace", "/nonexistent/wf/workspace",
            "--container", "ctr", "--model", "M",
        ])
        self.assertEqual(rc, EXIT_PRECONDITION)

    def test_unreachable_container_fails_closed_at_step01(self):
        """容器不可达 → 步骤01 前置校验失败、退出码 2、不产生报告"""
        tmp = tempfile.mkdtemp()
        try:
            for sub in ("shared", "results", "logs"):
                (Path(tmp) / sub).mkdir(parents=True, exist_ok=True)
            rc = main([
                "--workspace", tmp,
                "--container", "wf_cli_no_such_ctr_9f3a",
                "--model", "TestModel",
                "--model-path", "/models/TestModel",
            ])
            self.assertEqual(rc, EXIT_PRECONDITION)
            # 前置校验没过 → 不应产出报告
            self.assertFalse((Path(tmp) / "results" / "report.md").exists())
            # 引擎状态照常落盘（可续跑），且落在 config/engine/ 而非 shared/
            self.assertTrue((Path(tmp) / "config" / "engine" / "context.yaml").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestStep01Precondition(unittest.TestCase):
    """步骤01：只校验不创建，缺什么报什么（fail-closed）"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _engine(self, dirs=("shared", "results", "logs"), model_path="/models/M"):
        for sub in dirs:
            (Path(self.tmpdir) / sub).mkdir(parents=True, exist_ok=True)
        eng = WorkflowEngine(self.tmpdir, executor=FakeExecutor())
        eng.context.runtime.container_name = "ctr"
        eng.context.runtime.model_name = "M"
        eng.context.runtime.model_path = model_path
        return eng

    def test_missing_dirs_reported(self):
        eng = self._engine(dirs=("shared",))
        result = eng.execute_step("01_container_preparation")
        self.assertEqual(result.status, "failed")
        self.assertIn("results/", result.fail_reason)
        self.assertIn("logs/", result.fail_reason)

    def test_missing_model_path_reported(self):
        eng = self._engine(model_path="")
        result = eng.execute_step("01_container_preparation")
        self.assertEqual(result.status, "failed")
        self.assertIn("model_path", result.fail_reason)

    def test_unreachable_container_reported(self):
        eng = self._engine()
        eng.executor.when("docker inspect", returncode=1, stderr="No such container")
        result = eng.execute_step("01_container_preparation")
        self.assertEqual(result.status, "failed")
        self.assertIn("容器不可达", result.fail_reason)

    def test_pass_registers_precondition_evidence(self):
        eng = self._engine()
        result = eng.execute_step("01_container_preparation")
        self.assertEqual(result.status, "success")
        self.assertTrue(result.output_artifacts)
        content = eng.artifact_registry.load_artifact_content(result.output_artifacts[0])
        self.assertEqual(content["container_name"], "ctr")
        self.assertTrue(content["container_reachable"])
        self.assertIn("workspace_mount", content)


class TestStep14Report(unittest.TestCase):
    """步骤14：新 schema 报告（plan §9 必备字段，不含 V1/V2 性能比）"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        for sub in ("shared", "results", "logs"):
            (Path(self.tmpdir) / sub).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_full(self, accuracy_exit=0, throughputs=None):
        fake = make_fake(accuracy_exit=accuracy_exit)
        if throughputs is not None:
            from workflow.engine.command_executor import ExecResult
            fake.when_sequence("benchmark_runner", [
                ExecResult(0, json.dumps({"throughput_tokens_per_sec": t,
                                          "ttft_ms": 1.0, "tpot_ms": 1.0}))
                for t in throughputs
            ])
        eng = WorkflowEngine(self.tmpdir, executor=fake)
        eng.context.runtime.container_name = "ctr"
        eng.context.runtime.model_name = "TestModel"
        eng.context.runtime.model_path = "/models/TestModel"
        eng.v4_max_rounds = 2
        eng.run()
        return eng

    def test_report_files_written(self):
        eng = self._run_full()
        self.assertTrue((Path(self.tmpdir) / "results" / "report.md").exists())
        self.assertTrue((Path(self.tmpdir) / "results" / "report.json").exists())
        self.assertTrue((Path(self.tmpdir) / "shared" / "context_final.yaml").exists())
        self.assertTrue((Path(self.tmpdir) / "config" / "engine" / "artifacts" / "index.json").exists())

    def test_report_covers_plan_required_fields(self):
        eng = self._run_full()
        report = json.loads((Path(self.tmpdir) / "results" / "report.json").read_text())

        self.assertEqual(len(report["steps"]), 15)                      # step 执行状态
        self.assertIn("admission", {g["gate_id"] for g in report["gates"]})  # Gate 状态
        self.assertIn("artifacts_summary", report)                      # Artifact 校验状态
        self.assertTrue(report["operator_revisions"])                   # operator revision
        self.assertIn("v3", report["establishment"])                    # V3 establishment
        self.assertIn("v4", report["establishment"])                    # V4 establishment
        self.assertIn("release", report)                                # release class/destination
        self.assertIn("fallback_to_v3", report["establishment"]["v4"])  # V4 fallback 原因
        self.assertIn("v3_performance", report)                         # 性能绝对值

    def test_report_has_no_v1_v2_performance_ratio(self):
        """plan 明令：新报告不得出现 V1/V2 性能比（新流程无本地 V1）"""
        eng = self._run_full()
        md = (Path(self.tmpdir) / "results" / "report.md").read_text()
        # 注意别用裸 "ratio"——它会在 "prepa|ratio|n" 里误命中
        for forbidden in ("V1/V2", "V2/V1", "relative to V1", "native_performance",
                          "min_ratio", "target_ratio", "performance_ratio",
                          "基线×", "×1.05", "80% of V1"):
            self.assertNotIn(forbidden, md)
        self.assertIn("不产出任何版本间比值", md)

    def test_v4_fallback_reason_surfaced(self):
        """V4 无提升 → 报告需显出回退 V3 与其原因"""
        eng = self._run_full()  # make_fake 吞吐恒定 → V4 无提升
        report = json.loads((Path(self.tmpdir) / "results" / "report.json").read_text())
        self.assertTrue(report["establishment"]["v4"]["fallback_to_v3"])
        self.assertIn("no_valid_improvement", report["establishment"]["v4"]["gate_reason"])
        md = (Path(self.tmpdir) / "results" / "report.md").read_text()
        self.assertIn("回退 V3", md)

    def test_context_final_is_v2_schema_and_terminal(self):
        """context_final 是 v2 schema，且在步骤15 写完 finished_at 后被刷新为真正终态"""
        eng = self._run_full()
        self.assertEqual(eng.context.runtime.finished_at is not None, True)
        final = yaml.safe_load((Path(self.tmpdir) / "shared" / "context_final.yaml").read_text())
        self.assertEqual(final["schema_version"], "2.0")
        self.assertEqual(len(final["steps"]), 15)
        self.assertTrue(final["runtime"]["finished_at"])
        self.assertTrue(final["steps"]["15_finalize"]["status"] == "success")


class TestMigrationIsolation(unittest.TestCase):
    """迁移期隔离：引擎不碰 legacy 状态文件，也不复现 legacy 的路径漂移"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        for sub in ("shared", "results", "logs"):
            (Path(self.tmpdir) / sub).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_legacy_context_untouched(self):
        """legacy shared/context.yaml（旧 schema）在引擎跑完后必须逐字节不变"""
        legacy_src = Path(__file__).parent.parent.parent / "shared" / "context.template.yaml"
        legacy = Path(self.tmpdir) / "shared" / "context.yaml"
        shutil.copyfile(legacy_src, legacy)
        before = legacy.read_bytes()

        eng = WorkflowEngine(self.tmpdir, executor=make_fake())
        eng.context.runtime.container_name = "ctr"
        eng.context.runtime.model_name = "TestModel"
        eng.context.runtime.model_path = "/models/TestModel"
        eng.run()

        self.assertEqual(legacy.read_bytes(), before, "legacy context.yaml 被引擎改写了")
        self.assertTrue((Path(self.tmpdir) / "config" / "engine" / "context.yaml").exists())

    def test_second_run_starts_from_archived_state(self):
        """每轮 config/engine/ 归档 → 第二轮从干净状态起跑（不静默 no-op）"""
        eng = WorkflowEngine(self.tmpdir, executor=make_fake())
        eng.context.runtime.container_name = "ctr"
        eng.context.runtime.model_name = "TestModel"
        eng.context.runtime.model_path = "/models/TestModel"
        eng.run()

        # 模拟宿主编排的每轮归档（把 config/ 整体移走）
        archive = Path(self.tmpdir) / "archive" / "20260911_000000"
        archive.mkdir(parents=True)
        shutil.move(str(Path(self.tmpdir) / "config"), str(archive / "config"))

        eng2 = WorkflowEngine(self.tmpdir, executor=make_fake())
        self.assertEqual(eng2.detect_recovery_point(), "01_container_preparation")
        self.assertFalse(eng2.context.runtime.finished_at)


class TestContainerPaths(unittest.TestCase):
    """容器内工具路径必须与 setup_workspace.sh 的实际部署一致（否则真跑 file-not-found）"""

    def setUp(self):
        self.repo = Path(__file__).parent.parent.parent
        self.setup_sh = (self.repo / "skills" / "flagos-container-preparation"
                         / "tools" / "setup_workspace.sh").read_text()

    def test_accuracy_scripts_deployed_to_scripts(self):
        from workflow.domain import v3_accuracy
        for path in (v3_accuracy.EVAL_SCRIPT, v3_accuracy.ACCURACY_COMPARE):
            self.assertTrue(path.startswith("/flagos-workspace/scripts/"), path)
            name = path.rsplit("/", 1)[-1]
            # SCRIPT_MAP 里该文件名被投到 scripts/ 下
            self.assertRegex(self.setup_sh, rf"tools/{name}:scripts/{name}")
            # 且不能引用容器里不存在的 skills/ 目录
            self.assertNotIn("/flagos-workspace/skills/", path)

    def test_apply_op_config_dir_is_sibling_of_its_import(self):
        from workflow.domain import v3_startup_tuning as t
        self.assertEqual(t.APPLY_OP_CONFIG_DIR, "/flagos-workspace/scripts")
        self.assertIn("tools/apply_op_config.py:scripts/apply_op_config.py", self.setup_sh)
        # apply_op_config.py 依赖同目录的 flagos_op_config.py
        self.assertIn("tools/flagos_op_config.py:scripts/flagos_op_config.py", self.setup_sh)

    def test_no_workflow_module_references_skills_dir(self):
        """workflow/ 生产代码不得出现容器内不存在的 /flagos-workspace/skills/ 路径"""
        hits = []
        for py in (self.repo / "workflow").rglob("*.py"):
            if "tests" in py.parts:
                continue  # 测试里会因为断言本身出现该字面量
            for lineno, line in enumerate(py.read_text().splitlines(), 1):
                if "/flagos-workspace/skills/" in line:
                    hits.append(f"{py.name}:{lineno}")
        self.assertEqual(hits, [], f"引用了容器内不存在的路径: {hits}")


if __name__ == "__main__":
    unittest.main()
