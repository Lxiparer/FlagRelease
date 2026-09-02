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

"""Artifact Schema - 证据化事实的统一契约

所有 Artifact 必须：
1. 有唯一 ID（art-<type>-<sequence>）
2. 有内容哈希（sha256）
3. 有生成时间戳
4. 有生成来源（脚本/Agent/手动）
5. 遵循类型特定的 schema
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Literal, Any
from datetime import datetime
import hashlib
import json


@dataclass
class ArtifactMetadata:
    """Artifact 元数据"""
    artifact_id: str  # art-<type>-<sequence>
    artifact_type: str  # runtime-oplist / accuracy-result / performance-result / ...
    version: int = 1

    # 来源
    generated_by: str = ""  # script / agent / manual
    generator_version: str = ""  # 脚本版本或 agent session ID

    # 时间
    created_at: str = ""  # ISO 8601

    # 内容验证
    content_hash: str = ""  # sha256
    content_size: int = 0

    # 文件路径（相对于 /flagos-workspace）
    file_path: str = ""

    # 依赖关系
    depends_on: List[str] = field(default_factory=list)  # 依赖的其他 artifact IDs

    # 有效性
    valid: bool = True
    validation_errors: List[str] = field(default_factory=list)

    # 自由元数据
    tags: Dict[str, str] = field(default_factory=dict)
    _meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeOplistArtifact:
    """运行时算子列表 Artifact"""
    metadata: ArtifactMetadata

    # 算子列表
    operators: List[str] = field(default_factory=list)

    # 来源
    source_file: str = ""  # /tmp/flaggems_enable_oplist.txt 或 gems.txt

    # 运行时环境
    runtime_env: Dict[str, str] = field(default_factory=dict)  # USE_FLAGGEMS, VLLM_PLUGINS, etc.

    # Freshness 校验
    service_start_time: str = ""  # 服务启动时间
    oplist_mtime: str = ""  # 文件修改时间
    freshness_validated: bool = False

    # Identity 校验
    flaggems_version: str = ""
    identity_validated: bool = False
    expected_operator_count_range: tuple = (50, 150)  # 合理范围

    # 控制输入对比（可选）
    control_input: Optional[List[str]] = None  # 白名单控制文件内容
    actual_matches_control: Optional[bool] = None


@dataclass
class AccuracyResultArtifact:
    """精度评测结果 Artifact"""
    metadata: ArtifactMetadata

    # 评测参数
    dataset: str = ""  # gpqa_diamond / mmlu / math_500
    candidate: str = ""  # v3 / v4
    limit: Optional[int] = None
    max_timeout: int = 7200

    # 结果
    accuracy: float = 0.0  # 0-100
    total_questions: int = 0
    correct: int = 0

    # 外部参考（NV baseline）
    nv_reference_artifact: Optional[str] = None  # 指向 NV reference artifact
    nv_reference_value: Optional[float] = None
    nv_reference_identity: Optional[str] = None  # 模型名+数据集指纹

    # 相对退化计算
    relative_drop: Optional[float] = None  # (nv - candidate) / nv
    qualified: Optional[bool] = None  # relative_drop <= 0.05

    # 详细路径
    detailed_result_file: str = ""


@dataclass
class PerformanceResultArtifact:
    """性能测量结果 Artifact（V3 仅测量，不比较）"""
    metadata: ArtifactMetadata

    # 测试参数
    candidate: str = ""  # v3 / v4
    test_type: Literal["quick", "comprehensive"] = "quick"

    # 结果（绝对值）
    throughput_tokens_per_sec: float = 0.0
    ttft_ms: float = 0.0
    tpot_ms: float = 0.0

    # 详细数据
    concurrency_results: List[Dict[str, Any]] = field(default_factory=list)

    # 测试条件
    input_length: int = 4096
    output_length: int = 1024

    # 详细路径
    detailed_result_file: str = ""

    # V3 性能不做比较，无 ratio 字段
    _meta: Dict[str, str] = field(default_factory=dict)


@dataclass
class ServiceHealthArtifact:
    """服务健康检查 Artifact"""
    metadata: ArtifactMetadata

    # 服务状态
    service_ready: bool = False
    health_check_passed: bool = False

    # 启动信息
    startup_duration_seconds: float = 0.0

    # 错误信息
    error_type: str = ""  # crash / timeout / health_check_failed
    error_message: str = ""

    # 日志路径
    service_log_path: str = ""

    # 运行时配置
    operator_revision_id: str = ""
    runtime_env: Dict[str, str] = field(default_factory=dict)


@dataclass
class DiagnosisResultArtifact:
    """诊断结果 Artifact（来自 diagnose_ops.py 或 Agent）"""
    metadata: ArtifactMetadata

    # 诊断类型
    diagnosis_type: Literal["startup_crash", "accuracy_regression", "unknown_failure"] = "startup_crash"

    # 诊断结果
    suspected_ops: List[Dict[str, Any]] = field(default_factory=list)  # [{name, confidence, evidence}]
    candidate_ops: List[str] = field(default_factory=list)  # 低置信度候选

    # 状态
    diagnosis_status: Literal["conclusive", "candidates_available", "exhausted"] = "exhausted"

    # 证据来源
    evidence_artifacts: List[str] = field(default_factory=list)

    # 是否需要 Agent
    requires_agent: bool = False


@dataclass
class AnalysisResultArtifact:
    """Agent 分析结果 Artifact"""
    metadata: ArtifactMetadata

    # Agent 会话
    agent_session_id: str = ""
    analysis_type: str = ""  # startup_failure / accuracy_regression / unknown_failure

    # 结果状态
    status: Literal["hypothesis_available", "no_hypothesis", "unresolved", "error"] = "unresolved"

    # 假设
    suspected_ops: List[Dict[str, Any]] = field(default_factory=list)
    recommended_experiment: Optional[Dict[str, Any]] = None

    # 验证结果（实验执行后填充）
    verification_status: Optional[Literal["success", "failed", "not_executed"]] = None
    verification_artifact: Optional[str] = None


def compute_artifact_hash(content: Any) -> str:
    """计算 Artifact 内容哈希"""
    content_json = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content_json.encode('utf-8')).hexdigest()


def generate_artifact_id(artifact_type: str, sequence: int) -> str:
    """生成 Artifact ID"""
    return f"art-{artifact_type}-{sequence:03d}"
