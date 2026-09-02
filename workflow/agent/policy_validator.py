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

"""Policy Validator - Agent 输出的三重校验

Agent 返回结果后必须经过三重校验：
1. Schema 校验：结构完整、类型正确
2. Identity 校验：workflow_run_id、operator_revision 匹配
3. Policy 校验：算子合法性、实验授权、证据完整性
"""

from typing import Dict, List, Optional, Tuple, Any
import logging

from .protocol import (
    AnalysisResult,
    StartupFailureRequest,
    AccuracyRegressionRequest,
    UnknownFailureRequest,
    SuspectedOperator,
    RecommendedExperiment,
)


class PolicyValidator:
    """Agent 输出策略校验器"""

    def __init__(self):
        self.logger = logging.getLogger("workflow.agent.policy")

    def validate_analysis_result(
        self,
        result: AnalysisResult,
        request: Any,  # StartupFailureRequest | AccuracyRegressionRequest | UnknownFailureRequest
        installed_operator_catalog: Optional[List[str]] = None,
    ) -> Tuple[bool, List[str]]:
        """三重校验 Agent 返回结果

        Args:
            result: Agent 返回的分析结果
            request: 原始请求
            installed_operator_catalog: 已安装的算子目录（用于 no-oplist 场景）

        Returns:
            (是否通过, 错误列表)
        """
        errors = []

        # 1. Schema 校验
        schema_errors = self._validate_schema(result)
        if schema_errors:
            errors.extend([f"[Schema] {e}" for e in schema_errors])

        # 2. Identity 校验
        identity_errors = self._validate_identity(result, request)
        if identity_errors:
            errors.extend([f"[Identity] {e}" for e in identity_errors])

        # 3. Policy 校验
        policy_errors = self._validate_policy(result, request, installed_operator_catalog)
        if policy_errors:
            errors.extend([f"[Policy] {e}" for e in policy_errors])

        passed = len(errors) == 0
        return passed, errors

    def _validate_schema(self, result: AnalysisResult) -> List[str]:
        """Schema 校验：结构完整性"""
        errors = []

        # 检查必填字段
        if not result.agent_session_id:
            errors.append("agent_session_id is required")

        if not result.workflow_run_id:
            errors.append("workflow_run_id is required")

        if not result.operator_revision:
            errors.append("operator_revision is required")

        if result.status not in ["hypothesis_available", "no_hypothesis", "unresolved", "error"]:
            errors.append(f"invalid status: {result.status}")

        # 状态特定校验
        if result.status == "hypothesis_available":
            if not result.suspected_ops:
                errors.append("hypothesis_available requires suspected_ops")

            if not result.recommended_experiment:
                errors.append("hypothesis_available requires recommended_experiment")

            # 检查 suspected_ops 结构
            for i, op in enumerate(result.suspected_ops):
                if not op.name:
                    errors.append(f"suspected_ops[{i}].name is required")

                if not (0.0 <= op.confidence <= 1.0):
                    errors.append(f"suspected_ops[{i}].confidence must be in [0.0, 1.0]")

                if not op.evidence_artifacts and not op.evidence_locations:
                    errors.append(f"suspected_ops[{i}] must have evidence_artifacts or evidence_locations")

        elif result.status == "error":
            if not result.error_message:
                errors.append("error status requires error_message")

        return errors

    def _validate_identity(
        self,
        result: AnalysisResult,
        request: Any,
    ) -> List[str]:
        """Identity 校验：上下文匹配"""
        errors = []

        # workflow_run_id 必须匹配
        if result.workflow_run_id != request.workflow_run_id:
            errors.append(
                f"workflow_run_id mismatch: result={result.workflow_run_id}, "
                f"request={request.workflow_run_id}"
            )

        # operator_revision 必须匹配
        if hasattr(request, 'operator_revision'):
            if result.operator_revision != request.operator_revision:
                errors.append(
                    f"operator_revision mismatch: result={result.operator_revision}, "
                    f"request={request.operator_revision}"
                )

        return errors

    def _validate_policy(
        self,
        result: AnalysisResult,
        request: Any,
        installed_operator_catalog: Optional[List[str]],
    ) -> List[str]:
        """Policy 校验：业务规则"""
        errors = []

        if result.status != "hypothesis_available":
            # 非假设状态无需 policy 校验
            return errors

        # 获取算子约束
        operator_constraints = getattr(request, 'operator_constraints', {})
        discovered_set = operator_constraints.get('discovered_set', [])
        allow_fallback = operator_constraints.get('allow_fallback_to_installed_catalog', False)
        require_evidence = operator_constraints.get('require_direct_log_evidence_for_fallback', True)

        # 校验 suspected_ops 的算子合法性
        for op in result.suspected_ops:
            op_name = op.name

            # 正常情况：算子必须属于 discovered_set
            if discovered_set and op_name not in discovered_set:
                # 检查是否允许 fallback
                if allow_fallback and installed_operator_catalog:
                    # No-oplist 场景：检查是否在 installed catalog
                    if op_name not in installed_operator_catalog:
                        errors.append(
                            f"operator '{op_name}' not in discovered_set nor installed_catalog"
                        )
                    elif require_evidence:
                        # 需要直接证据
                        if not op.evidence_locations:
                            errors.append(
                                f"operator '{op_name}' requires direct log evidence in no-oplist fallback"
                            )
                else:
                    errors.append(
                        f"operator '{op_name}' not in discovered_set (fallback not allowed)"
                    )

        # 校验 recommended_experiment 授权
        if result.recommended_experiment:
            allowed_experiments = getattr(request, 'allowed_experiments', [])
            exp_type = result.recommended_experiment.type

            if exp_type not in allowed_experiments:
                errors.append(
                    f"experiment type '{exp_type}' not in allowed_experiments: {allowed_experiments}"
                )

            # 检查实验涉及的算子是否都在 suspected_ops 中
            exp_ops = set(result.recommended_experiment.ops)
            suspected_op_names = set(op.name for op in result.suspected_ops)

            if not exp_ops.issubset(suspected_op_names):
                extra_ops = exp_ops - suspected_op_names
                errors.append(
                    f"experiment ops {extra_ops} not in suspected_ops"
                )

        # 校验 limits
        limits = getattr(request, 'limits', {})
        max_candidate_ops = limits.get('max_candidate_ops', 10)

        if len(result.suspected_ops) > max_candidate_ops:
            errors.append(
                f"too many suspected_ops: {len(result.suspected_ops)} > {max_candidate_ops}"
            )

        return errors

    def check_operator_membership(
        self,
        op_name: str,
        discovered_set: List[str],
        allow_fallback: bool = False,
        installed_catalog: Optional[List[str]] = None,
        evidence: Optional[List[str]] = None,
    ) -> bool:
        """检查算子是否合法

        Args:
            op_name: 算子名
            discovered_set: 已发现的官方集合
            allow_fallback: 是否允许 fallback 到 installed catalog
            installed_catalog: 已安装的算子目录
            evidence: 直接证据（日志位置）

        Returns:
            是否合法
        """
        # 首选：在 discovered_set 中
        if op_name in discovered_set:
            return True

        # Fallback：no-oplist 场景
        if allow_fallback and installed_catalog:
            if op_name in installed_catalog:
                # 如果要求直接证据，检查是否有
                if evidence is None or len(evidence) == 0:
                    self.logger.warning(
                        f"Operator '{op_name}' in installed_catalog but lacks direct evidence"
                    )
                    return False
                return True

        return False
