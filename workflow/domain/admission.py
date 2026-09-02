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

"""Plugin-only Admission - 全组件准入检测

Plugin-only 工作流的准入约束：
1. 准入镜像必须包含全部组件：vLLM + FlagGems + FlagTree + vllm-plugin-FL
2. 运行时固定为 VLLM_PLUGINS=fl, USE_FLAGGEMS=1
3. 组件缺失时 fail-closed（拒绝准入，不降级、不近似 Native）
4. 不通过关闭组件逼近原生环境
"""

import logging
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

from ..artifacts.registry import ArtifactRegistry


# Plugin-only 要求的组件
REQUIRED_COMPONENTS = ["vllm", "flaggems", "flagtree", "vllm_plugin"]

# 固定运行时环境
FIXED_RUNTIME_ENV = {
    "VLLM_PLUGINS": "fl",
    "USE_FLAGGEMS": "1",
}


class AdmissionResult:
    """准入检测结果"""

    def __init__(self):
        self.admitted: bool = False
        self.entry_image_type: str = "unknown"

        # 组件检测
        self.components: Dict[str, Dict[str, Any]] = {}  # {component: {installed, version}}
        self.missing_components: List[str] = []

        # 运行时
        self.runtime_env: Dict[str, str] = {}

        # 原因
        self.reason: str = ""
        self.admission_errors: List[str] = []

        # Artifact
        self.admission_artifact_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "entry_image_type": self.entry_image_type,
            "components": self.components,
            "missing_components": self.missing_components,
            "runtime_env": self.runtime_env,
            "reason": self.reason,
            "admission_errors": self.admission_errors,
            "admission_artifact_id": self.admission_artifact_id,
        }


class PluginOnlyAdmission:
    """Plugin-only 全组件准入检测器"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        artifact_registry: Optional[ArtifactRegistry] = None,
    ):
        self.workspace_root = workspace_root
        self.artifact_registry = artifact_registry or ArtifactRegistry(workspace_root)
        self.logger = logging.getLogger("workflow.domain.admission")

    def check_admission(self, capabilities: Dict[str, Any]) -> AdmissionResult:
        """检测准入资格

        Args:
            capabilities: inspect_env.py 输出的能力探测结果
                {
                    "flaggems_installed": bool,
                    "flaggems_version": str,
                    "vllm_plugin_installed": bool,
                    "plugin_version": str,
                    "vllm_version": str,
                    "flagtree": {"installed": bool, "version": str},
                }

        Returns:
            AdmissionResult
        """
        result = AdmissionResult()

        # 提取各组件状态
        components = self._extract_components(capabilities)
        result.components = components

        # 检查缺失组件
        missing = [
            comp for comp in REQUIRED_COMPONENTS
            if not components.get(comp, {}).get("installed", False)
        ]
        result.missing_components = missing

        # Plugin-only 准入判定：全组件必须存在
        if missing:
            # Fail-closed：组件缺失拒绝准入
            result.admitted = False
            result.entry_image_type = "unknown"
            result.reason = (
                f"Plugin-only 准入失败：缺失组件 {missing}。"
                f"准入镜像必须包含全部组件（{REQUIRED_COMPONENTS}），"
                f"不通过关闭组件逼近原生环境。"
            )
            result.admission_errors.append(f"missing components: {missing}")

            self.logger.error(result.reason)
            return result

        # 全组件存在 → 准入成功
        result.admitted = True
        result.entry_image_type = "gems_tree_plugin"
        result.runtime_env = FIXED_RUNTIME_ENV.copy()
        result.reason = (
            f"Plugin-only 准入成功：全组件存在。"
            f"运行时固定为 VLLM_PLUGINS=fl, USE_FLAGGEMS=1。"
        )

        self.logger.info(result.reason)
        return result

    def _extract_components(self, capabilities: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """提取各组件的安装状态和版本

        Args:
            capabilities: 能力探测结果

        Returns:
            {component: {installed, version}}
        """
        components = {}

        # vLLM
        vllm_version = capabilities.get("vllm_version", "")
        components["vllm"] = {
            "installed": bool(vllm_version),
            "version": vllm_version,
        }

        # FlagGems
        components["flaggems"] = {
            "installed": capabilities.get("flaggems_installed", False),
            "version": capabilities.get("flaggems_version", ""),
        }

        # FlagTree
        flagtree = capabilities.get("flagtree", {})
        components["flagtree"] = {
            "installed": bool(flagtree.get("installed", False)),
            "version": flagtree.get("version", ""),
        }

        # vllm-plugin-FL
        components["vllm_plugin"] = {
            "installed": capabilities.get("vllm_plugin_installed", False),
            "version": capabilities.get("plugin_version", ""),
        }

        return components

    def register_admission_artifact(
        self,
        result: AdmissionResult,
        file_path: str,
    ) -> str:
        """登记准入结果为 Artifact

        Args:
            result: 准入结果
            file_path: 结果文件路径

        Returns:
            artifact_id
        """
        artifact_id = self.artifact_registry.register_artifact(
            artifact_type="admission-result",
            content=result.to_dict(),
            file_path=file_path,
            generated_by="script",
            generator_version="plugin_only_admission_1.0",
            tags={
                "entry_image_type": result.entry_image_type,
                "admitted": str(result.admitted),
            },
        )

        result.admission_artifact_id = artifact_id
        return artifact_id

    def validate_component_identity(
        self,
        components: Dict[str, Dict[str, Any]],
        expected_versions: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, List[str]]:
        """验证组件版本身份（可选，用于严格校验）

        Args:
            components: 组件状态
            expected_versions: 期望的版本（如果提供）

        Returns:
            (是否通过, 错误列表)
        """
        errors = []

        if not expected_versions:
            # 无期望版本，只检查版本非空
            for comp in REQUIRED_COMPONENTS:
                version = components.get(comp, {}).get("version", "")
                if not version:
                    errors.append(f"{comp}: version unknown")
            return len(errors) == 0, errors

        # 有期望版本，严格比对
        for comp, expected in expected_versions.items():
            actual = components.get(comp, {}).get("version", "")
            if actual != expected:
                errors.append(f"{comp}: expected {expected}, got {actual}")

        return len(errors) == 0, errors
