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
from ..engine.command_executor import CommandExecutor, SubprocessExecutor
from .service_control import (
    DEFAULT_SERVICE_PORT,
    ServiceParams,
    start_service,
    stop_service,
    wait_for_service_ready,
)
from ..engine.long_task import LongTaskRunner


SERVICE_PORT = DEFAULT_SERVICE_PORT


class V3DiscoveryStartup:
    """V3 全组件发现启动"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
        executor: Optional[CommandExecutor] = None,
        poll_interval: Optional[float] = None,
    ):
        self.workspace_root = Path(workspace_root)
        self.container_name = container_name
        self.artifact_registry = artifact_registry or ArtifactRegistry(str(workspace_root))
        self.executor = executor or SubprocessExecutor()
        # 端口释放探测间隔（None = 立即返回；测试传 0）
        self.poll_interval = poll_interval
        self.logger = logging.getLogger("workflow.domain.v3_startup")

    def start_service_and_discover(
        self,
        model_path: str,
        flaggems_version: str,
        params: Optional[ServiceParams] = None,
    ) -> Tuple[bool, Optional[str], Optional[List[str]]]:
        """启动服务并发现算子列表

        Args:
            model_path: 模型路径（容器内）
            flaggems_version: FlagGems 版本
            params: 服务启动参数（ServiceParams）。起服务参数由引擎派生后传入——
                本模块**不再内联拼 vllm 命令**（那会丢掉 TP/可见设备/max_model_len/
                reasoning-parser/VLLM_PLUGINS 决策，而它们都在既有 start_service.sh 里）

        Returns:
            (是否成功, 错误消息, 发现的算子列表)
        """
        self.logger.info("Starting V3 discovery startup with full components")

        # 0. 先停旧服务（关键）：否则旧 vLLM 占着端口 → 新服务 bind 失败，而健康检查
        #    问的是旧服务（它回答 200）→ 引擎以为起来了，接着抽到**旧服务的 oplist**，
        #    全程"成功"但算子集是错的。这是最危险的一类静默错误。
        stop_service(
            self.executor, self.container_name,
            port=SERVICE_PORT, poll_interval=self.poll_interval, logger=self.logger,
        )
        started_at = time.time()

        if params is None:
            return False, "缺少服务启动参数（ServiceParams）——不再内联拼启动命令", None

        # 1. 启动服务：经既有 start_service.sh（`vllm serve` + TP + max-model-len +
        #    thinking 的 --reasoning-parser + VLLM_PLUGINS 三级决策 + 清缓存 + pid/日志软链）
        if not start_service(
            self.executor, self.container_name,
            model_path=params.model_path, model_name=params.model_name,
            port=params.port, tp_size=params.tp_size,
            max_model_len=params.max_model_len, thinking=params.thinking,
            cuda_visible_devices=params.cuda_visible_devices,
            vllm_plugins=params.vllm_plugins,
            env=params.env, logger=self.logger,
        ):
            return False, "start_service.sh 启动失败", None

        # 2. 等待就绪：经既有 wait_for_service.sh（**日志活动感知**：180s 无新输出才算卡住、
        #    绝对上限 5760s）。早前用 `curl /health` 轮询 300s 一刀切，大模型加载十几分钟
        #    会被误判失败——慢 ≠ 死。可能阻塞 1.6h，故走长任务协议。
        runner = LongTaskRunner(
            self.executor, self.container_name, poll_interval=self.poll_interval,
        )
        if not wait_for_service_ready(
            runner, self.container_name, params.port, params.model_name, logger=self.logger,
        ):
            return False, "服务未就绪（wait_for_service.sh）", None

        # 4. 提取 runtime oplist
        oplist, oplist_file = self._extract_runtime_oplist()

        if not oplist:
            return False, "Failed to extract runtime oplist", None

        # 5. Freshness 校验：oplist 必须是**本次启动之后**写的
        #    早前只判"5 分钟内"且失败仅告警——若新服务没起来，读到的是上一次服务的
        #    oplist（可能仍在 5 分钟内），于是带着错误的算子集继续跑。这里改为阻断。
        freshness_ok, freshness_reason = self._validate_freshness(oplist_file, since_ts=started_at)

        if not freshness_ok:
            self.logger.error(f"Freshness validation failed: {freshness_reason}")
            return False, f"runtime oplist 不是本次启动产出：{freshness_reason}", None

        # 6. Identity 校验
        identity_ok, identity_reason = self._validate_identity(
            oplist, flaggems_version
        )

        if not identity_ok:
            self.logger.warning(f"Identity validation warning: {identity_reason}")

        self.logger.info(f"V3 discovery completed: {len(oplist)} operators discovered")

        return True, None, oplist




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
            res = self.executor.docker_exec(
                self.container_name, f"cat {oplist_file}", timeout=10
            )
            if res.ok:
                content = res.stdout.strip()
                operators = [line.strip() for line in content.split('\n') if line.strip()]
                if operators:
                    self.logger.info(
                        f"Extracted {len(operators)} operators from {oplist_file}"
                    )
                    return operators, oplist_file

        self.logger.error("No runtime oplist file found")
        return None, None

    def _validate_freshness(self, oplist_file: str,
                            since_ts: Optional[float] = None) -> Tuple[bool, str]:
        """校验 oplist 是本次启动产出的（经注入的 executor）

        Args:
            oplist_file: Oplist 文件路径
            since_ts: 本次启动的时间戳；给定时要求 oplist 的 mtime **晚于**它
                      （不看"最近 5 分钟"这种宽松窗口——那会放过上一次服务的产物）

        Returns:
            (是否通过, 原因)
        """
        res = self.executor.docker_exec(
            self.container_name, f"stat -c %Y {oplist_file}", timeout=10
        )
        if not res.ok:
            return False, f"Failed to check freshness: exit={res.returncode}"
        try:
            mtime = int(res.stdout.strip())
        except (ValueError, AttributeError) as e:
            return False, f"Failed to parse mtime: {e}"

        now = time.time()
        if since_ts is not None:
            # 允许 60s 时钟/写入间隔（容器与宿主机时钟可能有偏差）
            if mtime < since_ts - 60:
                return False, (f"Oplist mtime={mtime} 早于本次启动 {int(since_ts)}"
                               f"（疑似上一个服务的产物）")
            return True, f"Oplist 由本次启动产出 (mtime={mtime})"

        age = int(now) - mtime
        if age <= 300:  # 兼容旧行为：未给 since_ts 时只看 5 分钟内
            return True, f"Oplist is fresh (age={age}s)"
        return False, f"Oplist is stale (age={age}s)"

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
