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

import copy
import unittest
import sys
import tempfile
import shutil
import json
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.engine.workflow_engine import WorkflowEngine, WORKFLOW_STEPS
from workflow.engine.state_store import YamlStateStore
from workflow.engine.command_executor import ExecResult, FakeExecutor
from workflow.schemas.context_v2 import (
    ContextSchemaV2,
    ContextValidationError,
)


# inspect_env.py 的真实输出形状（**嵌套**；真机核对过，见 test_cli_and_report.TestInspectEnvMapping）
FULL_CAPS = {
    "execution": {"mode": "container"},
    "inspection": {
        "core_packages": {"torch": "2.5.0", "vllm": "0.7.3", "torch_cuda": "12.4"},
        "flag_packages": {"flaggems": "5.1.0", "flagscale": "-", "flagcx": "-",
                          "vllm_plugin": "installed"},
        "vllm_plugin_installed": True,
    },
    "flagtree": {"installed": True, "version": "0.5.0", "triton_version": "3.2.0"},
}


def poll_payload(status: str = "", pid: Optional[int] = None, log: str = "",
                 exit_code: Optional[int] = None) -> str:
    """构造一次长任务轮询的 stdout（与 LongTaskRunner 的解析格式一致）"""
    from workflow.engine.long_task import MARK_LOG, MARK_PID
    state = {}
    if status:
        state = {"status": status, "pid": pid or 4321}
        if exit_code is not None:
            state["exit_code"] = exit_code
    return (f"{json.dumps(state) if state else ''}\n{MARK_LOG}\n{log}\n{MARK_PID}\n"
            f"{pid if pid else ''}\n")


def script_service_tools(fake: FakeExecutor):
    """脚本化"起服务/等就绪"用到的既有工具（detect_gpu / calc_tp_size / start_service.sh）

    起服务已改为调用容器内既有工具（不再内联拼 vllm 命令），测试必须给它们应答。
    """
    fake.when("detect_gpu", stdout=json.dumps({
        "vendor": "nvidia", "free_gpus": [1, 2, 3], "busy_gpus": [0],
        "total": 4, "visible_devices_env": "CUDA_VISIBLE_DEVICES",
    }))
    fake.when("calc_tp_size", stdout=json.dumps({"recommended_tp": 1, "gpu_count": 4}))
    return fake


def script_eval_task(fake: FakeExecutor, polls: Optional[List[str]] = None):
    """让 FakeExecutor 应答长任务协议（评测 detached + 轮询）

    缺省：预检无任务 → 第一轮 running（进程存活）→ 第二轮 done。
    注意轮询命令含 MARK_LOG，故用同一个 when_sequence 覆盖所有轮询。
    """
    seq = polls if polls is not None else [
        poll_payload(),                          # 启动前预检：无 state 无进程
        poll_payload("running", pid=4321, log="[EVAL] 10/30"),
        poll_payload("done", pid=4321, log="[EVAL] 完成", exit_code=0),
    ]
    fake.when_sequence("FLAGOS-TASK-LOG", [ExecResult(0, s) for s in seq])
    return fake


def task_block(kind: str) -> List[str]:
    """一个长任务的轮询序列：ok=成功；fail=静默死亡（终态，不会无限轮询）"""
    if kind == "ok":
        return [poll_payload(), poll_payload("running", pid=4321), poll_payload("done", pid=4321, exit_code=0)]
    if kind == "fail":
        return [poll_payload(), poll_payload("running", pid=4321), poll_payload("running")]
    raise ValueError(f"unknown kind: {kind}")


def script_task_blocks(fake: FakeExecutor, blocks: List[str]):
    """按顺序脚本化多个长任务（如"第1轮起服务失败 → 第2轮成功"）

    每个块对应一次 LongTaskRunner.run 的完整轮询；序列用尽后重复最后一项。
    会**先清掉既有的长任务规则**——规则先注册先匹配，直接追加不会生效。
    """
    seq: List[str] = []
    for b in blocks:
        seq.extend(task_block(b))
    fake.clear_rules("FLAGOS-TASK-LOG")
    fake.clear_rules("FLAGOS-TASK-LOG")
    fake.when_sequence("FLAGOS-TASK-LOG", [ExecResult(0, s) for s in seq])
    return fake


