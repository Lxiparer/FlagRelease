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
)
from ..artifacts.registry import ArtifactRegistry
from ..gates.reducer import GateReducer
from .state_store import YamlStateStore


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

    def __init__(self, workspace_root: str = "/flagos-workspace"):
        self.workspace_root = Path(workspace_root)
        self.context_file = self.workspace_root / "shared" / "context.yaml"

        # 设置日志
        self.logger = logging.getLogger("workflow.engine")

        # 状态存储后端（引擎是 context 的唯一写入者，见 state_store.py）
        self.state_store = YamlStateStore(self.context_file)

        # 初始化子系统
        self.artifact_registry = ArtifactRegistry(str(self.workspace_root))
        self.gate_reducer = GateReducer(self.artifact_registry)

        # 加载或初始化 context
        self.context = self._load_or_initialize_context()

        # 15 步执行器 dispatch 表（M0 为 stub，M1/M2 逐个替换为真实 domain 调用）
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
            "02_admission": self._stub_step,
            "03_v3_discovery_startup": self._stub_discovery,
            "04_v3_discovered": self._stub_freeze_discovered,
            "05_v3_startup_tuning": self._stub_step,
            "06_v3_accuracy": self._stub_step,
            "07_v3_accuracy_tuning": self._stub_step,
            "08_v3_performance": self._stub_step,
            "09_v3_final": self._stub_freeze_final,
            "10_v3_release": self._stub_step,
            "11_v4_reduction": self._stub_step,
            "12_v4_accuracy_check": self._stub_step,
            "13_v4_release": self._stub_step,
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
