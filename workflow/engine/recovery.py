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

"""Recovery Manager - 工作流恢复机制

职责：
1. 检测中断的工作流
2. 诊断中断原因
3. 确定恢复策略
4. 恢复长任务（通过 state 文件）
"""

import os
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import logging

from ..schemas.context_v2 import ContextSchemaV2, WorkflowStep


class RecoveryManager:
    """工作流恢复管理器"""

    def __init__(self, workspace_root: str = "/flagos-workspace"):
        self.workspace_root = Path(workspace_root)
        self.logger = logging.getLogger("workflow.recovery")

    def detect_interrupted_workflow(self, context: ContextSchemaV2) -> bool:
        """检测是否有中断的工作流

        Args:
            context: 当前 context

        Returns:
            是否有中断
        """
        # 检查是否有 running 状态的步骤
        for step in context.steps.values():
            if step.status == "running":
                return True

        # 检查是否有 failed 状态的步骤
        for step in context.steps.values():
            if step.status == "failed":
                return True

        # 检查是否有未完成的步骤（有已完成的，但流程未到最后）
        has_completed = any(s.status == "success" for s in context.steps.values())
        has_pending = any(s.status == "pending" for s in context.steps.values())

        return has_completed and has_pending

    def diagnose_interruption(
        self,
        context: ContextSchemaV2,
    ) -> Dict[str, any]:
        """诊断中断原因

        Args:
            context: 当前 context

        Returns:
            诊断结果字典
        """
        diagnosis = {
            "interrupted": False,
            "interrupted_step": None,
            "last_status": None,
            "failure_reason": None,
            "recovery_strategy": None,
            "long_task_state": None,
        }

        # 找到最后一个非 pending 步骤
        last_step = None
        for step_id, step in context.steps.items():
            if step.status != "pending":
                last_step = step

        if not last_step:
            # 所有步骤都是 pending - 新流程
            diagnosis["recovery_strategy"] = "start_from_beginning"
            return diagnosis

        diagnosis["interrupted"] = True
        diagnosis["interrupted_step"] = last_step.step_id
        diagnosis["last_status"] = last_step.status

        # 检查失败原因
        if last_step.status == "failed":
            diagnosis["failure_reason"] = last_step.fail_reason
            diagnosis["recovery_strategy"] = "retry_failed_step"

        elif last_step.status == "running":
            # 检查是否有长任务 state 文件
            long_task_state = self._check_long_task_state(last_step.step_id)
            if long_task_state:
                diagnosis["long_task_state"] = long_task_state
                diagnosis["recovery_strategy"] = "resume_long_task"
            else:
                diagnosis["recovery_strategy"] = "restart_running_step"

        elif last_step.status == "success":
            # 上一个成功了，继续下一个
            diagnosis["recovery_strategy"] = "continue_next_step"

        return diagnosis

    def _check_long_task_state(self, step_id: str) -> Optional[Dict]:
        """检查长任务的 state 文件

        Args:
            step_id: 步骤 ID

        Returns:
            State 文件内容，如果不存在返回 None
        """
        # 长任务 state 文件命名规则：state/<step_id>_<task_name>.state
        state_dir = self.workspace_root / "state"
        if not state_dir.exists():
            return None

        # 查找匹配的 state 文件
        for state_file in state_dir.glob(f"{step_id}_*.state"):
            try:
                with open(state_file, 'r') as f:
                    state = json.load(f)
                    # 检查任务是否还在运行
                    if state.get('status') == 'running':
                        return {
                            'file': str(state_file),
                            'task_name': state.get('task_name'),
                            'started_at': state.get('started_at'),
                            'pid': state.get('pid'),
                        }
            except Exception as e:
                self.logger.warning(f"Failed to read state file {state_file}: {e}")

        return None

    def generate_recovery_recommendation(
        self,
        diagnosis: Dict[str, any],
    ) -> str:
        """生成恢复建议（给用户看的）

        Args:
            diagnosis: 诊断结果

        Returns:
            恢复建议文本
        """
        if not diagnosis["interrupted"]:
            return "流程未中断，正常启动"

        strategy = diagnosis["recovery_strategy"]
        step = diagnosis["interrupted_step"]

        if strategy == "retry_failed_step":
            return f"步骤 {step} 失败，原因：{diagnosis['failure_reason']}\n建议：修复问题后从该步骤重试"

        elif strategy == "resume_long_task":
            task_info = diagnosis["long_task_state"]
            return (
                f"步骤 {step} 的长任务仍在后台运行\n"
                f"任务：{task_info['task_name']}\n"
                f"PID: {task_info['pid']}\n"
                f"建议：从断点恢复，继续监控任务状态"
            )

        elif strategy == "restart_running_step":
            return f"步骤 {step} 被中断，建议：重新执行该步骤"

        elif strategy == "continue_next_step":
            return f"上一步骤 {step} 已完成，建议：继续下一步骤"

        else:
            return "未知恢复策略"

    def attempt_recovery(
        self,
        context: ContextSchemaV2,
        diagnosis: Dict[str, any],
    ) -> Tuple[bool, str]:
        """尝试自动恢复

        Args:
            context: 当前 context
            diagnosis: 诊断结果

        Returns:
            (是否成功, 恢复信息)
        """
        strategy = diagnosis["recovery_strategy"]

        if strategy == "start_from_beginning":
            return True, "新流程，从头开始"

        elif strategy == "continue_next_step":
            # 找到下一个步骤
            step_ids = list(context.steps.keys())
            current_index = step_ids.index(diagnosis["interrupted_step"])
            if current_index < len(step_ids) - 1:
                next_step = step_ids[current_index + 1]
                context.current_step_id = next_step
                return True, f"继续执行步骤 {next_step}"
            else:
                return True, "所有步骤已完成"

        elif strategy == "resume_long_task":
            # 长任务恢复由 workflow engine 处理
            return True, f"从步骤 {diagnosis['interrupted_step']} 的长任务断点恢复"

        elif strategy == "restart_running_step":
            # 重置 running 状态为 pending
            step = context.steps[diagnosis["interrupted_step"]]
            step.status = "pending"
            step.started_at = None
            context.current_step_id = diagnosis["interrupted_step"]
            return True, f"重新执行步骤 {diagnosis['interrupted_step']}"

        elif strategy == "retry_failed_step":
            # 失败步骤需要人工介入或修复后重试
            step = context.steps[diagnosis["interrupted_step"]]
            step.status = "pending"
            step.fail_reason = ""
            context.current_step_id = diagnosis["interrupted_step"]
            return True, f"重试失败步骤 {diagnosis['interrupted_step']}"

        else:
            return False, f"未知恢复策略：{strategy}"

    def save_failure_diagnosis(
        self,
        diagnosis: Dict[str, any],
        output_file: Optional[str] = None,
    ):
        """保存失败诊断到文件

        Args:
            diagnosis: 诊断结果
            output_file: 输出文件路径（默认 logs/failure_diagnosis.json）
        """
        if output_file is None:
            output_file = self.workspace_root / "logs" / "failure_diagnosis.json"
        else:
            output_file = Path(output_file)

        output_file.parent.mkdir(parents=True, exist_ok=True)

        diagnosis_with_timestamp = {
            **diagnosis,
            "diagnosed_at": datetime.now().isoformat(),
        }

        with open(output_file, 'w') as f:
            json.dump(diagnosis_with_timestamp, f, indent=2, ensure_ascii=False)

        self.logger.info(f"Failure diagnosis saved to {output_file}")
