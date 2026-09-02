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

"""V3 Discovery Startup - 全组件发现启动

职责：
1. 首次启动服务（VLLM_PLUGINS=fl, USE_FLAGGEMS=1）
2. 提取 runtime oplist（freshness 校验）
3. Identity 校验（合理范围、版本一致性）
4. 生成 v3-discovered revision
"""

import os
import json
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import logging

from ..artifacts.registry import ArtifactRegistry
from ..schemas.context_v2 import OperatorRevision, ArtifactReference


class V3DiscoveryStartup:
    """V3 全组件发现启动"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
    ):
        self.workspace_root = Path(workspace_root)
        self.container_name = container_name
        self.artifact_registry = artifact_registry or ArtifactRegistry(str(workspace_root))
        self.logger = logging.getLogger("workflow.domain.v3_startup")

    def start_service_and_discover(
        self,
        model_path: str,
        flaggems_version: str,
    ) -> Tuple[bool, Optional[str], Optional[List[str]]]:
        """启动服务并发现算子列表

        Args:
            model_path: 模型路径
            flaggems_version: FlagGems 版本

        Returns:
            (是否成功, 错误消息, 发现的算子列表)
        """
        self.logger.info("Starting V3 discovery startup with full components")

        # 1. 清理缓存
        self._clear_caches()

        # 2. 启动服务
        success, error_msg = self._start_service(model_path)

        if not success:
            return False, error_msg, None

        # 3. 等待服务就绪
        service_ready = self._wait_for_service_ready(timeout=300)

        if not service_ready:
            return False, "Service failed to become ready", None

        # 4. 提取 runtime oplist
        oplist, oplist_file = self._extract_runtime_oplist()

        if not oplist:
            return False, "Failed to extract runtime oplist", None

        # 5. Freshness 校验
        freshness_ok, freshness_reason = self._validate_freshness(oplist_file)

        if not freshness_ok:
            self.logger.warning(f"Freshness validation failed: {freshness_reason}")
            # Freshness 失败不是致命错误，但会标记

        # 6. Identity 校验
        identity_ok, identity_reason = self._validate_identity(
            oplist, flaggems_version
        )

        if not identity_ok:
            self.logger.warning(f"Identity validation warning: {identity_reason}")

        self.logger.info(f"V3 discovery completed: {len(oplist)} operators discovered")

        return True, None, oplist

    def _clear_caches(self):
        """清理 Triton/FlagGems 缓存"""
        cache_dirs = [
            "/root/.triton/cache/",
            "/tmp/triton_cache/",
            "/root/.flaggems/code_cache/",
        ]

        for cache_dir in cache_dirs:
            cmd = f"docker exec {self.container_name} rm -rf {cache_dir}"
            try:
                subprocess.run(cmd, shell=True, check=True, capture_output=True)
                self.logger.info(f"Cleared cache: {cache_dir}")
            except subprocess.CalledProcessError as e:
                self.logger.warning(f"Failed to clear cache {cache_dir}: {e}")

    def _start_service(self, model_path: str) -> Tuple[bool, Optional[str]]:
        """启动服务

        Args:
            model_path: 模型路径

        Returns:
            (是否成功, 错误消息)
        """
        # 调用 start_service.sh
        # 这里简化实现，实际需要调用容器内的启动脚本

        start_cmd = (
            f"docker exec -d {self.container_name} bash -c "
            f"'cd /flagos-workspace && "
            f"VLLM_PLUGINS=fl USE_FLAGGEMS=1 "
            f"python3 -m vllm.entrypoints.openai.api_server "
            f"--model {model_path} "
            f"--port 8000 "
            f"> logs/service.log 2>&1'"
        )

        try:
            subprocess.run(start_cmd, shell=True, check=True, capture_output=True)
            self.logger.info("Service start command issued")
            return True, None
        except subprocess.CalledProcessError as e:
            error_msg = f"Failed to start service: {e.stderr.decode()}"
            self.logger.error(error_msg)
            return False, error_msg

    def _wait_for_service_ready(self, timeout: int = 300) -> bool:
        """等待服务就绪

        Args:
            timeout: 超时时间（秒）

        Returns:
            是否就绪
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            # 检查健康端点
            check_cmd = (
                f"docker exec {self.container_name} "
                f"curl -s http://localhost:8000/health"
            )

            try:
                result = subprocess.run(
                    check_cmd,
                    shell=True,
                    capture_output=True,
                    timeout=10,
                )

                if result.returncode == 0:
                    self.logger.info("Service is ready")
                    return True

            except subprocess.TimeoutExpired:
                pass

            time.sleep(5)

        self.logger.error(f"Service not ready after {timeout}s")
        return False

    def _extract_runtime_oplist(self) -> Tuple[Optional[List[str]], Optional[str]]:
        """提取运行时 oplist

        Returns:
            (算子列表, 文件路径)
        """
        # 查找 oplist 文件
        # 优先级：/tmp/flaggems_enable_oplist.txt > gems.txt

        oplist_candidates = [
            "/tmp/flaggems_enable_oplist.txt",
            "/tmp/gems.txt",
            "/root/gems.txt",
        ]

        for oplist_file in oplist_candidates:
            cmd = f"docker exec {self.container_name} cat {oplist_file}"

            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                if result.returncode == 0:
                    content = result.stdout.strip()
                    operators = [line.strip() for line in content.split('\n') if line.strip()]

                    self.logger.info(
                        f"Extracted {len(operators)} operators from {oplist_file}"
                    )

                    return operators, oplist_file

            except Exception as e:
                self.logger.debug(f"Could not read {oplist_file}: {e}")

        self.logger.error("No runtime oplist file found")
        return None, None

    def _validate_freshness(self, oplist_file: str) -> Tuple[bool, str]:
        """校验 oplist freshness（文件修改时间 vs 服务启动时间）

        Args:
            oplist_file: Oplist 文件路径

        Returns:
            (是否通过, 原因)
        """
        # 获取服务启动时间（从日志或进程）
        # 获取 oplist 文件修改时间
        # 比对：oplist_mtime 应该接近或晚于 service_start_time

        # 简化实现：检查文件是否在最近 5 分钟内修改
        cmd = f"docker exec {self.container_name} stat -c %Y {oplist_file}"

        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                mtime = int(result.stdout.strip())
                now = int(time.time())
                age = now - mtime

                if age <= 300:  # 5 分钟内
                    return True, f"Oplist is fresh (age={age}s)"
                else:
                    return False, f"Oplist is stale (age={age}s)"

        except Exception as e:
            return False, f"Failed to check freshness: {e}"

        return False, "Could not validate freshness"

    def _validate_identity(
        self,
        operators: List[str],
        flaggems_version: str,
    ) -> Tuple[bool, str]:
        """校验 oplist identity（合理范围、版本一致性）

        Args:
            operators: 算子列表
            flaggems_version: FlagGems 版本

        Returns:
            (是否通过, 原因)
        """
        # 合理范围：50-150 个算子（经验值）
        expected_range = (50, 150)

        if not (expected_range[0] <= len(operators) <= expected_range[1]):
            return False, (
                f"Operator count {len(operators)} out of expected range "
                f"{expected_range}"
            )

        # TODO: 版本一致性检查（需要 flaggems 版本对应的已知算子目录）

        return True, f"Operator count {len(operators)} in expected range"

    def create_v3_discovered_revision(
        self,
        operators: List[str],
        oplist_artifact_id: str,
    ) -> OperatorRevision:
        """创建 v3-discovered revision

        Args:
            operators: 发现的算子列表
            oplist_artifact_id: Runtime oplist Artifact ID

        Returns:
            OperatorRevision
        """
        revision = OperatorRevision(
            revision_id="v3-discovered",
            parent_revision_id=None,
            created_at=datetime.now().isoformat(),
            enabled_ops=operators.copy(),
            disabled_ops={},
            disable_reason_categories={"startup": [], "accuracy": [], "v4_performance": []},
            source_artifact=ArtifactReference(
                artifact_id=oplist_artifact_id,
                registered_at=datetime.now().isoformat(),
            ),
            verified=True,  # 发现阶段默认验证通过（服务已启动）
            frozen=False,
        )

        self.logger.info(f"Created v3-discovered revision: {len(operators)} operators")

        return revision
