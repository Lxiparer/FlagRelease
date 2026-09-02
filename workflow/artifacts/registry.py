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

"""Artifact Registry - 证据登记和查询中心

职责：
1. 登记所有产生的 Artifact
2. 验证 Artifact 完整性（哈希、schema）
3. 提供查询接口
4. 持久化到磁盘
"""

import os
import json
from typing import Dict, List, Optional, Any
from datetime import datetime
from pathlib import Path

from .artifact_schema import (
    ArtifactMetadata,
    RuntimeOplistArtifact,
    AccuracyResultArtifact,
    PerformanceResultArtifact,
    ServiceHealthArtifact,
    DiagnosisResultArtifact,
    AnalysisResultArtifact,
    compute_artifact_hash,
    generate_artifact_id,
)


class ArtifactRegistry:
    """Artifact 注册中心"""

    def __init__(self, workspace_root: str = "/flagos-workspace"):
        self.workspace_root = Path(workspace_root)
        self.registry_file = self.workspace_root / "artifacts" / "registry.json"
        self.artifacts_dir = self.workspace_root / "artifacts"

        # 内存索引
        self.artifacts: Dict[str, Dict[str, Any]] = {}
        self.sequence_counters: Dict[str, int] = {}

        # 确保目录存在
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        # 加载已有 registry
        self._load_registry()

    def _load_registry(self):
        """从磁盘加载 registry"""
        if self.registry_file.exists():
            with open(self.registry_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.artifacts = data.get('artifacts', {})
                self.sequence_counters = data.get('sequence_counters', {})

    def _save_registry(self):
        """保存 registry 到磁盘"""
        data = {
            'artifacts': self.artifacts,
            'sequence_counters': self.sequence_counters,
            'last_updated': datetime.now().isoformat(),
        }
        with open(self.registry_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def register_artifact(
        self,
        artifact_type: str,
        content: Any,
        file_path: str,
        generated_by: str = "script",
        generator_version: str = "",
        depends_on: List[str] = None,
        tags: Dict[str, str] = None,
        **metadata_kwargs
    ) -> str:
        """登记新 Artifact

        Args:
            artifact_type: 类型（runtime-oplist / accuracy-result / ...）
            content: Artifact 内容（dict 或 dataclass）
            file_path: 文件路径（相对于 workspace_root）
            generated_by: 生成者
            generator_version: 生成者版本
            depends_on: 依赖的 artifact IDs
            tags: 标签
            **metadata_kwargs: 额外的元数据字段

        Returns:
            artifact_id
        """
        # 生成 ID
        if artifact_type not in self.sequence_counters:
            self.sequence_counters[artifact_type] = 0
        self.sequence_counters[artifact_type] += 1
        artifact_id = generate_artifact_id(artifact_type, self.sequence_counters[artifact_type])

        # 计算哈希
        content_hash = compute_artifact_hash(content)

        # 获取文件大小
        full_path = self.workspace_root / file_path
        content_size = full_path.stat().st_size if full_path.exists() else 0

        # 构造元数据
        metadata = ArtifactMetadata(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            version=1,
            generated_by=generated_by,
            generator_version=generator_version,
            created_at=datetime.now().isoformat(),
            content_hash=content_hash,
            content_size=content_size,
            file_path=file_path,
            depends_on=depends_on or [],
            tags=tags or {},
            _meta=metadata_kwargs,
        )

        # 登记
        self.artifacts[artifact_id] = {
            'metadata': metadata.__dict__,
            'content_summary': self._summarize_content(content),
        }

        # 持久化
        self._save_registry()

        return artifact_id

    def _summarize_content(self, content: Any) -> Dict[str, Any]:
        """提取内容摘要（用于快速查询，不存储完整内容）"""
        if isinstance(content, dict):
            # 提取关键字段
            summary = {}
            for key in ['operators', 'accuracy', 'throughput_tokens_per_sec', 'service_ready',
                       'suspected_ops', 'status', 'dataset', 'candidate']:
                if key in content:
                    summary[key] = content[key]
            return summary
        return {}

    def get_artifact(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        """获取 Artifact 元数据（不加载完整内容）"""
        return self.artifacts.get(artifact_id)

    def load_artifact_content(self, artifact_id: str) -> Optional[Any]:
        """加载 Artifact 完整内容"""
        artifact = self.get_artifact(artifact_id)
        if not artifact:
            return None

        file_path = artifact['metadata']['file_path']
        full_path = self.workspace_root / file_path

        if not full_path.exists():
            return None

        with open(full_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def verify_artifact(self, artifact_id: str) -> bool:
        """验证 Artifact 完整性（文件存在 + 哈希匹配）"""
        artifact = self.get_artifact(artifact_id)
        if not artifact:
            return False

        file_path = artifact['metadata']['file_path']
        full_path = self.workspace_root / file_path

        if not full_path.exists():
            return False

        # 重新计算哈希
        content = self.load_artifact_content(artifact_id)
        if content is None:
            return False

        current_hash = compute_artifact_hash(content)
        stored_hash = artifact['metadata']['content_hash']

        return current_hash == stored_hash

    def query_artifacts(
        self,
        artifact_type: Optional[str] = None,
        generated_by: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        depends_on: Optional[str] = None,
    ) -> List[str]:
        """查询 Artifacts

        Returns:
            匹配的 artifact IDs
        """
        results = []

        for artifact_id, artifact in self.artifacts.items():
            metadata = artifact['metadata']

            # 类型过滤
            if artifact_type and metadata['artifact_type'] != artifact_type:
                continue

            # 生成者过滤
            if generated_by and metadata['generated_by'] != generated_by:
                continue

            # 标签过滤
            if tags:
                artifact_tags = metadata.get('tags', {})
                if not all(artifact_tags.get(k) == v for k, v in tags.items()):
                    continue

            # 依赖过滤
            if depends_on and depends_on not in metadata.get('depends_on', []):
                continue

            results.append(artifact_id)

        return results

    def get_latest_artifact(self, artifact_type: str, **query_kwargs) -> Optional[str]:
        """获取最新的指定类型 Artifact"""
        artifacts = self.query_artifacts(artifact_type=artifact_type, **query_kwargs)
        if not artifacts:
            return None

        # 按创建时间排序
        artifacts_with_time = [
            (aid, self.artifacts[aid]['metadata']['created_at'])
            for aid in artifacts
        ]
        artifacts_with_time.sort(key=lambda x: x[1], reverse=True)

        return artifacts_with_time[0][0]
