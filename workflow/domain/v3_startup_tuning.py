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

"""V3 Startup Tuning - 启动兼容性算子调优

职责：
1. 检测启动崩溃
2. 确定性诊断（diagnose_ops.py / 日志分析）
3. 诊断穷尽时调用 Agent
4. 执行验证实验（suggest-verify-commit）
5. 迭代直到服务稳定或达到最大轮次
"""

import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from ..schemas.context_v2 import OperatorRevision
from ..artifacts.registry import ArtifactRegistry
from ..agent.protocol import (
    StartupFailureRequest,
    AnalysisResult,
)
from ..agent.claude_code_agent import ClaudeCodeAnalysisAgent
from ..agent.policy_validator import PolicyValidator
from ..agent.session_manager import AgentSessionManager
from ..engine.verification_executor import VerificationExperimentExecutor
from ..engine.operator_revision_store import OperatorRevisionStore


class V3StartupTuning:
    """V3 启动兼容性算子调优"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        workflow_run_id: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
        agent: Optional[ClaudeCodeAnalysisAgent] = None,
        policy_validator: Optional[PolicyValidator] = None,
        session_manager: Optional[AgentSessionManager] = None,
        revision_store: Optional[OperatorRevisionStore] = None,
    ):
        self.workspace_root = workspace_root
        self.container_name = container_name
        self.workflow_run_id = workflow_run_id

        self.artifact_registry = artifact_registry or ArtifactRegistry(workspace_root)
        self.agent = agent or ClaudeCodeAnalysisAgent(workspace_root, self.artifact_registry)
        self.policy_validator = policy_validator or PolicyValidator()
        self.session_manager = session_manager or AgentSessionManager(workspace_root)
        self.revision_store = revision_store or OperatorRevisionStore()
        self.verification_executor = VerificationExperimentExecutor(workspace_root, self.artifact_registry)

        self.logger = logging.getLogger("workflow.domain.startup_tuning")

    def tune_startup_compatibility(
        self,
        v3_discovered: OperatorRevision,
        max_rounds: int = 5,
    ) -> Tuple[bool, OperatorRevision, List[str]]:
        """启动兼容性调优

        Args:
            v3_discovered: v3-discovered revision
            max_rounds: 最大调优轮次

        Returns:
            (是否成功, 最终 revision, agent_session_ids)
        """
        self.logger.info(f"Starting startup compatibility tuning (max {max_rounds} rounds)")

        current_revision = v3_discovered
        agent_sessions = []

        for round_num in range(1, max_rounds + 1):
            self.logger.info(f"=== Startup tuning round {round_num}/{max_rounds} ===")

            # 1. 尝试启动服务
            success, crash_info = self._attempt_startup(current_revision)

            if success:
                # 启动成功 - 调优完成
                self.logger.info(f"Service started successfully after {round_num} rounds")
                return True, current_revision, agent_sessions

            # 2. 启动失败 - 执行确定性诊断
            self.logger.warning(f"Startup failed: {crash_info.get('error_type')}")

            diagnosed_ops = self._deterministic_diagnosis(crash_info)

            if diagnosed_ops:
                # 确定性诊断成功 - 直接禁用
                self.logger.info(f"Deterministic diagnosis found: {diagnosed_ops}")
                current_revision = self._create_child_revision_with_disabled(
                    current_revision,
                    diagnosed_ops,
                    reason="startup crash (deterministic)",
                )
                continue

            # 3. 确定性诊断穷尽 - 调用 Agent
            self.logger.info("Deterministic diagnosis exhausted, invoking Agent")

            agent_result, session_id = self._invoke_agent_for_startup_failure(
                current_revision,
                crash_info,
            )

            if session_id:
                agent_sessions.append(session_id)

            if agent_result.status != "hypothesis_available":
                # Agent 无法提供假设
                self.logger.error(f"Agent returned status: {agent_result.status}")
                break

            # 4. 校验 Agent 输出
            passed, errors = self._validate_agent_output(
                agent_result,
                current_revision,
                crash_info,
            )

            if not passed:
                self.logger.error(f"Agent output validation failed: {errors}")
                break

            # 5. 执行验证实验
            verification_success, message, artifact_id = self.verification_executor.execute_experiment(
                parent_revision=current_revision,
                experiment=agent_result.recommended_experiment,
                agent_result=agent_result,
            )

            # 更新 session 验证结果
            if session_id:
                self.session_manager.update_verification_result(
                    session_id,
                    verification_status="success" if verification_success else "failed",
                    verification_artifact=artifact_id,
                )

            if verification_success:
                # 验证成功 - 更新 current_revision
                # (VerificationExecutor 内部已创建 child revision)
                # 这里简化：从 revision_store 获取最新的
                # 实际需要 VerificationExecutor 返回新 revision
                self.logger.info(f"Verification succeeded: {message}")
                # current_revision = new_revision
                # 这里暂时 break，实际应该继续下一轮
                break
            else:
                # 验证失败 - negative evidence 已记录，继续下一轮
                self.logger.warning(f"Verification failed: {message}")

        # 达到最大轮次
        self.logger.error(f"Startup tuning failed after {max_rounds} rounds")
        return False, current_revision, agent_sessions

    def _attempt_startup(self, revision: OperatorRevision) -> Tuple[bool, Optional[Dict]]:
        """尝试启动服务

        Args:
            revision: 当前 revision

        Returns:
            (是否成功, 崩溃信息)
        """
        # 实际需要：
        # 1. 应用算子配置（写控制文件或环境变量）
        # 2. 清理缓存
        # 3. 启动服务
        # 4. 等待健康检查或捕获崩溃

        # 简化实现：模拟
        self.logger.info(f"Attempting startup with revision {revision.revision_id}")

        # 模拟启动失败
        crash_info = {
            "error_type": "crash",
            "error_message": "CUDA error in softmax operator",
            "service_log": "/flagos-workspace/logs/service.log",
            "traceback": "...",
        }

        # 实际应该调用启动脚本并捕获结果
        return False, crash_info

    def _deterministic_diagnosis(self, crash_info: Dict) -> Optional[List[str]]:
        """确定性诊断

        Args:
            crash_info: 崩溃信息

        Returns:
            诊断出的问题算子列表，None 表示诊断穷尽
        """
        # 实际需要：
        # 1. 读本轮日志
        # 2. 调用 diagnose_ops.py
        # 3. 提取 crashed_ops / candidate_ops
        # 4. 分析 traceback

        # 简化实现：返回 None（诊断穷尽）
        self.logger.info("Running deterministic diagnosis")

        # 模拟诊断穷尽
        return None

    def _create_child_revision_with_disabled(
        self,
        parent: OperatorRevision,
        ops_to_disable: List[str],
        reason: str,
    ) -> OperatorRevision:
        """创建禁用指定算子的 child revision

        Args:
            parent: 父 revision
            ops_to_disable: 要禁用的算子
            reason: 禁用原因

        Returns:
            Child revision
        """
        additional_disabled = {op: reason for op in ops_to_disable}

        new_enabled_ops = [
            op for op in parent.enabled_ops
            if op not in ops_to_disable
        ]

        # 生成 child revision ID
        child_id = self._generate_child_revision_id(parent.revision_id)

        child_revision = self.revision_store.create_revision(
            revision_id=child_id,
            enabled_ops=new_enabled_ops,
            parent_revision_id=parent.revision_id,
            additional_disabled=additional_disabled,
        )

        return child_revision

    def _generate_child_revision_id(self, parent_id: str) -> str:
        """生成 child revision ID"""
        if "-r" in parent_id:
            base, seq = parent_id.rsplit("-r", 1)
            try:
                next_seq = int(seq) + 1
                return f"{base}-r{next_seq}"
            except ValueError:
                pass

        return f"{parent_id}-r1"

    def _invoke_agent_for_startup_failure(
        self,
        revision: OperatorRevision,
        crash_info: Dict,
    ) -> Tuple[AnalysisResult, Optional[str]]:
        """调用 Agent 分析启动失败

        Args:
            revision: 当前 revision
            crash_info: 崩溃信息

        Returns:
            (AnalysisResult, session_id)
        """
        # 构造 request
        request = StartupFailureRequest(
            schema_version="1.0",
            analysis_type="startup_failure",
            workflow_run_id=self.workflow_run_id,
            candidate="v3",
            operator_revision=revision.revision_id,
            input_artifacts=[],  # 实际需要包含 service log artifact
            operator_constraints={
                "discovered_set": revision.enabled_ops,
                "allow_fallback_to_installed_catalog": False,  # 正常情况不允许
                "require_direct_log_evidence_for_fallback": True,
            },
            allowed_experiments=["disable_ops_and_restart"],
            limits={
                "max_candidate_ops": 3,
                "max_tool_rounds": 12,
                "timeout_seconds": 900,
            },
        )

        # 创建 session
        session = self.session_manager.create_session(request)

        # 调用 Agent
        result = self.agent.analyze_startup_failure(request)

        # 更新 session
        self.session_manager.update_session_result(session.session_id, result)

        return result, session.session_id

    def _validate_agent_output(
        self,
        result: AnalysisResult,
        revision: OperatorRevision,
        crash_info: Dict,
    ) -> Tuple[bool, List[str]]:
        """校验 Agent 输出

        Args:
            result: Agent 结果
            revision: 当前 revision
            crash_info: 崩溃信息

        Returns:
            (是否通过, 错误列表)
        """
        # 构造 request（用于校验）
        request = StartupFailureRequest(
            workflow_run_id=self.workflow_run_id,
            operator_revision=revision.revision_id,
            operator_constraints={
                "discovered_set": revision.enabled_ops,
            },
            allowed_experiments=["disable_ops_and_restart"],
            limits={"max_candidate_ops": 3},
        )

        # 调用 PolicyValidator
        passed, errors = self.policy_validator.validate_analysis_result(
            result,
            request,
            installed_operator_catalog=None,  # 正常情况不需要
        )

        return passed, errors
