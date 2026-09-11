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
import re
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
from .command_executor import CommandExecutor, SubprocessExecutor, parse_json_output
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
    # 降级路由：本步失败/终止后需要标记为 skipped 的后续步骤（如启动不可恢复时跳过 06-09）
    skip_steps: List[str] = field(default_factory=list)
    # 失败但按既有约定继续（**只用于"允许降级"的场景**；基础设施故障仍应停流程）
    continue_on_failure: bool = False


class WorkflowEngine:
    """确定性工作流引擎"""

    def __init__(self, workspace_root: str = "/flagos-workspace",
                 executor: Optional[CommandExecutor] = None,
                 datasets: Optional[List[str]] = None,
                 state_file: Optional[str] = None,
                 artifacts_root: Optional[str] = None,
                 proxy: str = ""):
        self.workspace_root = Path(workspace_root)
        # 引擎状态与 Artifact 台账的缺省落点：`<workspace>/config/engine/`。
        #
        # 两个硬约束决定了不能放 `shared/`：
        # 1. 与 legacy `shared/context.yaml` 隔离——旧 schema 的消费者（run_batch.sh /
        #    generate_report.py / update_context.py / release tools）还在读那个文件，
        #    共用会被引擎覆写成 v2 schema 而写坏；
        # 2. **每轮归档目录**——宿主编排每轮开头把 `results/traces/logs/config/reports/eval`
        #    整体 mv 进 `archive/<ts>/`，但**不含 `shared/`**。放在 shared/ 会让第二轮
        #    直接加载上一轮的已完成状态 → run() 无步可走、静默"成功"。
        # `config/engine/` 每轮被归档 → 天然干净起步。
        self.engine_dir = self.workspace_root / "config" / "engine"
        self.context_file = (
            Path(state_file) if state_file else self.engine_dir / "context.yaml"
        )
        self.artifacts_root = (
            Path(artifacts_root) if artifacts_root else self.engine_dir / "artifacts"
        )

        # 设置日志
        self.logger = logging.getLogger("workflow.engine")

        # 命令执行后端（引擎注入给 domain 执行器；默认真实 subprocess，测试注入 Fake）
        self.executor = executor or SubprocessExecutor()
        # 评测数据集（默认 gpqa_diamond；每个独立判定，全部达标才 accuracy gate passed）
        self.datasets = datasets or ["gpqa_diamond"]

        # 状态存储后端（引擎是 context 的唯一写入者，见 state_store.py）
        self.state_store = YamlStateStore(self.context_file)

        # 启动调优的服务就绪探测预算（步骤05；测试可覆写为 0 以只探一次）
        self.startup_tuning_timeout = 300
        self.startup_tuning_poll_interval = 5.0

        # 起服务参数缓存（本轮内只探测一次 GPU / 派生一次 TP；约束14：卡数/TP 全程不变）
        self._service_params = None

        # 长任务（评测）轮询间隔（秒）。评测走 detached + state 轮询，
        # 引擎不是会被随时杀掉的会话，故比长任务协议的 8 分钟更密，尽早发现静默死亡。
        self.long_task_poll_interval = 60.0

        # V4 随机子集搜索（步骤11）：每轮随机只开 1~3 个算子，只测 2 轮，
        # 两轮都无性能提升 → 不产出 V4，回退 V3（对齐 CLAUDE.md 既有流程与
        # operator_reduction.py）。随机必须带种子，引擎才能可复现。
        self.v4_max_rounds = 2
        self.v4_seed = 0

        # 初始化子系统（台账落点与业务产物根分离，见上面 artifacts_root 说明）
        self.artifact_registry = ArtifactRegistry(
            str(self.workspace_root), registry_root=str(self.artifacts_root),
        )
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
        set_current: bool = True,
    ) -> OperatorRevision:
        """创建新的 operator revision（不可变）

        Args:
            revision_id: 版本 ID（v3-discovered / v3-startup-r1 / ...）
            parent_revision_id: 父版本 ID
            enabled_ops: 启用的算子列表
            additional_disabled: 额外禁用的算子 {op_name: reason}
            source_artifact: 来源 Artifact ID
            set_current: 是否把 current_revision_id 推进到新 revision。
                派生**候选**（调优试探/减算子搜索）应传 False——候选未经实测
                验证前不得成为当前 revision，指针由 handler 显式推进。

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
        if set_current:
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

            # 降级路由：把依赖本步的后续步骤标记为 skipped（写清原因，不做静默跳过）
            if result.skip_steps:
                self._skip_steps(result.skip_steps, current_step_id,
                                 result.skip_reason or result.fail_reason)

            if result.status == "failed":
                if result.continue_on_failure:
                    # 既定例外：如"启动无可归因算子 → service_ok=false → 直奔私有发布"
                    self.logger.warning(
                        f"Step {current_step_id} failed but flow continues by design: "
                        f"{result.fail_reason}"
                    )
                else:
                    self.logger.error(f"Step {current_step_id} failed: {result.fail_reason}")
                    break

            next_step = self.get_next_step(current_step_id)
            if next_step is None:
                self.logger.info("Workflow completed")
                break
            self.context.current_step_id = next_step
            self._save_context()

        # 终态收尾：在**所有步骤状态落定之后**写快照并重刷报告
        self._finalize_run()
        return self.context

    def _skip_steps(self, step_ids: List[str], from_step: str, reason: str):
        """把指定后续步骤标记为 skipped（降级路由用；原因必须写明）"""
        for sid in step_ids:
            step = self.context.steps.get(sid)
            if step is None or step.status in ("success", "skipped"):
                continue
            step.status = "skipped"
            step.finished_at = datetime.now().isoformat()
            step.skip_reason = f"因 {from_step} 降级跳过：{reason[:200]}"
            self.logger.warning(f"降级跳过 {sid}：{reason[:120]}")
        self._save_context()

    def _record_issue(self, category: str, summary: str, detail: str = "",
                      action: str = "", result: str = "",
                      extra_tool_args: Optional[Dict[str, str]] = None):
        """记录 issue（确定性故障事实 → logs/issues_*.log + 既有 issue_reporter）"""
        try:
            from ..domain.issue_writer import write_issue
            write_issue(
                str(self.workspace_root), category, summary,
                detail=detail, action=action, result=result,
                executor=self.executor, container=self.context.runtime.container_name,
                model_name=self.context.runtime.model_name,
                extra_tool_args=extra_tool_args, logger=self.logger,
            )
        except Exception as e:  # 记录 issue 失败不能影响主流程
            self.logger.warning(f"写 issue 失败（非阻断）：{type(e).__name__}: {e}")

    def _finalize_run(self):
        """终态收尾：写 context_final.yaml + 重刷报告（best-effort，失败不阻断）

        - 快照/报告必须在**所有步骤状态落定之后**写，否则 finished_at 恒空、
          步骤15 恒为 running（报告早前只在步骤14 生成，就踩了这个）。
        - **失败路径也要产出报告**：run() 因某步 failed 而 break 时步骤14 根本不会执行，
          早前的结果是"跑失败 → 零产出"，排障时只能看退出码。
        """
        from ..report import write_context_final, write_report
        for name, fn in (
            ("context_final", lambda: write_context_final(self.workspace_root, self.context)),
            ("report", lambda: write_report(self.workspace_root, self.context, self.artifact_registry)),
        ):
            try:
                fn()
            except Exception as e:
                self.logger.warning(f"{name} 收尾写入失败（非阻断）：{type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    # 15 步执行器 dispatch 表
    # M0：全部为 stub（引擎↔domain 的接线点）。M1/M2 逐个替换为真实 domain
    # 调用（见 plan §6 工作段 3-9 / dispatch 表）。此处刻意不 import domain，
    # 保证 M0 引擎可脱离容器空跑、全程单测可回归。
    # ------------------------------------------------------------------

    def _build_step_handlers(self) -> Dict[str, Callable[[], StepResult]]:
        return {
            "01_container_preparation": self._step_container_preparation,
            "02_admission": self._step_admission,
            "03_v3_discovery_startup": self._step_v3_discovery,
            "04_v3_discovered": self._stub_freeze_discovered,
            "05_v3_startup_tuning": self._step_v3_startup_tuning,
            "06_v3_accuracy": self._step_v3_accuracy,
            "07_v3_accuracy_tuning": self._step_v3_accuracy_tuning,
            "08_v3_performance": self._step_v3_performance,
            "09_v3_final": self._step_v3_final,
            "10_v3_release": self._step_v3_release,
            "11_v4_reduction": self._step_v4_reduction,
            "12_v4_accuracy_check": self._step_v4_accuracy_check,
            "13_v4_release": self._step_v4_release,
            "14_report": self._step_report,
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

    def _step_v3_final(self) -> StepResult:
        """步骤09：冻结 v3-final（继承当前达标 revision 的算子集 + 累计禁用清单）。

        注意：v3-final 的算子集必须来自调优链的终点（v3-startup-stable / v3-accuracy-rN），
        不能凭空构造——否则发布出去的是"全量开启"而非调优后的集合。
        """
        current_id = self.context.current_revision_id
        current = self.context.operator_revisions.get(current_id)
        if current is None:
            return StepResult(
                status="failed", fail_reason=f"v3-final: 当前 revision {current_id} 缺失",
            )

        self._ensure_revision(
            "v3-final", parent_revision_id=current_id, enabled_ops=list(current.enabled_ops),
        )
        self.freeze_revision("v3-final")
        return StepResult(status="success")

    def _stub_finalize(self) -> StepResult:
        """步骤15：标记流程结束时间并回传最终状态快照。

        `context_final.yaml` 在步骤14 已写过一次（报告口径），这里在写入 finished_at 后
        再刷一次，保证快照是真正的"终态"（plan §9 / 工作段10.10）。
        """
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

        # 起服务参数：GPU 空闲探测 → TP 派生 → 锁定卡数（约束14/16/17）
        params, perr = self._prepare_service_params()
        if params is None:
            return StepResult(status="failed", fail_reason=f"起服务参数派生失败：{perr}")

        from ..domain import V3DiscoveryStartup  # 惰性导入，避免顶层循环
        startup = V3DiscoveryStartup(
            workspace_root=str(self.workspace_root),
            container_name=container,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
            poll_interval=self.long_task_poll_interval,
        )
        success, err, oplist = startup.start_service_and_discover(
            model_path=self.context.runtime.model_path,
            flaggems_version="",  # identity 仅提示、非阻断
            params=params,
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

    def _step_container_preparation(self) -> StepResult:
        """步骤01：前置校验（fail-closed）——容器与工作区必须已就绪。

        容器创建（docker run / 模型权重 / 挂载 / setup_workspace.sh）是宿主侧编排职责
        （见 plan 已确认边界与 M4a 决策），引擎只校验不创建：缺什么就明确报什么并停在 01，
        不做任何"顺带创建"的隐式行为。
        """
        problems: List[str] = []

        # 1. 工作区目录结构（setup_workspace.sh 产出）
        for sub in ("shared", "results", "logs"):
            if not (self.workspace_root / sub).is_dir():
                problems.append(f"工作区缺少 {sub}/ 目录")
        if not os.access(self.workspace_root, os.W_OK):
            problems.append(f"工作区不可写：{self.workspace_root}")

        # 2. 容器可达
        container = self.context.runtime.container_name
        if not container:
            problems.append("runtime.container_name 为空")
        else:
            res = self.executor.run(["docker", "inspect", "--type=container", container])
            if not res.ok:
                problems.append(f"容器不可达（docker inspect 失败）：{container}")

        # 3. 模型权重路径
        if not self.context.runtime.model_path:
            problems.append("runtime.model_path 为空")

        # 4. 挂载一致性（证据，不阻断）：引擎在宿主侧落盘 results/state，
        #    容器内的 domain 命令按 /flagos-workspace 读写；两者必须是同一目录，
        #    否则"引擎写的产物容器看不见"。mounted/symlink 都算一致，internal 不一致。
        mount = self._inspect_workspace_mount(container) if container else None

        if problems:
            self.logger.error(f"前置校验未通过：{problems}")
            return StepResult(
                status="failed",
                fail_reason="前置校验未通过：" + "；".join(problems),
            )

        # 校验通过 → 落证据（报告/审计可引用）
        art = self._register_json_artifact(
            "precondition-check", "results/preconditions.json",
            {
                "workspace_root": str(self.workspace_root),
                "container_name": container,
                "model_name": self.context.runtime.model_name,
                "model_path": self.context.runtime.model_path,
                "checked_dirs": ["shared", "results", "logs"],
                "container_reachable": True,
                "workspace_mount": mount or {"found": False},
                "mount_consistent": (mount or {}).get("consistent"),
                "_meta": {
                    "note": "容器与工作区由宿主侧编排准备，引擎只校验（M4a 已确认边界）",
                    "mount": "宿主 workspace 与容器 /flagos-workspace 的挂载关系；"
                             "不一致时引擎在宿主落盘的产物容器内不可见（记录供排障）",
                },
            },
            "container_preparation_1.0", tags={"check": "precondition"},
        )
        return StepResult(status="success", output_artifacts=[art])

    def _inspect_workspace_mount(self, container: str) -> Optional[Dict]:
        """查容器 /flagos-workspace 的挂载来源，判断与宿主 workspace 是否同一目录"""
        res = self.executor.run(
            ["docker", "inspect", "-f", "{{json .Mounts}}", container], timeout=60,
        )
        if not res.ok:
            return None
        try:
            mounts = json.loads(res.stdout or "[]")
        except (json.JSONDecodeError, TypeError):
            return None

        target = str(self.workspace_root)
        for m in mounts if isinstance(mounts, list) else []:
            if m.get("Destination") != "/flagos-workspace":
                continue
            source = m.get("Source", "")
            consistent = os.path.realpath(source) == os.path.realpath(target) if source else False
            if not consistent:
                self.logger.warning(
                    f"挂载可能不一致：容器 /flagos-workspace ← {source}，"
                    f"引擎 workspace={target}（symlink 模式下可能仍指向同一目录）"
                )
            return {"found": True, "source": source, "destination": "/flagos-workspace",
                    "type": m.get("Type", ""), "consistent": consistent}
        self.logger.warning("容器未报告 /flagos-workspace 挂载")
        return {"found": False, "consistent": None}

    def _step_v3_startup_tuning(self) -> StepResult:
        """步骤05：启动兼容性调优（确定性诊断优先）→ 冻结 v3-startup-stable。"""
        container = self.context.runtime.container_name
        if not container:
            return StepResult(status="failed", fail_reason="startup tuning: container_name 为空")

        discovered = self.context.operator_revisions.get("v3-discovered")
        if discovered is None:
            return StepResult(status="failed", fail_reason="startup tuning: v3-discovered 缺失")

        tuning = self._make_startup_tuning()
        stable, final_rev, report = tuning.tune_startup_compatibility(discovered, max_rounds=5)

        art = self._register_json_artifact(
            "startup-tuning-result", "results/startup-tuning-result.json", report,
            "v3_startup_tuning_1.0", tags={"reason": report.get("reason", "")},
        )
        if not stable:
            # 约束18 例外：诊断穷尽、无可归因算子 → service_ok=false →
            # **跳过 06-09 直奔私有发布**（不切 native、不硬测）。若还有可归因算子，
            # 上面的循环会继续禁用重试（不限轮次），到这里就意味着真的没辙了。
            self.context.gates["service.available"] = Gate(
                gate_id="service.available", status="failed",
                criteria="V3 全组件服务可启动并稳定",
                evaluated_at=datetime.now().isoformat(),
                reason=f"启动调优未收敛：{report.get('reason')}",
            )
            reason = report.get("reason", "")
            degradable = reason in ("diagnosis_exhausted_agent_unavailable",
                                    "max_rounds_exhausted")
            # 启动崩溃/不可恢复必须留痕（否则引擎模式下这类事件完全没有出口）
            self._record_issue(
                "startup", f"V3 服务启动未收敛（{reason}）",
                detail=f"revision={final_rev.revision_id}，"
                       f"轮次={report.get('rounds')}，禁用={report.get('disabled_by_round')}",
                action="确定性诊断（diagnose_ops.py）+ 禁用问题算子重试",
                result="未收敛" + ("（无可归因算子 → 降级私有发布）" if degradable else ""),
            )
            return StepResult(
                status="failed",
                fail_reason=f"startup tuning failed: {reason}",
                output_artifacts=[art],
                skip_steps=([] if not degradable else
                            ["06_v3_accuracy", "07_v3_accuracy_tuning",
                             "08_v3_performance", "09_v3_final"]),
                continue_on_failure=degradable,
                skip_reason=f"服务不可用（{reason}）→ 跳过评测与调优，直接私有发布",
            )

        # 冻结启动稳定集合（后续精度/性能调优的起点）
        self._ensure_revision(
            "v3-startup-stable", parent_revision_id=final_rev.revision_id,
            enabled_ops=list(final_rev.enabled_ops),
        )
        self.freeze_revision("v3-startup-stable")
        self.context.current_revision_id = "v3-startup-stable"
        self._save_context()
        return StepResult(status="success", output_artifacts=[art])

    def _step_admission(self) -> StepResult:
        """步骤02：跑 inspect_env → 映射 capabilities → Plugin-only 准入 → gate + runtime。"""
        container = self.context.runtime.container_name
        if not container:
            return StepResult(status="failed", fail_reason="admission: container_name 为空")

        # `--output-json` 是必须的：默认输出是人类可读报告；且该工具的 stdout 前会混有
        # vLLM plugin INFO / warning 等日志行，必须容错解析（不能 json.loads 整体）
        script = f"python3 {self.INSPECT_ENV} --output-json"
        model_path = self.context.runtime.model_path
        if model_path:
            script += f" --model-path {model_path}"
        res = self.executor.docker_exec(container, script)
        if not res.ok:
            return StepResult(
                status="failed",
                fail_reason=f"admission: inspect_env exit={res.returncode}: {res.stderr[:300]}",
            )

        j = parse_json_output(res.stdout)
        if not isinstance(j, dict):
            return StepResult(
                status="failed",
                fail_reason="admission: inspect_env 输出不含可解析 JSON"
                            f"（stdout 前 200 字：{(res.stdout or '')[:200]!r}）",
            )

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
            poll_interval=self.long_task_poll_interval,
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
            # 指向**本次评测的快照证据文件**（真实存在、且不会被下一个候选覆盖）
            file_path = (result.get("details") or {}).get(
                "evidence_file", f"results/{dataset}_flagos_optimized.json")
            evaluator.register_accuracy_artifact("v3", dataset, result, file_path)

        # 精度 gate：判定直接来自 accuracy_compare 聚合退出码（不走 reducer 重判）。
        # 三态：passed / failed（真实退化，exit=1）/ **unresolved（无法评估，exit=2/3 或评测没跑成）**
        # ——工具坏掉或缺 NV 基线不能伪装成"模型精度退化"。
        all_assessed = all(r.get("assessed", True) for r in results.values())
        gate_status = "passed" if all_qualified else ("failed" if all_assessed else "unresolved")
        self.context.gates["accuracy.v3.qualified"] = Gate(
            gate_id="accuracy.v3.qualified",
            status=gate_status,
            criteria="所有数据集 accuracy_compare 退出码=0（相对退化≤5%，含小样本噪声容忍）",
            evaluated_at=datetime.now().isoformat(),
            reason="; ".join(
                self._dataset_verdict_text(d, r) for d, r in results.items()
            ),
        )
        self._save_context()

        if not all_qualified:
            self._record_issue(
                "accuracy",
                "V3 精度未达标（accuracy_compare 判定）" if all_assessed
                else "V3 精度无法评估（工具错误或缺 NV 基线）",
                detail="; ".join(self._dataset_verdict_text(d, r) for d, r in results.items()),
                action="交由步骤07 确定性分组调优" if all_assessed else "需修复评测工具/补 NV 基线",
                result="Gate=" + gate_status,
            )

        # 精度不达标不停流程（调优是步骤07）；步骤本身算成功（评测+判定+落 gate 完成）
        return StepResult(status="success")

    def _step_v3_accuracy_tuning(self) -> StepResult:
        """步骤07：精度不达标时的确定性分组调优（最多3轮）→ 达标集合回写 current_revision。"""
        container = self.context.runtime.container_name
        if not container:
            return StepResult(status="failed", fail_reason="accuracy tuning: container_name 为空")

        # 只对"真实不达标"做调优：
        # - passed           → 无需调优
        # - unresolved       → 评测本身没跑成/缺 NV 基线，调优没有意义（会白烧几小时 GPU），
        #                      应当修工具或补基线后重跑，而不是当作精度问题去关算子
        acc_gate = self.context.gates.get("accuracy.v3.qualified")
        if acc_gate is not None and acc_gate.status == "passed":
            return StepResult(
                status="skipped",
                skip_reason="V3 精度已达标（accuracy.v3.qualified passed），无需调优",
            )
        if acc_gate is not None and acc_gate.status == "unresolved":
            return StepResult(
                status="skipped",
                skip_reason=f"精度无法评估（{acc_gate.reason[:80]}）——属工具/基线问题，"
                            f"不按精度退化做算子调优",
            )

        revision = self.context.operator_revisions.get(self.context.current_revision_id)
        if revision is None:
            return StepResult(
                status="failed",
                fail_reason=f"accuracy tuning: revision {self.context.current_revision_id} 缺失",
            )

        from ..domain import V3AccuracyTuning  # 惰性导入，避免顶层循环
        startup = self._make_startup_tuning()
        tuning = V3AccuracyTuning(
            workspace_root=str(self.workspace_root),
            container_name=container,
            workflow_run_id=self.context.runtime.workflow_run_id,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
            revision_factory=self._revision_factory,
            startup=startup,
            plugin_mode=True,
            poll_interval=self.long_task_poll_interval,
        )
        qualified, final_rev, report = tuning.tune_accuracy(
            revision, self.datasets, max_rounds=3,
        )

        art = self._register_json_artifact(
            "accuracy-tuning-result", "results/accuracy-tuning-result.json", report,
            "v3_accuracy_tuning_1.0", tags={"reason": report.get("reason", "")},
        )

        if qualified:
            # 达标集合成为当前 revision（后续性能测量/发布基于它）
            self.context.current_revision_id = final_rev.revision_id
            self.context.gates["accuracy.v3.qualified"] = Gate(
                gate_id="accuracy.v3.qualified",
                status="passed",
                criteria="所有数据集 accuracy_compare 退出码=0（相对退化≤5%，含小样本噪声容忍）",
                evaluated_at=datetime.now().isoformat(),
                reason=f"调优达标（{report.get('reason')}，{final_rev.revision_id}）",
            )
            self._save_context()
            return StepResult(status="success", output_artifacts=[art])

        # 调优未达标：精度是硬闸门但不终止流程（发布走 private-only，plan/CLAUDE.md 约束18）
        return StepResult(
            status="success",
            output_artifacts=[art],
        )

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
            # 降级路径（服务不可用 → 已跳过 06-09，没有 v3-final）：按当前 revision
            # 打**私有**镜像。既有编排在 service_ok=false 时同样直接走私有发布，
            # 不留"这一步没有产出"的空档。
            final_rev = self.context.operator_revisions.get(self.context.current_revision_id)
            if final_rev is None:
                return StepResult(status="failed", fail_reason="release: 无可用 revision")
            self.logger.warning(
                f"无 v3-final（降级路径），按当前 revision {final_rev.revision_id} 私有发布"
            )

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
        # 搜索起点是候选，不推进 current_revision（V4 未成立时 V3 仍是当前版本）
        v4_r0 = self._ensure_revision(
            "v4-r0", parent_revision_id="v3-final",
            enabled_ops=list(v3_final.enabled_ops), set_current=False,
        )

        # 优化基线 = V3 实测吞吐（步骤08 登记的性能 artifact），不重复测量同一配置
        baseline = self._latest_performance_throughput(candidate="v3")

        from ..domain import V4OperatorReduction  # 惰性导入，避免顶层循环
        reduction = V4OperatorReduction(
            workspace_root=str(self.workspace_root),
            container_name=container,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
            revision_factory=self._revision_factory,
            startup=self._make_startup_tuning(),
            reference_model=self.context.runtime.model_name,
            nv_baseline_file=self.NV_BASELINE,
            poll_interval=self.long_task_poll_interval,
        )
        report = reduction.performance_search(
            v4_r0, baseline_throughput=baseline,
            seed=self.v4_seed, max_rounds=self.v4_max_rounds,
        )
        report["phase"] = "performance_search"
        report["_meta"] = {
            "candidates": "按吞吐降序的候选组合（阶段2 精度回溯的输入）",
            "baseline_source": "provided = 取步骤08 的 V3 实测吞吐；measured = 本轮重启实测",
            "reason": "no_valid_improvement = 两轮随机子集都没有性能提升 → V4 不成立",
        }

        art = self._register_json_artifact(
            "v4-search-result", "results/v4-search-result.json", report,
            "v4_reduction_1.0", tags={"phase": "performance_search"},
        )

        if not report["candidates"]:
            self._set_v4_established_gate(False, f"V4 无合法性能提升（{report['reason']}）")
            self._record_issue(
                "performance", f"V4 搜索无合法提升（{report['reason']}）",
                detail=f"基线={report.get('baseline_throughput')} tok/s，"
                       f"轮次={report.get('rounds_probed')}，"
                       f"试验={[(t.get('round'), t.get('outcome')) for t in report.get('trials', [])]}",
                action="随机子集性能搜索（两轮）",
                result="回退 V3（不产出 V4）",
            )
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
            revision_factory=self._revision_factory,
            startup=self._make_startup_tuning(),
            reference_model=self.context.runtime.model_name,
            nv_baseline_file=self.NV_BASELINE,
            poll_interval=self.long_task_poll_interval,
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
        set_current: bool = True,
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
            set_current=set_current,
        )

    def _revision_factory(
        self,
        revision_id: str,
        parent_revision_id: Optional[str],
        enabled_ops: List[str],
        disabled_ops_map: Dict[str, str],
        note: str,
    ) -> OperatorRevision:
        """注入给调优/减算子 domain 的 revision 工厂（Engine 是 context 唯一写入者）

        派生的是**候选** revision：未经实测验证前不推进 current_revision_id
        （由 handler 在确认达标后再显式推进）。
        """
        return self._ensure_revision(
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            enabled_ops=enabled_ops,
            additional_disabled=dict(disabled_ops_map) or None,
            set_current=False,
        )

    # thinking 模型判定（与既有编排同口径：模型名正则；CLAUDE.md）
    _THINKING_MODEL_RE = "qwen3|qwq|deepseek-r1|deepseek-r2|mimo|hunyuan"

    def _prepare_service_params(self, force: bool = False):
        """派生起服务参数并回填 runtime（GPU 规划 / TP / thinking / 端口）

        这些参数早前**在 schema 里根本不存在**，引擎只能内联拼一条缺 TP、缺可见设备、
        缺 max_model_len 的启动命令 → 多卡模型起不来、可能撞上别人占用的卡。

        Returns:
            (ServiceParams 或 None, 失败原因)
        """
        if self._service_params is not None and not force:
            return self._service_params, ""

        from ..domain.service_control import (
            ServiceParams, derive_tp_size, plan_service_devices,
        )
        rt = self.context.runtime
        container = rt.container_name
        if not container or not rt.model_path:
            return None, "container_name / model_path 未就绪"

        # 1. thinking 判定（影响 --reasoning-parser 与评测预算）
        if re.search(self._THINKING_MODEL_RE, (rt.model_name or "").lower()):
            rt.thinking_model = True
            self.logger.info("识别为 thinking 模型（将加 --reasoning-parser 并按 thinking 预算评测）")

        # 2. TP：已锁定则复用（约束14：卡数/TP 全流程不变），否则按模型权重大小派生
        tp = rt.tp_size
        if tp <= 0:
            tp = derive_tp_size(self.executor, container, rt.model_path, logger=self.logger)
            if not tp:
                return None, "TP 派生失败（calc_tp_size.py）——fail-closed，不盲目起服务"
            rt.tp_size = tp

        # 3. 可见卡：优先空闲卡；空闲不足时按约束14 复用上次卡列表（卡数优先）
        devices, err = plan_service_devices(
            self.executor, container, tp_needed=tp,
            locked_devices=rt.cuda_visible_devices, logger=self.logger,
        )
        if devices is None:
            return None, err
        rt.cuda_visible_devices = devices
        rt.gpu_count = len([d for d in devices.split(",") if d.strip()])
        rt.gpu_count_locked = True

        # 4. 服务名用短名（与既有编排 `model.name.split('/')[-1]` 一致；
        #    start_service.sh 的 --served-model-name 与 wait_for_service 都按它匹配）
        short_name = (rt.model_name or "").rstrip("/").split("/")[-1]

        self._service_params = ServiceParams(
            model_path=rt.model_path,
            model_name=short_name,
            port=rt.service_port,
            tp_size=tp,
            max_model_len=rt.max_model_len,
            thinking=rt.thinking_model,
            cuda_visible_devices=devices,
        )
        self._save_context()
        self.logger.info(
            f"服务参数就绪：tp={tp} devices={devices} port={rt.service_port} "
            f"max_model_len={rt.max_model_len} thinking={rt.thinking_model}"
        )
        return self._service_params, ""

    def _make_startup_tuning(self):
        """构造配置好的启动调优器（步骤05/07/11/12 共用）

        服务重启（清缓存 → 下发算子白名单 → 起服务 → 等就绪）是调优/减算子的共同前置，
        统一从这里取，保证重启语义与就绪预算一致。
        """
        # 自愈：断点续跑到 05/07/11/12 时（步骤03 在上一轮已成功）参数缓存可能是空的，
        # 这里补一次派生；失败则不阻断构造，由 attempt_startup 以明确原因 fail-closed。
        if self._service_params is None:
            params, perr = self._prepare_service_params()
            if params is None:
                self.logger.warning(f"起服务参数派生失败（{perr}）——启动调优将 fail-closed")

        from ..domain import V3StartupTuning  # 惰性导入，避免顶层循环
        return V3StartupTuning(
            workspace_root=str(self.workspace_root),
            container_name=self.context.runtime.container_name,
            workflow_run_id=self.context.runtime.workflow_run_id,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
            revision_factory=self._revision_factory,
            model_path=self.context.runtime.model_path,
            service_params=self._service_params,
            # 本工作流为 plugin-only（见 plan）；启动即 plugin 白名单路径
            plugin_mode=True,
            startup_timeout=self.startup_tuning_timeout,
            poll_interval=self.startup_tuning_poll_interval,
            long_task_poll_interval=self.long_task_poll_interval,
        )

    def _step_report(self) -> StepResult:
        """步骤14：报告汇总——report.md/json + context_final.yaml + artifact 索引。

        数据来源只有 Context + ArtifactRegistry（plan §9），不含 V1/V2 性能比。
        报告写失败不阻断收尾（步骤15 仍要跑），但记为步骤失败以便暴露问题。
        """
        from ..report import write_artifact_index, write_context_final, write_report

        try:
            md_path, json_path = write_report(self.workspace_root, self.context, self.artifact_registry)
            final_path = write_context_final(self.workspace_root, self.context)
            index_path = write_artifact_index(self.workspace_root, self.artifact_registry)
        except Exception as e:
            self.logger.exception("报告生成失败")
            return StepResult(status="failed", fail_reason=f"报告生成失败：{type(e).__name__}: {e}")

        # 登记报告产物（相对 workspace 的路径）
        art = self.artifact_registry.register_artifact(
            artifact_type="workflow-report",
            content={"report_json": "results/report.json", "report_md": "results/report.md"},
            file_path="results/report.json",
            generated_by="script",
            generator_version="workflow_report_1.0",
            tags={"kind": "report"},
        )
        self.logger.info(f"报告产出：{md_path} / {json_path} / {final_path} / {index_path}")
        return StepResult(status="success", output_artifacts=[art])

    def _latest_performance_throughput(self, candidate: str) -> Optional[float]:
        """取最新一条性能 artifact 的吞吐（用作 V4 的优化基线）；无证据返回 None

        优先读 registry 里已记录的内容摘要（registry 只存摘要，不存全文），
        摘要缺失时才回落到读 artifact 文件。
        """
        art_id = self.artifact_registry.get_latest_artifact(
            "performance-result", tags={"candidate": candidate},
        )
        if not art_id:
            return None

        entry = self.artifact_registry.get_artifact(art_id) or {}
        value = (entry.get("content_summary") or {}).get("throughput_tokens_per_sec")
        if not isinstance(value, (int, float)) or value <= 0:
            content = self.artifact_registry.load_artifact_content(art_id)
            value = (content or {}).get("throughput_tokens_per_sec") if isinstance(content, dict) else None
        return float(value) if isinstance(value, (int, float)) and value > 0 else None

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

    @staticmethod
    def _dataset_verdict_text(dataset: str, result: Dict) -> str:
        """gate reason 的逐数据集文案：区分"判定结论"与"无法评估" """
        if not result.get("assessed", True):
            return f"{dataset}:unassessed({result.get('unassessed_reason', '')[:60]})"
        return f"{dataset}:exit={result.get('exit_code')}"

    def _set_v4_accuracy_gate(self, results: Dict[str, Dict], passed: bool):
        """置 V4 精度 gate（判定来自 accuracy_compare 退出码，不内联重算；三态同 V3）"""
        all_assessed = all(r.get("assessed", True) for r in (results or {}).values())
        status = "passed" if passed else ("failed" if all_assessed else "unresolved")
        self.context.gates["accuracy.v4.qualified"] = Gate(
            gate_id="accuracy.v4.qualified",
            status=status,
            criteria="所有数据集 accuracy_compare 退出码=0（相对退化≤5%，含小样本噪声容忍）",
            evaluated_at=datetime.now().isoformat(),
            reason="; ".join(
                self._dataset_verdict_text(d, r) for d, r in (results or {}).items()
            ) or "no candidate result",
        )
        self._save_context()

    @staticmethod
    def _map_inspect_env_to_capabilities(j: Dict) -> Dict:
        """把 inspect_env.py 的 JSON 输出映射成 PluginOnlyAdmission.check_admission 所需 capabilities。

        真实输出结构（真机核对）是**嵌套**的：
            {
              "inspection": {
                "core_packages": {"torch": ..., "vllm": "0.24.0", "torch_cuda": ...},
                "flag_packages": {"flaggems": "5.3.4", "flagscale": "-", "flagcx": "-",
                                  "vllm_plugin": "installed"},
                "vllm_plugin_installed": true, ...
              },
              "flagtree": {"installed": false, "version": "", "triton_version": "3.6.0", ...}
            }
        而 check_admission 要的是**扁平**键（vllm_version / flaggems_installed / ...）。
        早前实现读的是扁平顶层键，真实输入里全都不存在 → 四个组件全判缺失、准入必失败。
        """
        inspection = j.get("inspection") or {}
        core = inspection.get("core_packages") or {}
        flag_packages = inspection.get("flag_packages") or {}

        def version_of(raw) -> str:
            """版本值归一：未安装的组件在真实输出里是 '-' 或空"""
            text = str(raw or "").strip()
            return "" if text in ("-", "none", "None") else text

        flaggems_version = version_of(flag_packages.get("flaggems"))
        vllm_version = version_of(core.get("vllm"))

        return {
            # 组件"已安装"以版本可用为准（真实输出无独立布尔位）
            "flaggems_installed": bool(flaggems_version),
            "flaggems_version": flaggems_version,
            "vllm_plugin_installed": bool(inspection.get("vllm_plugin_installed", False)),
            "plugin_version": version_of(flag_packages.get("vllm_plugin")),
            "vllm_version": vllm_version,
            "flagtree": j.get("flagtree") or {},
        }
