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
from typing import Dict, List, Optional, Tuple

from ..schemas.context_v2 import OperatorRevision, Gate
from ..artifacts.registry import ArtifactRegistry
from ..gates.reducer import GateReducer


class V3ReleaseManager:
    """V3 发布管理器"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        model_name: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
        gate_reducer: Optional[GateReducer] = None,
    ):
        self.workspace_root = workspace_root
        self.container_name = container_name
        self.model_name = model_name
        self.artifact_registry = artifact_registry or ArtifactRegistry(workspace_root)
        self.gate_reducer = gate_reducer or GateReducer(self.artifact_registry)
        self.logger = logging.getLogger("workflow.domain.v3_release")

    def release_v3(
        self,
        final_revision: OperatorRevision,
    ) -> Tuple[bool, Dict]:
        """发布 V3 版本

        Args:
            final_revision: v3-final 算子 revision

        Returns:
            (是否成功, 发布信息)
        """
        self.logger.info(f"Starting V3 release (revision={final_revision.revision_id})")

        # 1. 评估精度 Gate
        accuracy_gate = self.gate_reducer.evaluate_accuracy_gate("v3")
        self.logger.info(
            f"Accuracy Gate: passed={accuracy_gate.passed}, "
            f"reason={accuracy_gate.reason}"
        )

        # 2. 评估 V3 established Gate
        v3_established_gate = self.gate_reducer.evaluate_v3_established_gate()
        self.logger.info(
            f"V3 Established Gate: passed={v3_established_gate.passed}, "
            f"reason={v3_established_gate.reason}"
        )

        # 3. 确定发布范围
        release_scope = self._determine_release_scope(
            accuracy_gate,
            v3_established_gate,
        )

        self.logger.info(f"Release scope: {release_scope}")

        # 4. 打包镜像
        image_success, image_tag = self._package_image(
            final_revision,
            release_scope,
        )

        if not image_success:
            self.logger.error("Image packaging failed")
            return False, {"error": "image_packaging_failed"}

        # 5. 上传镜像
        upload_success = self._upload_image(
            image_tag,
            release_scope,
        )

        if not upload_success:
            self.logger.error("Image upload failed")
            return False, {"error": "image_upload_failed"}

        # 6. 生成发布报告
        report = self._generate_release_report(
            final_revision,
            accuracy_gate,
            v3_established_gate,
            release_scope,
            image_tag,
        )

        # 7. 保存发布记录
        self._save_release_record(report)

        return True, report

    def _determine_release_scope(
        self,
        accuracy_gate: Gate,
        v3_established_gate: Gate,
    ) -> str:
        """确定发布范围（Gate 驱动）

        Args:
            accuracy_gate: 精度 Gate
            v3_established_gate: V3 established Gate

        Returns:
            release_scope: "full" / "private-only"
        """
        if accuracy_gate.passed and v3_established_gate.passed:
            # 精度达标 + V3 established → 完整发布（Harbor + ModelScope/HF + README）
            return "full"
        else:
            # 精度不达标或 V3 未 established → 私有仅上传（仅 Harbor）
            return "private-only"

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
        # 实际通过 release_manager.py 执行
        # 此处为占位逻辑
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d%H%M")
        image_tag = f"harbor.baai.ac.cn/flagrelease-project/{self.model_name}-flagos:{timestamp}-v3"

        self.logger.info(f"Packaging image: {image_tag}")

        # 实际执行 docker commit + docker tag
        # success = self._execute_docker_commit(...)
        success = True  # 占位

        return success, image_tag

    def _upload_image(
        self,
        image_tag: str,
        release_scope: str,
    ) -> bool:
        """上传镜像

        Args:
            image_tag: 镜像 tag
            release_scope: 发布范围

        Returns:
            是否成功
        """
        self.logger.info(f"Uploading image: {image_tag} (scope={release_scope})")

        # 始终上传到 Harbor
        # success = self._execute_docker_push(image_tag)
        success = True  # 占位

        if release_scope == "full":
            # 额外发布到 ModelScope/HuggingFace（通过 release_manager.py）
            self.logger.info("Full release: updating ModelScope/HuggingFace")
            # self._publish_to_model_hub(...)

        return success

    def _generate_release_report(
        self,
        revision: OperatorRevision,
        accuracy_gate: Gate,
        v3_established_gate: Gate,
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
                "accuracy": {
                    "passed": accuracy_gate.passed,
                    "reason": accuracy_gate.reason,
                },
                "v3_established": {
                    "passed": v3_established_gate.passed,
                    "reason": v3_established_gate.reason,
                },
            },
            "release_scope": release_scope,
            "image_tag": image_tag,
            "artifacts": {
                "image": image_tag,
                "published_to": ["harbor.baai.ac.cn/flagrelease-project"],
            },
        }

        if release_scope == "full":
            report["artifacts"]["published_to"].extend(
                ["ModelScope", "HuggingFace"]
            )

        return report

    def _save_release_record(self, report: Dict):
        """保存发布记录

        Args:
            report: 发布报告
        """
        record_file = os.path.join(
            self.workspace_root, "results", "v3_release_record.json"
        )

        with open(record_file, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        self.logger.info(f"Release record saved: {record_file}")
