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

"""Plugin-only Admission 单元测试"""

import unittest
import sys
import tempfile
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.domain.admission import (
    PluginOnlyAdmission,
    AdmissionResult,
    REQUIRED_COMPONENTS,
    FIXED_RUNTIME_ENV,
)


class TestPluginOnlyAdmission(unittest.TestCase):
    """Plugin-only 准入检测测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.admission = PluginOnlyAdmission(workspace_root=self.tmpdir)

    def _full_capabilities(self):
        """全组件都存在的能力探测结果"""
        return {
            "vllm_version": "0.7.3",
            "flaggems_installed": True,
            "flaggems_version": "5.1.0",
            "vllm_plugin_installed": True,
            "plugin_version": "1.2.3",
            "flagtree": {"installed": True, "version": "0.5.0"},
        }

    def test_full_components_admitted(self):
        """全组件存在时准入成功"""
        result = self.admission.check_admission(self._full_capabilities())

        self.assertTrue(result.admitted)
        self.assertEqual(result.entry_image_type, "gems_tree_plugin")
        self.assertEqual(result.missing_components, [])
        self.assertEqual(result.runtime_env, FIXED_RUNTIME_ENV)

    def test_missing_flaggems_rejected(self):
        """缺失 FlagGems 时 fail-closed 拒绝准入"""
        caps = self._full_capabilities()
        caps["flaggems_installed"] = False

        result = self.admission.check_admission(caps)

        self.assertFalse(result.admitted)
        self.assertEqual(result.entry_image_type, "unknown")
        self.assertIn("flaggems", result.missing_components)

    def test_missing_plugin_rejected(self):
        """缺失 plugin 时 fail-closed 拒绝准入（不降级到分支 A）"""
        caps = self._full_capabilities()
        caps["vllm_plugin_installed"] = False

        result = self.admission.check_admission(caps)

        self.assertFalse(result.admitted)
        self.assertEqual(result.entry_image_type, "unknown")
        self.assertIn("vllm_plugin", result.missing_components)

    def test_missing_flagtree_rejected(self):
        """缺失 FlagTree 时 fail-closed 拒绝准入"""
        caps = self._full_capabilities()
        caps["flagtree"] = {"installed": False, "version": ""}

        result = self.admission.check_admission(caps)

        self.assertFalse(result.admitted)
        self.assertIn("flagtree", result.missing_components)

    def test_missing_vllm_rejected(self):
        """缺失 vLLM 时 fail-closed 拒绝准入"""
        caps = self._full_capabilities()
        caps["vllm_version"] = ""

        result = self.admission.check_admission(caps)

        self.assertFalse(result.admitted)
        self.assertIn("vllm", result.missing_components)

    def test_native_image_rejected(self):
        """纯 native 镜像（无任何 flag 组件）拒绝准入"""
        caps = {
            "vllm_version": "0.7.3",
            "flaggems_installed": False,
            "vllm_plugin_installed": False,
            "flagtree": {"installed": False},
        }

        result = self.admission.check_admission(caps)

        self.assertFalse(result.admitted)
        # 应该缺失 flaggems, flagtree, vllm_plugin
        self.assertIn("flaggems", result.missing_components)
        self.assertIn("flagtree", result.missing_components)
        self.assertIn("vllm_plugin", result.missing_components)

    def test_gems_tree_without_plugin_rejected(self):
        """gems_tree 镜像（无 plugin）在 Plugin-only 下拒绝准入"""
        caps = {
            "vllm_version": "0.7.3",
            "flaggems_installed": True,
            "flaggems_version": "5.1.0",
            "vllm_plugin_installed": False,
            "flagtree": {"installed": True, "version": "0.5.0"},
        }

        result = self.admission.check_admission(caps)

        # Plugin-only 要求全组件，缺 plugin 就拒绝
        self.assertFalse(result.admitted)
        self.assertEqual(result.missing_components, ["vllm_plugin"])

    def test_components_versions_captured(self):
        """准入成功时正确捕获组件版本"""
        result = self.admission.check_admission(self._full_capabilities())

        self.assertEqual(result.components["vllm"]["version"], "0.7.3")
        self.assertEqual(result.components["flaggems"]["version"], "5.1.0")
        self.assertEqual(result.components["flagtree"]["version"], "0.5.0")
        self.assertEqual(result.components["vllm_plugin"]["version"], "1.2.3")

    def test_required_components_definition(self):
        """验证 Plugin-only 要求的组件定义"""
        self.assertEqual(
            set(REQUIRED_COMPONENTS),
            {"vllm", "flaggems", "flagtree", "vllm_plugin"}
        )

    def test_fixed_runtime_env_definition(self):
        """验证固定运行时环境定义"""
        self.assertEqual(FIXED_RUNTIME_ENV["VLLM_PLUGINS"], "fl")
        self.assertEqual(FIXED_RUNTIME_ENV["USE_FLAGGEMS"], "1")


if __name__ == "__main__":
    unittest.main()
