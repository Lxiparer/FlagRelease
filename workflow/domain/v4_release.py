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

"""V4 Release - V4 发布管理

职责：
1. 按 Engine 传入的 V4 establishment 判定决定发布或回退
2. 发布 V4（-v4 tag，Harbor flagrelease-public）
3. 处理回退场景（V4 不成立时不产出独立 V4，报告中说明回退到 V3）

约定（与步骤10 V3 发布一致）：Gate 判定由引擎持有并传入，domain 不重判。
"""

import json
import logging
import os
from datetime import datetime
from typing import Dict, Optional, Tuple

from ..schemas.context_v2 import OperatorRevision
from ..artifacts.registry import ArtifactRegistry
from ..engine.command_executor import CommandExecutor, SubprocessExecutor

# V4 发布目标仓库（V1/V2/V4 → flagrelease-public；V3 单独走 flagrelease-project，见 CLAUDE.md）
HARBOR_V4_PROJECT = "harbor.baai.ac.cn/flagrelease-public"


class V4ReleaseManager:
    """V4 发布管理器"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        model_name: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
        executor: Optional[CommandExecutor] = None,
    ):
        self.workspace_root = workspace_root
        self.container_name = container_name
        self.model_name = model_name
        self.artifact_registry = artifact_registry or ArtifactRegistry(workspace_root)
        self.executor = executor or SubprocessExecutor()
        self.logger = logging.getLogger("workflow.domain.v4_release")

    def release_v4(
        self,
        v4_final: Optional[OperatorRevision],
        optimization_report: Dict,
        established_passed: bool = False,
    ) -> Tuple[bool, Dict]:
        """发布 V4 版本（或处理回退）

        Args:
            v4_final: v4-final revision（None 表示 V4 不成立）
            optimization_report: V4 优化报告（阶段1/2 产出，经 artifact 传递）
            established_passed: V4 establishment Gate 是否通过（引擎传入）

        Returns:
            (是否成功, 发布信息)

        Note:
            回退场景返回 (True, fallback_report)：不是执行错误，而是
            「无合法提升 → 不产出 V4」的正常结论，由引擎记为 skipped。
        """
        if v4_final is None or not established_passed:
            # V4 不成立 - 回退到 V3
            self.logger.info(
                f"V4 not established (revision={v4_final}, "
                f"gate_passed={established_passed}), fallback to V3"
            )
            return self._handle_v4_fallback(optimization_report)

        self.logger.info(f"V4 established (revision={v4_final.revision_id})")

        # 打包镜像（-v4 tag）
        image_success, image_tag = self._package_image(v4_final)

        if not image_success:
            self.logger.error("Image packaging failed")
            return False, {"error": "image_packaging_failed"}

        # 上传镜像（plugin 镜像模式，发布到 flagrelease-project）
        upload_success = self._upload_image(image_tag)

        if not upload_success:
            self.logger.error("Image upload failed")
            return False, {"error": "image_upload_failed"}

        # 生成发布报告
        report = self._generate_release_report(
            v4_final,
            established_passed,
            optimization_report,
            image_tag,
        )

        # 保存发布记录
        self._save_release_record(report)

        return True, report

    def _handle_v4_fallback(self, optimization_report: Dict) -> Tuple[bool, Dict]:
        """处理 V4 回退场景

        Args:
            optimization_report: V4 优化报告

        Returns:
            (是否成功, 回退报告)
        """
        fallback_report = {
            "version": "v4",
            "success": False,
            "execution_success": optimization_report.get("execution_success", True),
            "established": False,
            "fallback_to_v3": True,
            "reason": optimization_report.get("reason", "unknown"),
            "optimization_attempted": True,
            "phase1_candidates": optimization_report.get("phase1_candidates", 0),
            "phase2_tested": optimization_report.get("phase2_tested", 0),
            "message": (
                "V4 optimization did not yield qualified improvements. "
                "V3 remains as the final delivery version."
            ),
        }

        # 保存回退记录
        results_dir = os.path.join(self.workspace_root, "results")
        os.makedirs(results_dir, exist_ok=True)
        record_file = os.path.join(results_dir, "v4_fallback_record.json")

        with open(record_file, "w") as f:
            json.dump(fallback_report, f, indent=2, ensure_ascii=False)

        self.logger.info(f"V4 fallback record saved: {record_file}")

        return True, fallback_report

    def _package_image(
        self,
        revision: OperatorRevision,
    ) -> Tuple[bool, str]:
        """打包 V4 镜像

        Args:
            revision: v4-final revision

        Returns:
            (是否成功, image_tag)
        """
        timestamp = datetime.now().strftime("%Y%m%d%H%M")
        image_tag = f"{HARBOR_V4_PROJECT}/{self.model_name}-flagos:{timestamp}-v4"

        self.logger.info(f"Packaging V4 image: {image_tag}")

        # docker commit 快照容器为镜像（宿主机 docker 命令，非容器内执行）
        res = self.executor.run(["docker", "commit", self.container_name, image_tag], timeout=1800)
        if not res.ok:
            self.logger.error(f"docker commit failed: exit={res.returncode}: {res.stderr[:300]}")
            return False, image_tag

        return True, image_tag

    def _upload_image(self, image_tag: str) -> bool:
        """上传 V4 镜像（docker push 到 Harbor flagrelease-public）

        Args:
            image_tag: 镜像 tag

        Returns:
            是否成功
        """
        self.logger.info(f"Uploading V4 image: {image_tag}")

        res = self.executor.run(["docker", "push", image_tag], timeout=3600)
        if not res.ok:
            self.logger.error(f"docker push failed: exit={res.returncode}: {res.stderr[:300]}")
            return False

        return True

    def _generate_release_report(
        self,
        revision: OperatorRevision,
        established_passed: bool,
        optimization_report: Dict,
        image_tag: str,
    ) -> Dict:
        """生成 V4 发布报告

        Returns:
            report dict
        """
        report = {
            "version": "v4",
            "model_name": self.model_name,
            "revision_id": revision.revision_id,
            "enabled_operators": revision.enabled_ops,
            "disabled_operators": revision.disabled_ops,
            "operator_count": len(revision.enabled_ops),
            "optimization": {
                "phase1_candidates": optimization_report.get("phase1_candidates", 0),
                "phase2_tested": optimization_report.get("phase2_tested", 0),
                "v4_throughput": optimization_report.get("v4_throughput", 0.0),
                "improvement_over_v3": True,
            },
            "gates": {
                "v4_established": {"passed": established_passed},
            },
            "image_tag": image_tag,
            "artifacts": {
                "image": image_tag,
                "published_to": [HARBOR_V4_PROJECT],
            },
        }

        return report

    def _save_release_record(self, report: Dict):
        """保存发布记录

        Args:
            report: 发布报告
        """
        results_dir = os.path.join(self.workspace_root, "results")
        os.makedirs(results_dir, exist_ok=True)
        record_file = os.path.join(results_dir, "v4_release_record.json")

        with open(record_file, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        self.logger.info(f"V4 release record saved: {record_file}")
