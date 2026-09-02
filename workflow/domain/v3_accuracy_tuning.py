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

"""V3 Accuracy Tuning - V3 精度算子调优

职责：
1. 检测精度退化
2. 分析问题算子（Agent 介入）
3. 执行验证实验
4. 迭代直到精度达标或达到最大轮次
"""

import logging
from typing import Dict, List, Optional, Tuple

from ..schemas.context_v2 import OperatorRevision
from ..artifacts.registry import ArtifactRegistry
from ..agent.protocol import (
    AccuracyRegressionRequest,
    AnalysisResult,
)
from ..agent.claude_code_agent import ClaudeCodeAnalysisAgent
from ..agent.policy_validator import PolicyValidator
from ..agent.session_manager import AgentSessionManager
from ..engine.verification_executor import VerificationExperimentExecutor
from ..engine.operator_revision_store import OperatorRevisionStore


class V3AccuracyTuning:
    """V3 精度算子调优"""

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

        self.logger = logging.getLogger("workflow.domain.accuracy_tuning")

    def tune_accuracy(
        self,
        current_revision: OperatorRevision,
        datasets: List[str],
        threshold: float = 0.05,
        max_rounds: int = 3,
    ) -> Tuple[bool, OperatorRevision, List[str]]:
        """精度算子调优

        Args:
            current_revision: 当前 revision（v3-startup-stable）
            datasets: 数据集列表
            threshold: 相对退化阈值（默认 5%）
            max_rounds: 最大调优轮次

        Returns:
            (是否成功, 最终 revision, agent_session_ids)
        """
        self.logger.info(
            f"Starting accuracy tuning (threshold={threshold*100}%, "
            f"max {max_rounds} rounds)"
        )

        agent_sessions = []

        for round_num in range(1, max_rounds + 1):
            self.logger.info(f"=== Accuracy tuning round {round_num}/{max_rounds} ===")

            # 1. 评测精度
            from .v3_accuracy import V3AccuracyEvaluation

            evaluator = V3AccuracyEvaluation(
                self.workspace_root,
                self.container_name,
                self.artifact_registry,
            )

            all_qualified, results = evaluator.evaluate_accuracy(
                candidate="v3",
                revision=current_revision,
                datasets=datasets,
            )

            if all_qualified:
                # 精度达标 - 调优完成
                self.logger.info(f"Accuracy qualified after {round_num} rounds")
                return True, current_revision, agent_sessions

            # 2. 精度不达标 - 调用 Agent 分析
            self.logger.warning("Accuracy not qualified, invoking Agent")

            # 找出不达标的数据集
            failed_datasets = [
                ds for ds, res in results.items()
                if not res.get("qualified", False)
            ]

            # 对每个失败的数据集单独分析（简化：只处理第一个）
            dataset = failed_datasets[0]

            agent_result, session_id = self._invoke_agent_for_accuracy_regression(
                current_revision,
                dataset,
                results[dataset],
            )

            if session_id:
                agent_sessions.append(session_id)

            if agent_result.status != "hypothesis_available":
                self.logger.error(f"Agent returned status: {agent_result.status}")
                break

            # 3. 校验 Agent 输出
            passed, errors = self._validate_agent_output(
                agent_result,
                current_revision,
                dataset,
            )

            if not passed:
                self.logger.error(f"Agent output validation failed: {errors}")
                break

            # 4. 执行验证实验
            verification_success, message, artifact_id = self.verification_executor.execute_experiment(
                parent_revision=current_revision,
                experiment=agent_result.recommended_experiment,
                agent_result=agent_result,
            )

            # 更新 session
            if session_id:
                self.session_manager.update_verification_result(
                    session_id,
                    verification_status="success" if verification_success else "failed",
                    verification_artifact=artifact_id,
                )

            if verification_success:
                self.logger.info(f"Verification succeeded: {message}")
                # 实际应该更新 current_revision
                # 简化：暂时 break
                break
            else:
                self.logger.warning(f"Verification failed: {message}")

        # 达到最大轮次
        self.logger.error(f"Accuracy tuning failed after {max_rounds} rounds")
        return False, current_revision, agent_sessions

    def _invoke_agent_for_accuracy_regression(
        self,
        revision: OperatorRevision,
        dataset: str,
        result: Dict,
    ) -> Tuple[AnalysisResult, Optional[str]]:
        """调用 Agent 分析精度退化

        Args:
            revision: 当前 revision
            dataset: 数据集
            result: 评测结果

        Returns:
            (AnalysisResult, session_id)
        """
        # 构造 request
        request = AccuracyRegressionRequest(
            schema_version="1.0",
            analysis_type="accuracy_regression",
            workflow_run_id=self.workflow_run_id,
            candidate="v3",
            operator_revision=revision.revision_id,
            dataset=dataset,
            input_artifacts=[],  # 实际需要包含 accuracy result artifact
            operator_constraints={
                "discovered_set": revision.enabled_ops,
                "allow_fallback_to_installed_catalog": False,
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
        agent_result = self.agent.analyze_accuracy_regression(request)

        # 更新 session
        self.session_manager.update_session_result(session.session_id, agent_result)

        return agent_result, session.session_id

    def _validate_agent_output(
        self,
        result: AnalysisResult,
        revision: OperatorRevision,
        dataset: str,
    ) -> Tuple[bool, List[str]]:
        """校验 Agent 输出

        Args:
            result: Agent 结果
            revision: 当前 revision
            dataset: 数据集

        Returns:
            (是否通过, 错误列表)
        """
        # 构造 request（用于校验）
        request = AccuracyRegressionRequest(
            workflow_run_id=self.workflow_run_id,
            operator_revision=revision.revision_id,
            dataset=dataset,
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
            installed_operator_catalog=None,
        )

        return passed, errors
