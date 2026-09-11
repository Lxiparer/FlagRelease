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

"""Workflow Engine - 确定性工作流引擎

职责：
1. 管理 15 步工作流状态转换
2. 协调 Artifact Registry、Gate Reducer、Operator Revision Store
3. 恢复中断的工作流
4. 调用领域执行器和 Analysis Agent
"""

import os
import sys
import json
from pathlib import Path
from typing import Dict, List, Optional, Literal, Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging

from ..schemas.context_v2 import (
    ContextSchemaV2,
    RuntimeInfo,
    WorkflowStep,
    OperatorRevision,
    Gate,
)
from ..artifacts.registry import ArtifactRegistry
from ..gates.reducer import GateReducer
from .state_store import YamlStateStore
from .command_executor import CommandExecutor, SubprocessExecutor
# 注：domain 类在 handler 内惰性导入，避免 engine<->domain 顶层循环导入
# （domain 子模块会 import engine.* 子模块，顶层双向引用会因入口顺序触发 partial-init）


# 15 步工作流定义
WORKFLOW_STEPS = [
    ("01_container_preparation", "容器准备"),
    ("02_admission", "环境检测"),
    ("03_v3_discovery_startup", "V3 全组件发现启动"),
    ("04_v3_discovered", "生成 v3-discovered"),
    ("05_v3_startup_tuning", "V3 启动兼容性调优"),
    ("06_v3_accuracy", "V3 精度评测"),
    ("07_v3_accuracy_tuning", "V3 精度算子调优"),
    ("08_v3_performance", "V3 性能测量"),
    ("09_v3_final", "冻结 v3-final"),
    ("10_v3_release", "V3 发布"),
    ("11_v4_reduction", "V4 减算子"),
    ("12_v4_accuracy_check", "V4 精度终检"),
    ("13_v4_release", "V4 发布"),
    ("14_report", "报告汇总"),
    ("15_finalize", "Finalize"),
]


@dataclass
class StepResult:
    """单步执行结果（domain 执行器 / stub handler 的统一返回契约）"""
    status: Literal["success", "failed", "skipped"] = "success"
    output_artifacts: List[str] = field(default_factory=list)
    fail_reason: str = ""
    skip_reason: str = ""


