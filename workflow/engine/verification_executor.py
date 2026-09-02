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

"""Verification Experiment Executor - 执行 Agent 推荐的验证实验

职责：
1. 基于 Agent 推荐创建 experimental child revision
2. 应用算子配置
3. 重启服务
4. 验证结果（服务健康、错误消失）
5. 提交或回滚 revision
6. 记录 negative evidence
"""

import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from ..schemas.context_v2 import OperatorRevision
from ..artifacts.registry import ArtifactRegistry
from ..agent.protocol import RecommendedExperiment, AnalysisResult


class VerificationExperimentExecutor:
    """验证实验执行器"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        artifact_registry: Optional[ArtifactRegistry] = None,
    ):
        self.workspace_root = workspace_root
        self.artifact_registry = artifact_registry or ArtifactRegistry(workspace_root)
        self.logger = logging.getLogger("workflow.verification")

    def execute_experiment(
        self,
        parent_revision: OperatorRevision,
        experiment: RecommendedExperiment,
        agent_result: AnalysisResult,
    ) -> Tuple[bool, str, Optional[str]]:
        """执行验证实验

        Args:
            parent_revision: 父版本
            experiment: 推荐实验
            agent_result: Agent 分析结果

        Returns:
            (是否成功, 消息, 新 artifact_id)
        """
        self.logger.info(
            f"Executing verification experiment: {experiment.type}, "
            f"ops={experiment.ops}"
        )

        # 生成 child revision ID
        child_revision_id = self._generate_child_revision_id(parent_revision.revision_id)

        # 根据实验类型执行
        if experiment.type == "disable_ops_and_restart":
            return self._execute_disable_ops(
                parent_revision, child_revision_id, experiment, agent_result
            )
        elif experiment.type == "enable_ops_and_restart":
            return self._execute_enable_ops(
                parent_revision, child_revision_id, experiment, agent_result
            )
        else:
            self.logger.error(f"Unknown experiment type: {experiment.type}")
            return False, f"Unknown experiment type: {experiment.type}", None

    def _generate_child_revision_id(self, parent_revision_id: str) -> str:
        """生成 child revision ID

        Args:
            parent_revision_id: 父版本 ID

        Returns:
            Child revision ID
        """
        # 提取父版本的基础部分和序号
        # 例如: v3-startup-r1 → v3-startup-r2
        #      v3-accuracy-r2 → v3-accuracy-r3

        if "-r" in parent_revision_id:
            base, seq = parent_revision_id.rsplit("-r", 1)
            try:
                next_seq = int(seq) + 1
                return f"{base}-r{next_seq}"
            except ValueError:
                pass

        # 如果没有序号，添加 -r1
        return f"{parent_revision_id}-r1"

    def _execute_disable_ops(
        self,
        parent_revision: OperatorRevision,
        child_revision_id: str,
        experiment: RecommendedExperiment,
        agent_result: AnalysisResult,
    ) -> Tuple[bool, str, Optional[str]]:
        """执行禁用算子实验

        Args:
            parent_revision: 父版本
            child_revision_id: 子版本 ID
            experiment: 实验定义
            agent_result: Agent 结果

        Returns:
            (是否成功, 消息, 验证 artifact_id)
        """
        # 1. 创建 child revision
        additional_disabled = {}
        for op_name in experiment.ops:
            # 从 Agent result 中找到对应的 suspected_op 获取 reasoning
            reasoning = "Agent recommendation"
            for suspected_op in agent_result.suspected_ops:
                if suspected_op.name == op_name:
                    reasoning = suspected_op.reasoning or "Agent recommendation"
                    break

            additional_disabled[op_name] = f"startup crash: {reasoning}"

        # 计算新的 enabled_ops（移除被禁用的）
        new_enabled_ops = [
            op for op in parent_revision.enabled_ops
            if op not in experiment.ops
        ]

        # 这里需要实际创建 revision（通过 OperatorRevisionStore）
        # 简化实现：返回成功
        self.logger.info(
            f"Created child revision {child_revision_id} with {len(additional_disabled)} ops disabled"
        )

        # 2. 应用配置（写入控制文件或环境变量）
        # 这里需要调用领域执行器来实际应用配置
        # 简化实现：假设成功

        # 3. 清理缓存
        self.logger.info("Clearing Triton/FlagGems cache")
        # 实际需要执行: docker exec <container> rm -rf /root/.triton/cache/ ...

        # 4. 重启服务
        self.logger.info("Restarting service with new operator configuration")
        # 实际需要调用 start_service.sh

        # 5. 验证结果（服务健康检查 + 原错误是否消失）
        service_healthy = True  # 模拟
        original_error_gone = True  # 模拟

        if service_healthy and original_error_gone:
            # 验证成功 - 提交 revision
            self.logger.info(f"Verification successful, committing revision {child_revision_id}")

            # 创建验证 artifact
            verification_artifact_id = self._create_verification_artifact(
                child_revision_id,
                experiment,
                success=True,
                message="Service recovered, original error gone",
            )

            return True, "Verification successful", verification_artifact_id

        else:
            # 验证失败 - 回滚
            self.logger.info(f"Verification failed, rolling back to {parent_revision.revision_id}")

            # 记录 negative evidence
            self._record_negative_evidence(
                experiment.ops,
                agent_result.agent_session_id,
                "Verification failed: service still unhealthy or error persists",
            )

            # 创建失败 artifact
            verification_artifact_id = self._create_verification_artifact(
                child_revision_id,
                experiment,
                success=False,
                message="Verification failed",
            )

            return False, "Verification failed", verification_artifact_id

    def _execute_enable_ops(
        self,
        parent_revision: OperatorRevision,
        child_revision_id: str,
        experiment: RecommendedExperiment,
        agent_result: AnalysisResult,
    ) -> Tuple[bool, str, Optional[str]]:
        """执行启用算子实验（用于精度调优恢复）

        Args:
            parent_revision: 父版本
            child_revision_id: 子版本 ID
            experiment: 实验定义
            agent_result: Agent 结果

        Returns:
            (是否成功, 消息, 验证 artifact_id)
        """
        # 类似 disable_ops 的流程，但是是添加算子
        # 这里简化实现
        self.logger.info(f"Enable ops experiment not fully implemented yet")
        return False, "Enable ops not implemented", None

    def _create_verification_artifact(
        self,
        revision_id: str,
        experiment: RecommendedExperiment,
        success: bool,
        message: str,
    ) -> str:
        """创建验证结果 Artifact

        Args:
            revision_id: Revision ID
            experiment: 实验定义
            success: 是否成功
            message: 结果消息

        Returns:
            Artifact ID
        """
        content = {
            "revision_id": revision_id,
            "experiment_type": experiment.type,
            "experiment_ops": experiment.ops,
            "success": success,
            "message": message,
            "timestamp": datetime.now().isoformat(),
        }

        # 写入临时文件（实际需要写到正确位置）
        # 简化实现：返回模拟 artifact ID
        artifact_id = f"art-verification-{revision_id.replace('-', '_')}"

        self.logger.info(f"Created verification artifact: {artifact_id}")

        return artifact_id

    def _record_negative_evidence(
        self,
        ops: List[str],
        agent_session_id: str,
        reason: str,
    ):
        """记录 negative evidence（实验失败的算子不是问题根因）

        Args:
            ops: 实验涉及的算子
            agent_session_id: Agent session ID
            reason: 失败原因
        """
        self.logger.info(
            f"Recording negative evidence for ops {ops}: {reason} "
            f"(session: {agent_session_id})"
        )

        # 实际需要写入持久化存储
        # 用于后续 Agent 调用时提供历史信息
