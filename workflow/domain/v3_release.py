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

"""V3 Release - V3 发布管理

职责：
1. 冻结 v3-final revision
2. 根据精度 Gate 决定发布范围
3. 执行镜像打包和上传
4. 生成发布报告
"""

import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from ..schemas.context_v2 import OperatorRevision, Gate
from ..artifacts.registry import ArtifactRegistry
from ..gates.reducer import GateReducer
from ..engine.command_executor import CommandExecutor, SubprocessExecutor

# V3 发布目标仓库（交付 SVT 验收，见 CLAUDE.md）
HARBOR_V3_PROJECT = "harbor.baai.ac.cn/flagrelease-project"


def normalize_model_name(model_name: str) -> str:
    """镜像 tag 里的模型名归一化

    原因（真机）：`--model` 常带 vendor 前缀（如 `Qwen/Qwen3-8B`），直接拼进 tag 会
    产生 `harbor.../Qwen/Qwen3-8B-flagos:...` 这种多级 repo 路径 → push 失败或推错地方。
    取短名并与既有容器命名口径一致（宿主编排也是 `sed 's|.*/||'` 取短名），
    再把 Docker 不允许的字符换成 `-`。
    """
    short = (model_name or "").rstrip("/").split("/")[-1]
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in short)
    return safe or "model"


# Harbor 凭证环境变量（CLAUDE.md：凭证通过环境变量提供）
HARBOR_USER_ENV = "HARBOR_USER"
HARBOR_PASSWORD_ENV = "HARBOR_PASSWORD"


def harbor_login(executor, registry: str, logger: logging.Logger) -> bool:
    """登录 Harbor（凭证从环境变量取；没配凭证则跳过并告警）

    既有编排的教训：发布命令可能因权限/环境问题**静默失败**而流程照常继续
    （2026-08-05 Mistral-Small V3 事故：报告说发布了，实际没有）。所以这里
    显式登录 + 推送后回读校验。
    """
    user = os.environ.get(HARBOR_USER_ENV, "")
    password = os.environ.get(HARBOR_PASSWORD_ENV, "")
    if not user or not password:
        logger.warning(
            f"未提供 {HARBOR_USER_ENV}/{HARBOR_PASSWORD_ENV}，跳过 docker login"
            f"（若宿主已登录则可继续）"
        )
        return False
    # 密码走 stdin（`-p` 会让凭证出现在进程列表里）
    res = executor.run(
        ["docker", "login", registry, "-u", user, "--password-stdin"],
        timeout=120, stdin=password,
    )
    if not res.ok:
        logger.error(f"docker login {registry} 失败：{res.stderr[:200]}")
        return False
    logger.info(f"已登录 {registry}")
    return True


def push_image(executor, image_tag: str, logger: logging.Logger,
               attempts: int = 2) -> bool:
    """推送镜像：失败重试一次；成功后**回读校验**确认远端真的有这个 tag"""
    for attempt in range(1, attempts + 1):
        res = executor.run(["docker", "push", image_tag], timeout=3600)
        if res.ok:
            if verify_pushed(executor, image_tag, logger):
                return True
            logger.error(f"推送返回成功但回读校验失败（{image_tag}）——疑似静默未发布")
        else:
            logger.error(f"docker push 失败（第 {attempt}/{attempts} 次）：{res.stderr[:200]}")
    return False


def verify_pushed(executor, image_tag: str, logger: logging.Logger) -> bool:
    """回读校验：确认镜像 tag 在远端仓库可见（防"命令成功但实际没发布"）"""
    res = executor.run(["docker", "manifest", "inspect", image_tag], timeout=300)
    if not res.ok:
        logger.error(f"回读校验失败：docker manifest inspect {image_tag} → {res.stderr[:200]}")
        return False
    logger.info(f"发布回读校验通过：{image_tag}")
    return True


