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

"""Operator Revision Store - 不可变算子配置版本管理

核心原则：
1. 每个 revision 不可变（创建后不能修改 enabled_ops）
2. 新配置通过创建 child revision 实现
3. 禁用原因分 startup / accuracy / v4_performance 三类
4. v3-final 和 v4-final 冻结后不能派生非 v4 的子版本
"""

from typing import Dict, List, Optional, Tuple
from datetime import datetime
import logging

from ..schemas.context_v2 import OperatorRevision, ArtifactReference


class OperatorRevisionStore:
    """算子版本管理器"""

    def __init__(self):
        self.revisions: Dict[str, OperatorRevision] = {}
        self.logger = logging.getLogger("workflow.operator_revision")

    def create_revision(
        self,
        revision_id: str,
        enabled_ops: List[str],
        parent_revision_id: Optional[str] = None,
        additional_disabled: Dict[str, str] = None,
        source_artifact_id: Optional[str] = None,
    ) -> OperatorRevision:
        """创建新 revision

        Args:
            revision_id: 版本 ID
            enabled_ops: 启用的算子列表
            parent_revision_id: 父版本 ID
            additional_disabled: 额外禁用的算子及原因
            source_artifact_id: 来源 Artifact ID

        Returns:
            新创建的 OperatorRevision
        """
        if revision_id in self.revisions:
            raise ValueError(f"Revision already exists: {revision_id}")

        # 验证父版本
        if parent_revision_id:
            if parent_revision_id not in self.revisions:
                raise ValueError(f"Parent revision not found: {parent_revision_id}")

            parent = self.revisions[parent_revision_id]

            # 检查父版本是否冻结且禁止派生
            if parent.frozen:
                # v3-final 只能派生 v4-*
                if parent.revision_id == "v3-final" and not revision_id.startswith("v4-"):
                    raise ValueError("v3-final can only derive v4-* revisions")

                # v4-final 不能派生任何子版本
                if parent.revision_id == "v4-final":
                    raise ValueError("v4-final is frozen and cannot derive child revisions")

        # 继承父版本的禁用列表
        disabled_ops = {}
        disable_reason_categories = {"startup": [], "accuracy": [], "v4_performance": []}

        if parent_revision_id:
            parent = self.revisions[parent_revision_id]
            disabled_ops = parent.disabled_ops.copy()
            disable_reason_categories = {
                k: v.copy() for k, v in parent.disable_reason_categories.items()
            }

        # 添加新禁用
        if additional_disabled:
            for op_name, reason in additional_disabled.items():
                disabled_ops[op_name] = reason

                # 分类（基于 reason 字符串）
                reason_lower = reason.lower()
                if "startup" in reason_lower or "crash" in reason_lower:
                    if op_name not in disable_reason_categories["startup"]:
                        disable_reason_categories["startup"].append(op_name)
                elif "accuracy" in reason_lower:
                    if op_name not in disable_reason_categories["accuracy"]:
                        disable_reason_categories["accuracy"].append(op_name)
                elif "performance" in reason_lower or "v4" in reason_lower:
                    if op_name not in disable_reason_categories["v4_performance"]:
                        disable_reason_categories["v4_performance"].append(op_name)

        # 创建 revision
        revision = OperatorRevision(
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            created_at=datetime.now().isoformat(),
            enabled_ops=enabled_ops.copy(),  # 不可变，拷贝一份
            disabled_ops=disabled_ops,
            disable_reason_categories=disable_reason_categories,
            verified=False,
            frozen=False,
        )

        if source_artifact_id:
            revision.source_artifact = ArtifactReference(
                artifact_id=source_artifact_id,
                registered_at=datetime.now().isoformat(),
            )

        # 保存
        self.revisions[revision_id] = revision

        self.logger.info(
            f"Created revision {revision_id}: {len(enabled_ops)} enabled, "
            f"{len(disabled_ops)} disabled"
        )

        return revision

    def get_revision(self, revision_id: str) -> Optional[OperatorRevision]:
        """获取指定 revision"""
        return self.revisions.get(revision_id)

    def verify_revision(
        self,
        revision_id: str,
        verification_artifact_id: str,
    ):
        """标记 revision 为已验证

        Args:
            revision_id: 版本 ID
            verification_artifact_id: 验证结果 Artifact ID
        """
        revision = self.revisions.get(revision_id)
        if not revision:
            raise ValueError(f"Revision not found: {revision_id}")

        revision.verified = True
        revision.verification_artifact = ArtifactReference(
            artifact_id=verification_artifact_id,
            registered_at=datetime.now().isoformat(),
        )

        self.logger.info(f"Revision {revision_id} verified with artifact {verification_artifact_id}")

    def freeze_revision(self, revision_id: str):
        """冻结 revision（v3-final / v4-final）

        Args:
            revision_id: 版本 ID
        """
        revision = self.revisions.get(revision_id)
        if not revision:
            raise ValueError(f"Revision not found: {revision_id}")

        if revision.frozen:
            self.logger.warning(f"Revision {revision_id} already frozen")
            return

        revision.frozen = True
        self.logger.info(f"Revision {revision_id} frozen")

    def get_revision_chain(self, revision_id: str) -> List[str]:
        """获取 revision 的完整继承链

        Args:
            revision_id: 版本 ID

        Returns:
            从根到当前 revision 的 ID 列表
        """
        chain = []
        current_id = revision_id

        while current_id:
            chain.append(current_id)
            revision = self.revisions.get(current_id)
            if not revision:
                break
            current_id = revision.parent_revision_id

        chain.reverse()
        return chain

    def get_disabled_ops_by_category(
        self,
        revision_id: str,
    ) -> Dict[str, List[str]]:
        """获取按类别分组的禁用算子

        Args:
            revision_id: 版本 ID

        Returns:
            {category: [op_names]}
        """
        revision = self.revisions.get(revision_id)
        if not revision:
            return {"startup": [], "accuracy": [], "v4_performance": []}

        return revision.disable_reason_categories.copy()

    def compute_diff(
        self,
        base_revision_id: str,
        target_revision_id: str,
    ) -> Dict[str, List[str]]:
        """计算两个 revision 之间的差异

        Args:
            base_revision_id: 基准版本
            target_revision_id: 目标版本

        Returns:
            {
                "added_enabled": [...],
                "removed_enabled": [...],
                "newly_disabled": [...],
            }
        """
        base = self.revisions.get(base_revision_id)
        target = self.revisions.get(target_revision_id)

        if not base or not target:
            return {"added_enabled": [], "removed_enabled": [], "newly_disabled": []}

        base_enabled = set(base.enabled_ops)
        target_enabled = set(target.enabled_ops)

        base_disabled = set(base.disabled_ops.keys())
        target_disabled = set(target.disabled_ops.keys())

        return {
            "added_enabled": list(target_enabled - base_enabled),
            "removed_enabled": list(base_enabled - target_enabled),
            "newly_disabled": list(target_disabled - base_disabled),
        }

    def export_to_dict(self) -> Dict[str, dict]:
        """导出所有 revisions 为字典（用于序列化）

        Returns:
            {revision_id: revision_dict}
        """
        return {
            revision_id: revision.__dict__
            for revision_id, revision in self.revisions.items()
        }

    def import_from_dict(self, data: Dict[str, dict]):
        """从字典导入 revisions

        Args:
            data: {revision_id: revision_dict}
        """
        for revision_id, revision_data in data.items():
            revision = OperatorRevision(**revision_data)
            self.revisions[revision_id] = revision

        self.logger.info(f"Imported {len(data)} revisions")
