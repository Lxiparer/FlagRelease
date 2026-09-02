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

"""Context Schema v2 - Artifact-backed workflow state

所有业务事实必须引用已登记的 Artifact，不能是自由文本推断。
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Literal
from datetime import datetime


@dataclass
class ArtifactReference:
    """Artifact 引用，指向已登记的证据"""
    artifact_id: str  # 格式: art-<type>-<sequence>
    version: int = 1
    registered_at: Optional[str] = None  # ISO 8601


@dataclass
class RuntimeInfo:
    """运行时环境信息"""
    workflow_run_id: str  # 格式: wf-<YYYYMMDD>-<HHMMSS>-<short-hash>
    started_at: str  # ISO 8601
    finished_at: Optional[str] = None

    # 容器和模型
    container_name: str = ""
    container_id: str = ""
    model_name: str = ""
    model_path: str = ""

    # GPU 信息
    gpu_vendor: str = ""  # nvidia/iluvatar/ascend/...
    gpu_model: str = ""
    gpu_count: int = 0
    gpu_devices: List[str] = field(default_factory=list)
    gpu_count_locked: bool = False  # 首次启动后锁定卡数

    # 组件版本（基于 Artifact）
    flaggems_version: Optional[ArtifactReference] = None
    flagtree_version: Optional[ArtifactReference] = None
    plugin_version: Optional[ArtifactReference] = None
    vllm_version: Optional[ArtifactReference] = None

    # 准入镜像类型
    entry_image_type: Literal["gems_tree_plugin", "unknown"] = "unknown"

    # 代理和网络
    proxy_list: List[str] = field(default_factory=list)
    active_proxy: str = ""


@dataclass
class OperatorRevision:
    """不可变算子配置版本"""
    revision_id: str  # v3-discovered / v3-startup-r1 / v3-accuracy-r2 / v3-final / v4-r1 / v4-final
    parent_revision_id: Optional[str] = None
    created_at: str = ""  # ISO 8601

    # 算子集合
    enabled_ops: List[str] = field(default_factory=list)
    disabled_ops: Dict[str, str] = field(default_factory=dict)  # {op_name: reason}

    # 禁用原因分类
    disable_reason_categories: Dict[str, List[str]] = field(default_factory=dict)  # {startup: [...], accuracy: [...], v4_performance: [...]}

    # 来源证据
    source_artifact: Optional[ArtifactReference] = None  # runtime oplist Artifact

    # 验证状态
    verified: bool = False
    verification_artifact: Optional[ArtifactReference] = None

    # 元数据
    frozen: bool = False  # v3-final 和 v4-final 冻结后不可修改
    _meta: Dict[str, str] = field(default_factory=dict)


@dataclass
class Gate:
    """业务闸门，基于 Artifact 归约结果"""
    gate_id: str  # accuracy.qualified / v3.established / v4.established
    status: Literal["pending", "passed", "failed", "unresolved"] = "pending"

    # 判定依据（必须是 Artifact）
    required_artifacts: List[str] = field(default_factory=list)
    decision_artifact: Optional[ArtifactReference] = None

    # 判定逻辑（描述性，实际逻辑在 gate reducer 中）
    criteria: str = ""

    # 结果
    evaluated_at: Optional[str] = None
    reason: str = ""
    _meta: Dict[str, str] = field(default_factory=dict)


@dataclass
class WorkflowStep:
    """工作流步骤状态"""
    step_id: str  # 01_container_preparation / 02_admission / ... / 15_finalize
    step_name: str
    status: Literal["pending", "running", "success", "failed", "skipped"] = "pending"

    # 时间
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None

    # 产出 Artifacts
    output_artifacts: List[str] = field(default_factory=list)

    # 依赖 Gates
    required_gates: List[str] = field(default_factory=list)
    gates_passed: bool = False

    # 失败信息
    fail_reason: str = ""
    skip_reason: str = ""

    # Agent 介入记录
    agent_sessions: List[str] = field(default_factory=list)  # [agent_session_id, ...]

    _meta: Dict[str, str] = field(default_factory=dict)


@dataclass
class ContextSchemaV2:
    """Context Schema v2 - 完整工作流状态"""
    schema_version: str = "2.0"

    # 运行时信息
    runtime: RuntimeInfo = field(default_factory=RuntimeInfo)

    # Operator revisions（不可变版本链）
    operator_revisions: Dict[str, OperatorRevision] = field(default_factory=dict)
    current_revision_id: str = ""  # 当前活跃的 revision

    # Gates（业务闸门）
    gates: Dict[str, Gate] = field(default_factory=dict)

    # 工作流步骤
    steps: Dict[str, WorkflowStep] = field(default_factory=dict)
    current_step_id: str = ""

    # 已登记的 Artifacts（ID 列表，详细内容在 artifact registry）
    registered_artifacts: List[str] = field(default_factory=list)

    # 恢复点信息
    recovery: Dict[str, str] = field(default_factory=dict)

    # 全局元数据
    _meta: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转换为字典，用于序列化"""
        # 这里简化实现，实际需要递归处理所有 dataclass
        return {
            "schema_version": self.schema_version,
            "runtime": self.runtime.__dict__,
            "operator_revisions": {k: v.__dict__ for k, v in self.operator_revisions.items()},
            "current_revision_id": self.current_revision_id,
            "gates": {k: v.__dict__ for k, v in self.gates.items()},
            "steps": {k: v.__dict__ for k, v in self.steps.items()},
            "current_step_id": self.current_step_id,
            "registered_artifacts": self.registered_artifacts,
            "recovery": self.recovery,
            "_meta": self._meta,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContextSchemaV2":
        """从字典反序列化"""
        # 简化实现，实际需要递归构造所有 dataclass
        ctx = cls()
        ctx.schema_version = data.get("schema_version", "2.0")
        # ... 完整实现需要处理所有嵌套结构
        return ctx