class V3ReleaseManager:
    """V3 发布管理器"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        model_name: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
        gate_reducer: Optional[GateReducer] = None,
        executor: Optional[CommandExecutor] = None,
    ):
        self.workspace_root = workspace_root
        self.container_name = container_name
        self.model_name = model_name
        self.artifact_registry = artifact_registry or ArtifactRegistry(workspace_root)
        self.gate_reducer = gate_reducer or GateReducer(self.artifact_registry)
        self.executor = executor or SubprocessExecutor()
        self.logger = logging.getLogger("workflow.domain.v3_release")

    def release_v3(
        self,
        final_revision: OperatorRevision,
        accuracy_passed: bool,
        established_passed: bool,
    ) -> Tuple[bool, Dict]:
        """发布 V3 版本

        Gate 决策由引擎传入（引擎拥有 gate 真相，domain 不重判——与 M1a 精度判定一致）。

        Args:
            final_revision: v3-final 算子 revision
            accuracy_passed: 精度 gate 是否达标
            established_passed: v3-final 是否已冻结建立

        Returns:
            (是否成功, 发布信息)
        """
        self.logger.info(f"Starting V3 release (revision={final_revision.revision_id})")

        # 1. 发布范围：精度达标 + established → full（Harbor+MS/HF）；否则 private-only（仅 Harbor）
        release_scope = "full" if (accuracy_passed and established_passed) else "private-only"
        self.logger.info(f"Release scope: {release_scope}")

        # 2. 打包镜像（docker commit，宿主机 docker 命令）
        image_success, image_tag = self._package_image(final_revision, release_scope)
        if not image_success:
            self.logger.error("Image packaging failed")
            return False, {"error": "image_packaging_failed"}

        # 3. 上传镜像（docker push；full 额外 MS/HF）
        upload_success = self._upload_image(image_tag, release_scope)
        if not upload_success:
            self.logger.error("Image upload failed")
            return False, {"error": "image_upload_failed", "image_tag": image_tag}

        # 4. 生成 + 保存发布报告
        report = self._generate_release_report(
            final_revision, accuracy_passed, established_passed, release_scope, image_tag
        )
        self._save_release_record(report)
        return True, report

    def _package_image(
        self,
        revision: OperatorRevision,
        release_scope: str,
    ) -> Tuple[bool, str]:
        """打包镜像

        Args:
            revision: v3-final revision
            release_scope: 发布范围

        Returns:
            (是否成功, image_tag)
        """
        # docker commit 快照容器为镜像（宿主机 docker 命令，非容器内执行）
        timestamp = datetime.now().strftime("%Y%m%d%H%M")
        image_tag = (f"{HARBOR_V3_PROJECT}/"
                     f"{normalize_model_name(self.model_name)}-flagos:{timestamp}-v3")

        self.logger.info(f"Packaging image: {image_tag}")
        res = self.executor.run(["docker", "commit", self.container_name, image_tag], timeout=1800)
        if not res.ok:
            self.logger.error(f"docker commit failed: exit={res.returncode}: {res.stderr[:300]}")
            return False, image_tag
        return True, image_tag

    def _upload_image(
        self,
        image_tag: str,
        release_scope: str,
    ) -> bool:
        """上传镜像（docker push 到 Harbor；full 额外 MS/HF）

        Args:
            image_tag: 镜像 tag
            release_scope: 发布范围

        Returns:
            是否成功
        """
        self.logger.info(f"Uploading image: {image_tag} (scope={release_scope})")

        # 始终 push 到 Harbor（宿主机 docker 命令）：先登录、再推送、再回读校验
        harbor_login(self.executor, HARBOR_V3_PROJECT.split("/")[0], self.logger)
        if not push_image(self.executor, image_tag, self.logger):
            return False

        if release_scope == "full":
            # full：额外发布到 ModelScope/HuggingFace（README + 权重）。
            # 走 release_manager.py 的上传能力，接线留待发布竖片扩展（M1c）；此处仅标记。
            self.logger.info("Full release: ModelScope/HuggingFace 上传（待 M1c 接 release_manager）")

        return True

    def _generate_release_report(
        self,
        revision: OperatorRevision,
        accuracy_passed: bool,
        established_passed: bool,
        release_scope: str,
        image_tag: str,
    ) -> Dict:
        """生成发布报告

        Returns:
            report dict
        """
        report = {
            "version": "v3",
            "model_name": self.model_name,
            "revision_id": revision.revision_id,
            "enabled_operators": revision.enabled_ops,
            "disabled_operators": revision.disabled_ops,
            "operator_count": len(revision.enabled_ops),
            "gates": {
                "accuracy": {"passed": accuracy_passed},
                "v3_established": {"passed": established_passed},
            },
            "release_scope": release_scope,
            "image_tag": image_tag,
            "artifacts": {
                "image": image_tag,
                "published_to": [HARBOR_V3_PROJECT],
            },
        }

        if release_scope == "full":
            # 对外发布（MS/HF 权重 + README）**尚未实现**（M1c）。
            # 这里只登记"待办"，绝不把它写进 published_to——否则报告会声称已对外发布，
            # 而实际只 push 了 Harbor 私有镜像（"把步骤成功显示成版本达标"的同类问题）。
            report["artifacts"]["pending_publish"] = ["ModelScope", "HuggingFace"]
            report["artifacts"]["_pending_reason"] = "对外发布通道未实现（M1c）"

        return report

    def _save_release_record(self, report: Dict):
        """保存发布记录

        Args:
            report: 发布报告
        """
        results_dir = os.path.join(self.workspace_root, "results")
        os.makedirs(results_dir, exist_ok=True)
        record_file = os.path.join(results_dir, "v3_release_record.json")

        with open(record_file, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        self.logger.info(f"Release record saved: {record_file}")