class WorkflowEngine:
    """确定性工作流引擎"""

    def __init__(self, workspace_root: str = "/flagos-workspace",
                 executor: Optional[CommandExecutor] = None,
                 datasets: Optional[List[str]] = None):
        self.workspace_root = Path(workspace_root)
        self.context_file = self.workspace_root / "shared" / "context.yaml"

        # 设置日志
        self.logger = logging.getLogger("workflow.engine")

        # 命令执行后端（引擎注入给 domain 执行器；默认真实 subprocess，测试注入 Fake）
        self.executor = executor or SubprocessExecutor()
        # 评测数据集（默认 gpqa_diamond；每个独立判定，全部达标才 accuracy gate passed）
        self.datasets = datasets or ["gpqa_diamond"]

        # 状态存储后端（引擎是 context 的唯一写入者，见 state_store.py）
        self.state_store = YamlStateStore(self.context_file)

        # 初始化子系统
        self.artifact_registry = ArtifactRegistry(str(self.workspace_root))
        self.gate_reducer = GateReducer(self.artifact_registry)

        # 加载或初始化 context
        self.context = self._load_or_initialize_context()

        # 15 步执行器 dispatch 表（M1a：02/06 已接真实 domain，其余仍 stub）
        self.step_handlers: Dict[str, Callable[[], StepResult]] = self._build_step_handlers()

    def _load_or_initialize_context(self) -> ContextSchemaV2:
        """加载已有 context 或初始化新的（经 StateStore，YAML 后端）"""
        data = self.state_store.load()
        if data is not None:
            return ContextSchemaV2.from_dict(data)

        # 初始化新 context
        ctx = ContextSchemaV2()
        ctx.runtime = RuntimeInfo(
            workflow_run_id=self._generate_run_id(),
            started_at=datetime.now().isoformat(),
        )
        # 初始化 15 个步骤
        for step_id, step_name in WORKFLOW_STEPS:
            ctx.steps[step_id] = WorkflowStep(
                step_id=step_id,
                step_name=step_name,
                status="pending",
            )
        ctx.current_step_id = "01_container_preparation"
        return ctx

    def _generate_run_id(self) -> str:
        """生成 workflow run ID"""
        import hashlib
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        random_suffix = hashlib.md5(str(datetime.now().timestamp()).encode()).hexdigest()[:6]
        return f"wf-{timestamp}-{random_suffix}"

    def _save_context(self):
        """保存 context 到磁盘（经 StateStore，写入前自动校验）"""
        self.state_store.save(self.context.to_dict())

    def get_current_step(self) -> Optional[WorkflowStep]:
        """获取当前步骤"""
        return self.context.steps.get(self.context.current_step_id)

    def transition_to_step(self, step_id: str):
        """转换到指定步骤"""
        if step_id not in self.context.steps:
            raise ValueError(f"Unknown step: {step_id}")

        self.context.current_step_id = step_id
        step = self.context.steps[step_id]

        if step.status == "pending":
            step.status = "running"
            step.started_at = datetime.now().isoformat()

        self._save_context()

    def complete_step(
        self,
        step_id: str,
        status: Literal["success", "failed", "skipped"],
        output_artifacts: List[str] = None,
        fail_reason: str = "",
        skip_reason: str = "",
    ):
        """完成步骤"""
        step = self.context.steps.get(step_id)
        if not step:
            raise ValueError(f"Unknown step: {step_id}")

        step.status = status
        step.finished_at = datetime.now().isoformat()

        # 计算耗时
        if step.started_at:
            start = datetime.fromisoformat(step.started_at)
            end = datetime.fromisoformat(step.finished_at)
            step.duration_seconds = (end - start).total_seconds()

        if output_artifacts:
            step.output_artifacts.extend(output_artifacts)

        if fail_reason:
            step.fail_reason = fail_reason

        if skip_reason:
            step.skip_reason = skip_reason

        self._save_context()

    def get_next_step(self, current_step_id: str) -> Optional[str]:
        """获取下一个步骤"""
        step_ids = [s[0] for s in WORKFLOW_STEPS]
        try:
            current_index = step_ids.index(current_step_id)
            if current_index < len(step_ids) - 1:
                return step_ids[current_index + 1]
        except ValueError:
            pass
        return None

    def detect_recovery_point(self) -> Optional[str]:
        """检测恢复点

        Returns:
            应该恢复的步骤 ID，如果无需恢复则返回 None
        """
        # 检查是否有未完成的步骤
        for step_id, _ in WORKFLOW_STEPS:
            step = self.context.steps.get(step_id)
            if not step:
                continue

            if step.status == "running":
                # 正在运行的步骤 - 检查是否有后台任务
                return step_id
            elif step.status == "failed":
                # 失败的步骤 - 从这里恢复
                return step_id
            elif step.status == "pending":
                # 第一个 pending 步骤
                return step_id

        # 所有步骤都完成了
        return None

    def check_gates(self, required_gates: List[str]) -> bool:
        """检查 Gates 是否都通过

        Args:
            required_gates: Gate IDs

        Returns:
            是否全部通过
        """
        for gate_id in required_gates:
            gate = self.context.gates.get(gate_id)
            if not gate or gate.status != "passed":
                return False
        return True

    def create_operator_revision(
        self,
        revision_id: str,
        parent_revision_id: Optional[str],
        enabled_ops: List[str],
        additional_disabled: Dict[str, str] = None,
        source_artifact: Optional[str] = None,
    ) -> OperatorRevision:
        """创建新的 operator revision（不可变）

        Args:
            revision_id: 版本 ID（v3-discovered / v3-startup-r1 / ...）
            parent_revision_id: 父版本 ID
            enabled_ops: 启用的算子列表
            additional_disabled: 额外禁用的算子 {op_name: reason}
            source_artifact: 来源 Artifact ID

        Returns:
            OperatorRevision 对象
        """
        # 继承父版本的禁用列表
        disabled_ops = {}
        disable_reason_categories = {"startup": [], "accuracy": [], "v4_performance": []}

        if parent_revision_id and parent_revision_id in self.context.operator_revisions:
            parent = self.context.operator_revisions[parent_revision_id]
            disabled_ops = parent.disabled_ops.copy()
            disable_reason_categories = {
                k: v.copy() for k, v in parent.disable_reason_categories.items()
            }

        # 添加新禁用
        if additional_disabled:
            for op_name, reason in additional_disabled.items():
                disabled_ops[op_name] = reason

                # 分类
                if "startup" in reason.lower() or "crash" in reason.lower():
                    disable_reason_categories["startup"].append(op_name)
                elif "accuracy" in reason.lower():
                    disable_reason_categories["accuracy"].append(op_name)
                elif "performance" in reason.lower() or "v4" in reason.lower():
                    disable_reason_categories["v4_performance"].append(op_name)

        # 创建 revision
        revision = OperatorRevision(
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            created_at=datetime.now().isoformat(),
            enabled_ops=enabled_ops,
            disabled_ops=disabled_ops,
            disable_reason_categories=disable_reason_categories,
        )

        if source_artifact:
            from ..schemas.context_v2 import ArtifactReference
            revision.source_artifact = ArtifactReference(
                artifact_id=source_artifact,
                registered_at=datetime.now().isoformat(),
            )

        # 保存到 context
        self.context.operator_revisions[revision_id] = revision
        self.context.current_revision_id = revision_id
        self._save_context()

        return revision

    def freeze_revision(self, revision_id: str):
        """冻结 revision（v3-final / v4-final）"""
        revision = self.context.operator_revisions.get(revision_id)
        if not revision:
            raise ValueError(f"Revision not found: {revision_id}")

        revision.frozen = True
        self._save_context()

    def execute_step(self, step_id: str) -> StepResult:
        """执行单个步骤：置 running → 调 handler → complete_step 落状态"""
        step = self.context.steps.get(step_id)
        if not step:
            raise ValueError(f"Unknown step: {step_id}")

        # 置为 running（重试 failed 步骤时刷新 started_at）
        if step.status != "running":
            step.status = "running"
            step.started_at = datetime.now().isoformat()
            self._save_context()

        handler = self.step_handlers.get(step_id)
        if handler is None:
            result = StepResult(status="failed", fail_reason=f"No handler for step {step_id}")
        else:
            self.logger.info(f"Executing step: {step_id} - {step.step_name}")
            try:
                result = handler()
            except Exception as e:  # domain/stub 抛错 → 记为失败，不崩溃引擎
                self.logger.exception(f"Step {step_id} raised")
                result = StepResult(status="failed", fail_reason=f"{type(e).__name__}: {e}")

        self.complete_step(
            step_id,
            status=result.status,
            output_artifacts=result.output_artifacts,
            fail_reason=result.fail_reason,
            skip_reason=result.skip_reason,
        )
        return result

    def run(self) -> ContextSchemaV2:
        """运行工作流：从恢复点起，确定性驱动 15 步流转直到完成或失败"""
        self.logger.info(f"Starting workflow run: {self.context.runtime.workflow_run_id}")

        # 检测恢复点（首次运行返回 01，中断续跑返回中断步骤）
        recovery_step = self.detect_recovery_point()
        if recovery_step and recovery_step != self.context.current_step_id:
            self.logger.info(f"Recovering from step: {recovery_step}")
            self.context.current_step_id = recovery_step
            self._save_context()

        # 主循环
        while True:
            current_step_id = self.context.current_step_id
            step = self.context.steps.get(current_step_id)

            if step is None:
                self.logger.info("Workflow completed")
                break

            # 已完成 / 跳过 → 前进到下一步
            if step.status in ("success", "skipped"):
                next_step = self.get_next_step(current_step_id)
                if next_step is None:
                    self.logger.info("Workflow completed")
                    break
                self.context.current_step_id = next_step
                self._save_context()
                continue

            # pending / running / failed → 执行
            result = self.execute_step(current_step_id)
            if result.status == "failed":
                self.logger.error(f"Step {current_step_id} failed: {result.fail_reason}")
                break

            next_step = self.get_next_step(current_step_id)
            if next_step is None:
                self.logger.info("Workflow completed")
                break
            self.context.current_step_id = next_step
            self._save_context()

        return self.context

    # ------------------------------------------------------------------
    # 15 步执行器 dispatch 表
    # M0：全部为 stub（引擎↔domain 的接线点）。M1/M2 逐个替换为真实 domain
    # 调用（见 plan §6 工作段 3-9 / dispatch 表）。此处刻意不 import domain，
    # 保证 M0 引擎可脱离容器空跑、全程单测可回归。
    # ------------------------------------------------------------------

    def _build_step_handlers(self) -> Dict[str, Callable[[], StepResult]]:
        return {
            "01_container_preparation": self._stub_step,
            "02_admission": self._step_admission,
            "03_v3_discovery_startup": self._step_v3_discovery,
            "04_v3_discovered": self._stub_freeze_discovered,
            "05_v3_startup_tuning": self._stub_step,
            "06_v3_accuracy": self._step_v3_accuracy,
            "07_v3_accuracy_tuning": self._stub_step,
            "08_v3_performance": self._step_v3_performance,
            "09_v3_final": self._stub_freeze_final,
            "10_v3_release": self._step_v3_release,
            "11_v4_reduction": self._step_v4_reduction,
            "12_v4_accuracy_check": self._step_v4_accuracy_check,
            "13_v4_release": self._step_v4_release,
            "14_report": self._stub_step,
            "15_finalize": self._stub_finalize,
        }

    def _stub_step(self) -> StepResult:
        """M0 通用占位：直接成功。M1/M2 替换为真实 domain 执行器调用。"""
        return StepResult(status="success")

    def _stub_discovery(self) -> StepResult:
        """M0 占位：创建 v3-discovered revision（练习 revision store 接线）。"""
        if "v3-discovered" not in self.context.operator_revisions:
            self.create_operator_revision(
                revision_id="v3-discovered",
                parent_revision_id=None,
                enabled_ops=[],
            )
        return StepResult(status="success")

    def _stub_freeze_discovered(self) -> StepResult:
        """M0 占位：冻结 v3-discovered。"""
        if "v3-discovered" in self.context.operator_revisions:
            self.freeze_revision("v3-discovered")
        return StepResult(status="success")

    def _stub_freeze_final(self) -> StepResult:
        """M0 占位：创建并冻结 v3-final（继承 v3-discovered）。"""
        if "v3-final" not in self.context.operator_revisions:
            parent = "v3-discovered" if "v3-discovered" in self.context.operator_revisions else None
            self.create_operator_revision(
                revision_id="v3-final",
                parent_revision_id=parent,
                enabled_ops=[],
            )
        self.freeze_revision("v3-final")
        return StepResult(status="success")

    def _stub_finalize(self) -> StepResult:
        """M0 占位：标记流程结束时间。"""
        self.context.runtime.finished_at = datetime.now().isoformat()
        return StepResult(status="success")

    # ------------------------------------------------------------------
    # M1a 真实 handler：步骤02 准入、步骤06 精度
    # ------------------------------------------------------------------

    # inspect_env / nv_baseline 容器内路径
    INSPECT_ENV = "/flagos-workspace/scripts/inspect_env.py"
    NV_BASELINE = "/flagos-workspace/shared/nv_baseline.yaml"

    def _step_v3_discovery(self) -> StepResult:
        """步骤03：清缓存→起服务→等就绪→抽 oplist→校验→创建 v3-discovered revision。"""
        container = self.context.runtime.container_name
        if not container:
            return StepResult(status="failed", fail_reason="discovery: container_name 为空")

        from ..domain import V3DiscoveryStartup  # 惰性导入，避免顶层循环
        startup = V3DiscoveryStartup(
            workspace_root=str(self.workspace_root),
            container_name=container,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
        )
        success, err, oplist = startup.start_service_and_discover(
            model_path=self.context.runtime.model_path,
            flaggems_version="",  # identity 仅提示、非阻断
        )
        if not success or not oplist:
            # 起服务/发现失败 → 停在 03（启动调优是步骤05，M1b 尚未接）
            return StepResult(status="failed", fail_reason=f"discovery failed: {err}")

        # 登记 runtime-oplist artifact（provenance）+ 创建 v3-discovered revision
        oplist_art = self.artifact_registry.register_artifact(
            artifact_type="runtime-oplist",
            content={"operators": oplist, "count": len(oplist)},
            file_path="results/operator-configs/v3-discovered-oplist.txt",
            generated_by="script",
            generator_version="v3_discovery_1.0",
            tags={"revision": "v3-discovered"},
        )
        if "v3-discovered" not in self.context.operator_revisions:
            self.create_operator_revision(
                revision_id="v3-discovered",
                parent_revision_id=None,
                enabled_ops=oplist,
                source_artifact=oplist_art,
            )
        return StepResult(status="success", output_artifacts=[oplist_art])

    def _step_admission(self) -> StepResult:
        """步骤02：跑 inspect_env → 映射 capabilities → Plugin-only 准入 → gate + runtime。"""
        container = self.context.runtime.container_name
        if not container:
            return StepResult(status="failed", fail_reason="admission: container_name 为空")

        res = self.executor.docker_exec(container, f"python3 {self.INSPECT_ENV}")
        if not res.ok:
            return StepResult(
                status="failed",
                fail_reason=f"admission: inspect_env exit={res.returncode}: {res.stderr[:300]}",
            )

        try:
            j = json.loads(res.stdout)
        except Exception as e:
            return StepResult(status="failed", fail_reason=f"admission: inspect_env 输出非 JSON: {e}")

        capabilities = self._map_inspect_env_to_capabilities(j)
        from ..domain import PluginOnlyAdmission  # 惰性导入，避免顶层循环
        admission = PluginOnlyAdmission(str(self.workspace_root), self.artifact_registry)
        result = admission.check_admission(capabilities)

        # 回填 runtime + 置准入 gate
        self.context.runtime.entry_image_type = (
            "gems_tree_plugin" if result.admitted else "unknown"
        )
        self.context.gates["admission"] = Gate(
            gate_id="admission",
            status="passed" if result.admitted else "failed",
            criteria="Plugin-only 全组件准入（vllm+flaggems+flagtree+vllm_plugin）",
            evaluated_at=datetime.now().isoformat(),
            reason=result.reason,
        )
        self._save_context()

        if not result.admitted:
            # fail-closed：准入不过 → 步骤失败 → run() 停在 02
            return StepResult(
                status="failed",
                fail_reason=f"admission fail-closed: 缺组件 {result.missing_components}",
            )
        return StepResult(status="success")

    def _step_v3_accuracy(self) -> StepResult:
        """步骤06：真实评测 + accuracy_compare 退出码判定 → 精度 gate（判定权在脚本）。"""
        container = self.context.runtime.container_name
        if not container:
            return StepResult(status="failed", fail_reason="accuracy: container_name 为空")

        revision = self.context.operator_revisions.get(self.context.current_revision_id)
        if revision is None:
            revision = OperatorRevision(revision_id=self.context.current_revision_id or "v3")

        from ..domain import V3AccuracyEvaluation  # 惰性导入，避免顶层循环
        evaluator = V3AccuracyEvaluation(
            workspace_root=str(self.workspace_root),
            container_name=container,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
        )
        all_qualified, results = evaluator.evaluate_accuracy(
            candidate="v3",
            revision=revision,
            datasets=self.datasets,
            reference_model=self.context.runtime.model_name,
            nv_baseline_file=self.NV_BASELINE,
        )

        # 逐数据集登记精度 artifact（含 reducer 所需 nv_reference_value/relative_drop/qualified）
        for dataset, result in results.items():
            file_path = f"results/accuracy_{dataset}_v3.json"
            evaluator.register_accuracy_artifact("v3", dataset, result, file_path)

        # 精度 gate：判定直接来自 accuracy_compare 聚合退出码（不走 reducer 重判）
        self.context.gates["accuracy.v3.qualified"] = Gate(
            gate_id="accuracy.v3.qualified",
            status="passed" if all_qualified else "failed",
            criteria="所有数据集 accuracy_compare 退出码=0（相对退化≤5%，含小样本噪声容忍）",
            evaluated_at=datetime.now().isoformat(),
            reason="; ".join(
                f"{d}:exit={r.get('exit_code')}" for d, r in results.items()
            ),
        )
        self._save_context()

        # 精度不达标不停流程（调优是步骤07）；步骤本身算成功（评测+判定+落 gate 完成）
        return StepResult(status="success")

    def _step_v3_performance(self) -> StepResult:
        """步骤08：V3 性能纯测量（跑 benchmark→登记 artifact，无 gate、不阻断）。"""
        container = self.context.runtime.container_name
        if not container:
            return StepResult(status="failed", fail_reason="performance: container_name 为空")

        revision = self.context.operator_revisions.get(self.context.current_revision_id)
        if revision is None:
            revision = OperatorRevision(revision_id=self.context.current_revision_id or "v3")

        from ..domain import V3PerformanceMeasurement  # 惰性导入，避免顶层循环
        measurer = V3PerformanceMeasurement(
            workspace_root=str(self.workspace_root),
            container_name=container,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
        )
        success, perf = measurer.measure_performance("v3", revision, mode="quick")

        # 纯测量、无 gate：性能不阻断流程（plan §7）。测量失败仅告警，步骤仍成功。
        artifacts = [perf["artifact_id"]] if success and perf.get("artifact_id") else []
        if not success:
            self.logger.warning("V3 性能测量未产出有效结果（非阻断）")
        return StepResult(status="success", output_artifacts=artifacts)

    def _step_v3_release(self) -> StepResult:
        """步骤10：V3 发布——引擎按 gate 真相定发布范围，domain 执行 docker commit/push。"""
        container = self.context.runtime.container_name
        if not container:
            return StepResult(status="failed", fail_reason="release: container_name 为空")

        # 发布范围决策由引擎持有（不让 domain 重判 gate）
        acc_gate = self.context.gates.get("accuracy.v3.qualified")
        accuracy_passed = bool(acc_gate and acc_gate.status == "passed")
        final_rev = self.context.operator_revisions.get("v3-final")
        established_passed = bool(final_rev and final_rev.frozen)
        if final_rev is None:
            return StepResult(status="failed", fail_reason="release: v3-final revision 缺失")

        from ..domain import V3ReleaseManager  # 惰性导入，避免顶层循环
        manager = V3ReleaseManager(
            workspace_root=str(self.workspace_root),
            container_name=container,
            model_name=self.context.runtime.model_name,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
        )
        success, report = manager.release_v3(final_rev, accuracy_passed, established_passed)
        if not success:
            # 打包/上传失败是真实失败（阻断）——与"精度不达标不阻断"不同
            return StepResult(status="failed", fail_reason=f"release failed: {report.get('error')}")

        # 登记发布决策 artifact
        art = self.artifact_registry.register_artifact(
            artifact_type="release-decision",
            content=report,
            file_path="results/v3_release_record.json",
            generated_by="script",
            generator_version="v3_release_1.0",
            tags={"version": "v3", "scope": report.get("release_scope", "")},
        )
        return StepResult(status="success", output_artifacts=[art])

    # ------------------------------------------------------------------
    # M1b 真实 handler：步骤11/12/13 V4 减算子竖片
    # 步骤11 = 性能搜索（阶段1）→ 候选；步骤12 = 精度回溯（阶段2）+ 终检 → v4-final；
    # 步骤13 = 条件发布（未成立则回退 V3，步骤 skipped 且原因取自 v4.established Gate）。
    # 跨步通道只有 artifact：候选（可序列化）经 results/v4-search-result.json 传递。
    # ------------------------------------------------------------------

    def _step_v4_reduction(self) -> StepResult:
        """步骤11：V4 性能搜索（阶段1，不测精度）→ v4-search-result artifact。"""
        container = self.context.runtime.container_name
        v3_final = self.context.operator_revisions.get("v3-final")
        if v3_final is None or not v3_final.frozen:
            self._set_v4_established_gate(False, "v3-final 未建立（冻结），V4 不成立")
            return StepResult(
                status="skipped",
                skip_reason="v4: v3-final 未建立（冻结），跳过 V4 减算子",
            )

        # v4-r0 = v3-final 的不可变克隆（搜索起点 + 性能基线，plan §8.1）
        v4_r0 = self._ensure_revision(
            "v4-r0", parent_revision_id="v3-final", enabled_ops=list(v3_final.enabled_ops),
        )

        from ..domain import V4OperatorReduction  # 惰性导入，避免顶层循环
        reduction = V4OperatorReduction(
            workspace_root=str(self.workspace_root),
            container_name=container,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
            revision_factory=self._v4_revision_factory,
            reference_model=self.context.runtime.model_name,
            nv_baseline_file=self.NV_BASELINE,
        )
        report = reduction.performance_search(v4_r0)
        report["phase"] = "performance_search"
        report["_meta"] = {
            "candidates": "按吞吐降序的候选组合（阶段2 精度回溯的输入）",
            "reason": "no_valid_improvement = 无合法性能提升 → V4 不成立",
        }

        art = self._register_json_artifact(
            "v4-search-result", "results/v4-search-result.json", report,
            "v4_reduction_1.0", tags={"phase": "performance_search"},
        )

        if not report["candidates"]:
            self._set_v4_established_gate(False, f"V4 无合法性能提升（{report['reason']}）")
            return StepResult(
                status="skipped",
                skip_reason=f"V4 无合法性能提升（{report['reason']}）→ 回退 V3",
                output_artifacts=[art],
            )
        return StepResult(status="success", output_artifacts=[art])

    def _step_v4_accuracy_check(self) -> StepResult:
        """步骤12：V4 精度回溯（阶段2）+ 选中候选全数据集终检 → v4-final 或回退。"""
        container = self.context.runtime.container_name
        v3_final = self.context.operator_revisions.get("v3-final")
        if v3_final is None or not v3_final.frozen:
            self._set_v4_established_gate(False, "v3-final 未建立（冻结），V4 不成立")
            return StepResult(status="skipped", skip_reason="v4: v3-final 未建立，跳过 V4 精度回溯")

        search_art = self.artifact_registry.get_latest_artifact("v4-search-result")
        search = self.artifact_registry.load_artifact_content(search_art) if search_art else None
        candidates = (search or {}).get("candidates", [])
        if not candidates:
            reason = (search or {}).get("reason", "no_search_evidence")
            self._set_v4_established_gate(False, f"V4 无候选（{reason}）")
            return StepResult(
                status="skipped",
                skip_reason=f"V4 无候选可回溯（{reason}）→ 回退 V3",
            )

        v4_r0 = self.context.operator_revisions.get("v4-r0")
        if v4_r0 is None:
            self._set_v4_established_gate(False, "v4-r0 缺失")
            return StepResult(status="skipped", skip_reason="V4 搜索起点 v4-r0 缺失 → 回退 V3")

        from ..domain import V4OperatorReduction  # 惰性导入，避免顶层循环
        reduction = V4OperatorReduction(
            workspace_root=str(self.workspace_root),
            container_name=container,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
            revision_factory=self._v4_revision_factory,
            reference_model=self.context.runtime.model_name,
            nv_baseline_file=self.NV_BASELINE,
        )
        selected, report = reduction.accuracy_backtrack(v4_r0, candidates, self.datasets)

        art = self._register_json_artifact(
            "v4-optimization-report", "results/v4-optimization-report.json", report,
            "v4_reduction_1.0", tags={"phase": "accuracy_backtrack"},
        )

        if selected is None:
            # 候选全部精度不达标 → V4 不成立（流程不回退，步骤13 走 fallback 记录）
            reason = report.get("reason", "accuracy_not_met")
            self._set_v4_accuracy_gate(report.get("last_accuracy_results", {}), passed=False)
            self._set_v4_established_gate(False, f"候选全部精度不达标（{reason}）")
            return StepResult(
                status="skipped",
                skip_reason=f"V4 不成立：候选全部精度不达标 → 回退 V3",
                output_artifacts=[art],
            )

        # V4 成立：终检结果落精度 gate（判定来自 accuracy_compare 退出码）
        self._set_v4_accuracy_gate(report.get("accuracy_results", {}), passed=True)

        # 建立 v4-final：克隆选中候选并冻结（冻结 = V4 establishment 成立的唯一标志）
        self._ensure_revision(
            "v4-final", parent_revision_id=selected.revision_id,
            enabled_ops=list(selected.enabled_ops),
        )
        self.freeze_revision("v4-final")

        reason = (
            f"performance improved (v4_throughput={report.get('v4_throughput', 0.0):.1f} > "
            f"v3 baseline), accuracy qualified, ops={report.get('v4_operator_count', 0)} >= 1"
        )
        self._set_v4_established_gate(True, reason)
        return StepResult(status="success", output_artifacts=[art])

    def _step_v4_release(self) -> StepResult:
        """步骤13：V4 条件发布——成立则打包上传，不成立则落回退记录（步骤 skipped）。"""
        container = self.context.runtime.container_name
        if not container:
            return StepResult(status="failed", fail_reason="v4 release: container_name 为空")

        gate = self.context.gates.get("v4.established")
        established = bool(gate and gate.status == "passed")
        v4_final = self.context.operator_revisions.get("v4-final") if established else None
        if established and (v4_final is None or not v4_final.frozen):
            # gate 说成立但证据链不完整 → fail-closed
            return StepResult(
                status="failed",
                fail_reason="v4 release: v4.established 通过但 v4-final 缺失/未冻结",
            )

        # 优化报告（步骤11/12 已落 artifact）——回退记录也要引用它说明原因
        report_art = self.artifact_registry.get_latest_artifact("v4-optimization-report")
        optimization_report = {}
        if report_art:
            optimization_report = self.artifact_registry.load_artifact_content(report_art) or {}
        if not optimization_report:
            search_art = self.artifact_registry.get_latest_artifact("v4-search-result")
            optimization_report = (
                self.artifact_registry.load_artifact_content(search_art) or {} if search_art else {}
            )

        from ..domain import V4ReleaseManager  # 惰性导入，避免顶层循环
        manager = V4ReleaseManager(
            workspace_root=str(self.workspace_root),
            container_name=container,
            model_name=self.context.runtime.model_name,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
        )
        success, rel = manager.release_v4(
            v4_final, optimization_report, established_passed=established,
        )
        if not success:
            # 打包/上传失败是真实失败（阻断）——与"V4 未成立回退"不同
            return StepResult(status="failed", fail_reason=f"v4 release failed: {rel.get('error')}")

        rel_path = (
            "results/v4_release_record.json" if established else "results/v4_fallback_record.json"
        )
        art = self._register_json_artifact(
            "v4-release-decision", rel_path, rel, "v4_release_1.0",
            tags={"version": "v4", "established": str(established)},
            write=False,  # domain 已落盘
        )

        if not established:
            skip_reason = gate.reason if gate else "V4 未成立 → 回退 V3"
            return StepResult(status="skipped", skip_reason=skip_reason, output_artifacts=[art])
        return StepResult(status="success", output_artifacts=[art])

    def _ensure_revision(
        self,
        revision_id: str,
        parent_revision_id: Optional[str],
        enabled_ops: List[str],
        additional_disabled: Optional[Dict[str, str]] = None,
    ) -> OperatorRevision:
        """取回已存在的 revision，否则创建（幂等——跨步/跨进程重建同一 revision）"""
        existing = self.context.operator_revisions.get(revision_id)
        if existing is not None:
            return existing
        return self.create_operator_revision(
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            enabled_ops=enabled_ops,
            additional_disabled=additional_disabled,
        )

    def _v4_revision_factory(
        self,
        revision_id: str,
        parent_revision_id: Optional[str],
        enabled_ops: List[str],
        disabled_ops_map: Dict[str, str],
        note: str,
    ) -> OperatorRevision:
        """注入给 V4OperatorReduction 的 revision 工厂（Engine 是 context 唯一写入者）"""
        return self._ensure_revision(
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            enabled_ops=enabled_ops,
            additional_disabled=dict(disabled_ops_map) or None,
        )

    def _register_json_artifact(
        self,
        artifact_type: str,
        rel_path: str,
        content: Dict,
        generator_version: str,
        tags: Optional[Dict[str, str]] = None,
        write: bool = True,
    ) -> str:
        """登记 JSON artifact（默认同时把 content 落盘，供跨步/跨进程读取）"""
        if write:
            full = self.workspace_root / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                json.dump(content, f, indent=2, ensure_ascii=False)
        return self.artifact_registry.register_artifact(
            artifact_type=artifact_type,
            content=content,
            file_path=rel_path,
            generated_by="script",
            generator_version=generator_version,
            tags=tags or {},
        )

    def _set_v4_established_gate(self, passed: bool, reason: str):
        """置 V4 establishment gate（Engine 持有判定，domain 不重判）"""
        self.context.gates["v4.established"] = Gate(
            gate_id="v4.established",
            status="passed" if passed else "failed",
            criteria=(
                "search_execution_success AND performance_improved_over_v3 AND "
                "accuracy_qualified_against_external_nv AND retained_operator_count >= 1"
            ),
            evaluated_at=datetime.now().isoformat(),
            reason=reason,
        )
        self._save_context()

    def _set_v4_accuracy_gate(self, results: Dict[str, Dict], passed: bool):
        """置 V4 精度 gate（判定来自 accuracy_compare 退出码，不内联重算）"""
        self.context.gates["accuracy.v4.qualified"] = Gate(
            gate_id="accuracy.v4.qualified",
            status="passed" if passed else "failed",
            criteria="所有数据集 accuracy_compare 退出码=0（相对退化≤5%，含小样本噪声容忍）",
            evaluated_at=datetime.now().isoformat(),
            reason="; ".join(
                f"{d}:exit={r.get('exit_code')}" for d, r in (results or {}).items()
            ) or "no candidate result",
        )
        self._save_context()

    @staticmethod
    def _map_inspect_env_to_capabilities(j: Dict) -> Dict:
        """把 inspect_env.py 的 JSON 输出映射成 PluginOnlyAdmission.check_admission 所需 capabilities。"""
        return {
            "flaggems_installed": j.get("flaggems_installed", False),
            "flaggems_version": j.get("flaggems_version", ""),
            "vllm_plugin_installed": j.get("vllm_plugin_installed", False),
            "plugin_version": j.get("plugin_version", ""),
            "vllm_version": j.get("vllm_version", ""),
            "flagtree": j.get("flagtree", {}),
        }
