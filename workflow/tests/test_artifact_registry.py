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

"""Artifact Registry 单元测试"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.artifacts.registry import ArtifactRegistry


class TestArtifactRegistry(unittest.TestCase):
    """Artifact Registry 测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.registry = ArtifactRegistry(self.tmpdir)
        os.makedirs(os.path.join(self.tmpdir, "results"), exist_ok=True)

    def test_register_and_query_artifact(self):
        """注册并查询 Artifact"""
        content = {"test": "data", "value": 42}
        file_path = "results/test.json"

        abs_path = os.path.join(self.tmpdir, file_path)
        with open(abs_path, "w") as f:
            json.dump(content, f)

        artifact_id = self.registry.register_artifact(
            artifact_type="test-result",
            content=content,
            file_path=file_path,
            generated_by="test",
            tags={"category": "unit-test"},
        )

        self.assertIsNotNone(artifact_id)
        self.assertTrue(artifact_id.startswith("art-"))

        # 查询
        results = self.registry.query_artifacts(
            artifact_type="test-result",
            tags={"category": "unit-test"},
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], artifact_id)

    def test_load_artifact_content(self):
        """加载 Artifact 内容"""
        content = {"key": "value", "number": 123}
        file_path = "results/content_test.json"

        abs_path = os.path.join(self.tmpdir, file_path)
        with open(abs_path, "w") as f:
            json.dump(content, f)

        artifact_id = self.registry.register_artifact(
            artifact_type="test-result",
            content=content,
            file_path=file_path,
            generated_by="test",
        )

        # 加载内容
        loaded = self.registry.load_artifact_content(artifact_id)

        self.assertEqual(loaded["key"], "value")
        self.assertEqual(loaded["number"], 123)

    def test_verify_artifact_integrity(self):
        """验证 Artifact 完整性"""
        content = {"data": "original"}
        file_path = "results/integrity_test.json"

        abs_path = os.path.join(self.tmpdir, file_path)
        with open(abs_path, "w") as f:
            json.dump(content, f)

        artifact_id = self.registry.register_artifact(
            artifact_type="test-result",
            content=content,
            file_path=file_path,
            generated_by="test",
        )

        # 验证应该通过
        valid = self.registry.verify_artifact(artifact_id)
        self.assertTrue(valid)

        # 修改文件内容（破坏完整性）
        with open(abs_path, "w") as f:
            json.dump({"data": "tampered"}, f)

        # 验证应该失败
        valid = self.registry.verify_artifact(artifact_id)
        self.assertFalse(valid)

    def test_query_with_multiple_filters(self):
        """使用多个过滤器查询"""
        # 注册多个 Artifacts
        for i in range(3):
            content = {"index": i}
            file_path = f"results/multi_{i}.json"

            abs_path = os.path.join(self.tmpdir, file_path)
            with open(abs_path, "w") as f:
                json.dump(content, f)

            self.registry.register_artifact(
                artifact_type="test-result" if i < 2 else "other-result",
                content=content,
                file_path=file_path,
                generated_by="script" if i == 0 else "agent",
                tags={"batch": "A" if i < 2 else "B"},
            )

        # 按类型查询
        results = self.registry.query_artifacts(artifact_type="test-result")
        self.assertEqual(len(results), 2)

        # 按生成者查询
        results = self.registry.query_artifacts(generated_by="agent")
        self.assertEqual(len(results), 2)

        # 按标签查询
        results = self.registry.query_artifacts(tags={"batch": "A"})
        self.assertEqual(len(results), 2)

        # 组合查询
        results = self.registry.query_artifacts(
            artifact_type="test-result",
            generated_by="agent",
        )
        self.assertEqual(len(results), 1)

    def test_missing_artifact_returns_none(self):
        """查询不存在的 Artifact 返回 None"""
        content = self.registry.load_artifact_content("art-nonexistent-123")
        self.assertIsNone(content)

    def test_verify_nonexistent_artifact_returns_false(self):
        """验证不存在的 Artifact 返回 False"""
        valid = self.registry.verify_artifact("art-nonexistent-456")
        self.assertFalse(valid)


if __name__ == "__main__":
    unittest.main()
