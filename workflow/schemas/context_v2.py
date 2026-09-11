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

from dataclasses import dataclass, field, asdict, fields
from typing import List, Dict, Optional, Literal
from datetime import datetime


class ContextValidationError(Exception):
    """Context 写入校验失败（字段越权 / 篡改不可变数据）"""
    pass


@dataclass
class ArtifactReference:
    """Artifact 引用，指向已登记的证据"""
    artifact_id: str  # 格式: art-<type>-<sequence>
    version: int = 1
    registered_at: Optional[str] = None  # ISO 8601


@dataclass
class RuntimeInfo:
    """运行时环境信息"""
    workflow_run_id: str = ""  # 格式: wf-<YYYYMMDD>-<HHMMSS>-<short-hash>
    started_at: str = ""  # ISO 8601
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
        """转换为字典，用于序列化（递归处理所有嵌套 dataclass / Dict / List）"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ContextSchemaV2":
        """从字典反序列化（递归重建所有嵌套 dataclass），保证与 to_dict 等价往返"""
        ctx = cls()
        ctx.schema_version = data.get("schema_version", "2.0")
        ctx.current_revision_id = data.get("current_revision_id", "")
        ctx.current_step_id = data.get("current_step_id", "")
        ctx.registered_artifacts = list(data.get("registered_artifacts", []))
        ctx.recovery = dict(data.get("recovery", {}))
        ctx._meta = dict(data.get("_meta", {}))

        if data.get("runtime"):
            ctx.runtime = _reconstruct_runtime(data["runtime"])

        ctx.operator_revisions = {
            rid: _reconstruct_revision(rd)
            for rid, rd in (data.get("operator_revisions") or {}).items()
        }
        ctx.gates = {
            gid: _reconstruct_gate(gd)
            for gid, gd in (data.get("gates") or {}).items()
        }
        ctx.steps = {
            sid: _reconstruct_step(sd)
            for sid, sd in (data.get("steps") or {}).items()
        }
        return ctx


def _filter_known(d: dict, cls) -> dict:
    """只保留 dataclass `cls` 认识的字段，丢弃其余。

    容忍"混入非本 schema 字段"的输入：例如把它指向旧版 context.yaml（顶层/嵌套键都不同，
    旧 runtime 段带 framework 等字段）时，反序列化应忽略多余字段而不是 TypeError 崩掉。
    缺失字段交给 dataclass 默认值。
    """
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in known}


def _reconstruct_artifact_ref(d: Optional[dict]) -> Optional[ArtifactReference]:
    """重建 Optional[ArtifactReference]"""
    if not d:
        return None
    return ArtifactReference(**_filter_known(d, ArtifactReference))


def _reconstruct_runtime(d: dict) -> RuntimeInfo:
    """重建 RuntimeInfo（含嵌套 ArtifactReference 版本字段）"""
    d = dict(d)
    for k in ("flaggems_version", "flagtree_version", "plugin_version", "vllm_version"):
        if k in d:
            d[k] = _reconstruct_artifact_ref(d[k])
    return RuntimeInfo(**_filter_known(d, RuntimeInfo))


def _reconstruct_revision(d: dict) -> OperatorRevision:
    """重建 OperatorRevision（含嵌套 ArtifactReference）"""
    d = dict(d)
    d["source_artifact"] = _reconstruct_artifact_ref(d.get("source_artifact"))
    d["verification_artifact"] = _reconstruct_artifact_ref(d.get("verification_artifact"))
    return OperatorRevision(**_filter_known(d, OperatorRevision))


def _reconstruct_gate(d: dict) -> Gate:
    """重建 Gate（含嵌套 ArtifactReference）"""
    d = dict(d)
    d["decision_artifact"] = _reconstruct_artifact_ref(d.get("decision_artifact"))
    return Gate(**_filter_known(d, Gate))


def _reconstruct_step(d: dict) -> WorkflowStep:
    """重建 WorkflowStep（字段均为基本类型 / List / Dict）"""
    return WorkflowStep(**_filter_known(d, WorkflowStep))


# 顶层字段白名单（用于写入校验，拒绝 schema 之外的字段）
_TOPLEVEL_FIELDS = {f.name for f in fields(ContextSchemaV2)}
# 已完成步骤不允许回退到的状态集合外的合法终态
_TERMINAL_STEP_STATUSES = {"success"}


def validate_context_dict(new: dict, old: Optional[dict] = None) -> None:
    """写入前校验：字段越权 + 篡改不可变数据。不合法抛 ContextValidationError。

    这是 YAML 阶段的「字段权限 / append-only」雏形——存储层无强制约束时，
    由引擎在写入咽喉处校验（见 memory state-storage-decision）。

    Args:
        new: 即将写入的 context dict
        old: 磁盘上已有的 context dict（首次写入为 None）
    """
    # --- 结构校验 ---
    if not isinstance(new, dict):
        raise ContextValidationError(f"context 必须是 dict，实际 {type(new).__name__}")

    extra = set(new.keys()) - _TOPLEVEL_FIELDS
    if extra:
        raise ContextValidationError(f"禁止写入 schema 外的顶层字段: {sorted(extra)}")

    if new.get("schema_version") != "2.0":
        raise ContextValidationError(
            f"schema_version 必须为 '2.0'，实际 {new.get('schema_version')!r}"
        )

    if old is None:
        return

    # --- 不可篡改：已冻结的 operator revision 内容不得变更 ---
    old_revs = old.get("operator_revisions") or {}
    new_revs = new.get("operator_revisions") or {}
    for rid, orev in old_revs.items():
        if orev.get("frozen"):
            if rid not in new_revs:
                raise ContextValidationError(f"禁止删除已冻结的 revision: {rid}")
            if new_revs[rid] != orev:
                raise ContextValidationError(f"禁止修改已冻结的 revision: {rid}")

    # --- 不可篡改：已 success 的步骤不得回退状态 ---
    old_steps = old.get("steps") or {}
    new_steps = new.get("steps") or {}
    for sid, ostep in old_steps.items():
        if ostep.get("status") in _TERMINAL_STEP_STATUSES:
            nstep = new_steps.get(sid)
            if nstep is None:
                raise ContextValidationError(f"禁止删除已完成的步骤: {sid}")
            if nstep.get("status") not in _TERMINAL_STEP_STATUSES:
                raise ContextValidationError(
                    f"禁止将已完成步骤 {sid} 回退为 {nstep.get('status')!r}"
                )