def write_eval_result(workspace, dataset: str = "gpqa_diamond",
                      total_questions: int = 30, score: float = 65.5,
                      producer: str = "fast_gpqa.py", timestamp: Optional[str] = None) -> Path:
    """写出评测结果文件（结果有效性校验会读它）

    时间戳默认为"现在 + 300s"：真实评测是在**评测过程中**写出结果的，
    而测试是在 setUp 里预先写好、之后才跑评测——用未来的时间戳模拟这一时序，
    否则会被"timestamp 早于本次评测开始 → 疑似上一轮残留"正确拦下。
    """
    path = Path(workspace) / "results" / f"{dataset}_flagos_optimized.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = timestamp or (datetime.now() + timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(json.dumps({
        "score": score, "total_questions": total_questions,
        "_producer": producer, "timestamp": ts, "benchmark": dataset,
        "_meta": {"test": "fixture"},
    }), encoding="utf-8")
    return path


def make_fake(admitted: bool = True, accuracy_exit: int = 0) -> FakeExecutor:
    """构造脚本化 FakeExecutor：满足步骤02 准入 + 步骤06 精度（含长任务协议）。"""
    fake = FakeExecutor()
    caps = copy.deepcopy(FULL_CAPS)  # 嵌套结构，必须深拷贝
    if not admitted:
        caps["inspection"]["vllm_plugin_installed"] = False
    fake.when("inspect_env", stdout=json.dumps(caps))
    fake.when("stat -c %Y", stdout=str(int(time.time())))  # freshness（须先于 oplist 规则）
    fake.when("flaggems_enable_oplist",
              stdout="\n".join(f"op_{i}" for i in range(80)))  # 步骤03 oplist 发现
    script_service_tools(fake)  # 起服务/等就绪走既有工具
    # 起服务等就绪 + 调优各轮重启 + 评测都会走长任务，序列要够长；
    # 用尽后会重复末项（done），后续任务会"接管终态"而不再发命令
    script_task_blocks(fake, ["ok"] * 6)
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
    # 步骤05 启动调优：plugin 白名单生成（curl /health 走默认 ok → 一次就绪）
    fake.when("apply_op_config", returncode=0, stdout=json.dumps({
        "success": True, "mode": "custom",
        "env_inline": "USE_FLAGGEMS=1 VLLM_FL_PREFER_ENABLED=true",
    }))
    return fake



class TestEngineEndToEnd(unittest.TestCase):
    """引擎端到端空跑"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # 步骤01 前置校验要求 setup_workspace.sh 产出的目录结构就位
        for sub in ("shared", "results", "logs"):
            (Path(self.tmpdir) / sub).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _engine(self, fake=None) -> WorkflowEngine:
        """构造引擎并注入 fake executor + 容器/模型名（步骤01/02/06 需要）。"""
        eng = WorkflowEngine(self.tmpdir, executor=fake or make_fake())
        eng.context.runtime.container_name = "test_ctr"
        eng.context.runtime.model_name = "TestModel"
        eng.context.runtime.model_path = "/models/TestModel"
        # V4 只测两轮（默认值）；e2e 的 fake 吞吐恒定 → 无提升 → 回退 V3
        eng.v4_max_rounds = 2
        # 长任务轮询不睡（默认 60s）；并写出评测结果文件（完整性校验会读）
        eng.long_task_poll_interval = 0
        write_eval_result(self.tmpdir)
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
        """状态应落进引擎状态文件并能等价重建（不碰 legacy shared/context.yaml）"""
        engine = self._engine()
        engine.run()

        context_file = Path(self.tmpdir) / "config" / "engine" / "context.yaml"
        self.assertTrue(context_file.exists())
        # legacy 状态文件绝不被引擎创建/覆写（迁移期隔离）
        self.assertFalse((Path(self.tmpdir) / "shared" / "context.yaml").exists())

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
        # 步骤03 已发现算子集（步骤05 启动调优的起点）
        engine.create_operator_revision(
            "v3-discovered", parent_revision_id=None,
            enabled_ops=["op_a", "op_b", "op_c"],
        )
        engine.context.steps["05_v3_startup_tuning"].status = "failed"
        engine.context.current_step_id = "05_v3_startup_tuning"
        engine._save_context()

        # 新引擎从磁盘加载并续跑
        engine2 = WorkflowEngine(self.tmpdir, executor=make_fake())
        engine2.context.runtime.model_path = "/models/TestModel"
        engine2.long_task_poll_interval = 0
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
