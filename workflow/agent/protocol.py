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

"""Analysis Agent Protocol - 结构化请求和响应契约

定义 Workflow Engine 与 Analysis Agent 之间的通信协议。
所有实现（ClaudeCodeAnalysisAgent、LangGraphAnalysisAgent）必须遵循此协议。
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Literal, Any
from abc import ABC, abstractmethod


# ============================================================================
# 请求 Schema
# ============================================================================

@dataclass
class StartupFailureRequest:
    """启动失败分析请求"""
    schema_version: str = "1.0"
    analysis_type: Literal["startup_failure"] = "startup_failure"

    # 上下文
    workflow_run_id: str = ""
    candidate: str = ""  # v3
    operator_revision: str = ""  # v3-startup-r1

    # 输入证据（已登记的 Artifact IDs）
    input_artifacts: List[str] = field(default_factory=list)

    # 算子约束
    operator_constraints: Dict[str, Any] = field(default_factory=dict)
    # {
    #   "discovered_set": [...],  # 来自 v3-discovered 的官方集合
    #   "allow_fallback_to_installed_catalog": bool,  # 仅当无新 oplist
    #   "require_direct_log_evidence_for_fallback": bool,
    # }

    # 允许的实验
    allowed_experiments: List[str] = field(default_factory=list)  # ["disable_ops_and_restart"]

    # 限制
    limits: Dict[str, int] = field(default_factory=dict)
    # {
    #   "max_candidate_ops": 3,
    #   "max_tool_rounds": 12,
    #   "timeout_seconds": 900,
    # }

    _meta: Dict[str, str] = field(default_factory=dict)


@dataclass
class AccuracyRegressionRequest:
    """精度退化分析请求"""
    schema_version: str = "1.0"
    analysis_type: Literal["accuracy_regression"] = "accuracy_regression"

    # 上下文
    workflow_run_id: str = ""
    candidate: str = ""  # v3
    operator_revision: str = ""  # v3-accuracy-r1
    dataset: str = ""  # gpqa_diamond

    # 输入证据
    input_artifacts: List[str] = field(default_factory=list)

    # 算子约束
    operator_constraints: Dict[str, Any] = field(default_factory=dict)

    # 允许的实验
    allowed_experiments: List[str] = field(default_factory=list)

    # 限制
    limits: Dict[str, int] = field(default_factory=dict)

    _meta: Dict[str, str] = field(default_factory=dict)


@dataclass
class UnknownFailureRequest:
    """未知故障分析请求"""
    schema_version: str = "1.0"
    analysis_type: Literal["unknown_failure"] = "unknown_failure"

    # 上下文
    workflow_run_id: str = ""
    step_id: str = ""
    failure_description: str = ""

    # 输入证据
    input_artifacts: List[str] = field(default_factory=list)

    # 限制
    limits: Dict[str, int] = field(default_factory=dict)

    _meta: Dict[str, str] = field(default_factory=dict)


# ============================================================================
# 响应 Schema
# ============================================================================

@dataclass
class SuspectedOperator:
    """可疑算子及证据"""
    name: str  # 算子名（必须精确匹配）
    confidence: float  # 0.0-1.0
    evidence_artifacts: List[str] = field(default_factory=list)  # Artifact IDs
    evidence_locations: List[str] = field(default_factory=list)  # 如 "service.log:1832-1874"
    reasoning: str = ""  # 推理过程（可选）


@dataclass
class RecommendedExperiment:
    """推荐的验证实验"""
    type: str  # disable_ops_and_restart / enable_ops_and_restart / ...
    ops: List[str] = field(default_factory=list)  # 涉及的算子
    parameters: Dict[str, Any] = field(default_factory=dict)  # 实验参数
    expected_outcome: str = ""  # 预期结果


@dataclass
class AnalysisResult:
    """Agent 分析结果"""
    schema_version: str = "1.0"

    # 会话标识
    agent_session_id: str = ""  # Agent 生成的唯一 session ID
    workflow_run_id: str = ""  # 必须与 request 一致
    operator_revision: str = ""  # 必须与 request 一致

    # 状态
    status: Literal["hypothesis_available", "no_hypothesis", "unresolved", "error"] = "unresolved"

    # 假设（status == hypothesis_available 时必填）
    suspected_ops: List[SuspectedOperator] = field(default_factory=list)
    recommended_experiment: Optional[RecommendedExperiment] = None

    # 失败信息（status == error 时填写）
    error_message: str = ""
    error_type: str = ""  # schema_validation / timeout / api_failure / ...

    # 元数据
    analysis_duration_seconds: float = 0.0
    tool_calls: int = 0  # Agent 使用的工具调用次数
    _meta: Dict[str, str] = field(default_factory=dict)


# ============================================================================
# Agent Session
# ============================================================================

@dataclass
class AgentSession:
    """Agent 分析会话状态"""
    session_id: str
    request_type: str  # startup_failure / accuracy_regression / unknown_failure
    workflow_run_id: str
    operator_revision: str

    # 状态
    status: Literal["pending", "running", "hypothesis_available", "no_hypothesis", "unresolved", "error"] = "pending"

    # 时间
    started_at: str = ""  # ISO 8601
    finished_at: Optional[str] = None

    # 结果
    result: Optional[AnalysisResult] = None

    # 验证实验结果（Engine 填写）
    verification_status: Optional[Literal["success", "failed", "not_executed"]] = None
    verification_artifact: Optional[str] = None

    _meta: Dict[str, str] = field(default_factory=dict)


# ============================================================================
# AnalysisAgent 接口
# ============================================================================

class AnalysisAgent(ABC):
    """Analysis Agent 抽象接口

    所有 Agent 实现（ClaudeCode、LangGraph）必须实现此接口。
    """

    @abstractmethod
    def analyze_startup_failure(self, request: StartupFailureRequest) -> AnalysisResult:
        """分析启动失败

        Args:
            request: 启动失败请求

        Returns:
            分析结果
        """
        pass

    @abstractmethod
    def analyze_accuracy_regression(self, request: AccuracyRegressionRequest) -> AnalysisResult:
        """分析精度退化

        Args:
            request: 精度退化请求

        Returns:
            分析结果
        """
        pass

    @abstractmethod
    def analyze_unknown_failure(self, request: UnknownFailureRequest) -> AnalysisResult:
        """分析未知故障

        Args:
            request: 未知故障请求

        Returns:
            分析结果
        """
        pass

    @abstractmethod
    def get_capabilities(self) -> Dict[str, Any]:
        """获取 Agent 能力描述

        Returns:
            {
                "structured_output": bool,
                "tool_calling": bool,
                "max_context_length": int,
                "max_tool_rounds": int,
                "supported_analysis_types": List[str],
            }
        """
        pass
