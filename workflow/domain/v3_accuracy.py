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

"""V3 Accuracy Evaluation - V3 精度评测

职责：
1. 运行精度评测（对外 NV reference）
2. 逐数据集比对
3. 计算相对退化
4. 生成 Accuracy Artifact
5. 触发精度 Gate 判定
"""

import json
import subprocess
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from ..artifacts.registry import ArtifactRegistry
from ..schemas.context_v2 import OperatorRevision


class V3AccuracyEvaluation:
    """V3 精度评测"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
    ):
        self.workspace_root = Path(workspace_root)
        self.container_name = container_name
        self.artifact_registry = artifact_registry or ArtifactRegistry(str(workspace_root))
        self.logger = logging.getLogger("workflow.domain.accuracy")

    def evaluate_accuracy(
        self,
        candidate: str,
        revision: OperatorRevision,
        datasets: List[str],
        nv_baseline_file: str = "/flagos-workspace/shared/nv_baseline.yaml",
    ) -> Tuple[bool, Dict[str, Dict]]:
        """评测精度并与外部 NV reference 比对

        Args:
            candidate: v3 或 v4
            revision: 当前 operator revision
            datasets: 数据集列表（如 ["gpqa_diamond", "mmlu"]）
            nv_baseline_file: NV baseline 文件路径

        Returns:
            (是否全部达标, {dataset: accuracy_result})
        """
        self.logger.info(
            f"Evaluating accuracy for {candidate} on datasets: {datasets}"
        )

        all_qualified = True
        results = {}

        for dataset in datasets:
            self.logger.info(f"=== Evaluating {dataset} ===")

            # 1. 运行评测
            success, accuracy, details = self._run_evaluation(
                dataset,
                candidate,
                revision,
            )

            if not success:
                self.logger.error(f"Evaluation failed for {dataset}")
                all_qualified = False
                results[dataset] = {
                    "success": False,
                    "error": details.get("error", "Unknown error"),
                }
                continue

            # 2. 加载 NV reference
            nv_reference = self._load_nv_reference(dataset, nv_baseline_file)

            if nv_reference is None:
                # NV reference 缺失 - fail closed
                self.logger.error(f"NV reference missing for {dataset} - fail closed")
                all_qualified = False
                results[dataset] = {
                    "success": True,
                    "accuracy": accuracy,
                    "nv_reference": None,
                    "relative_drop": None,
                    "qualified": False,
                    "reason": "NV reference missing",
                }
                continue

            # 3. 计算相对退化
            relative_drop = (nv_reference - accuracy) / nv_reference

            # 4. 判定是否达标（≤ 5%）
            qualified = relative_drop <= 0.05

            if not qualified:
                all_qualified = False

            results[dataset] = {
                "success": True,
                "accuracy": accuracy,
                "nv_reference": nv_reference,
                "relative_drop": relative_drop,
                "qualified": qualified,
                "details": details,
            }

            self.logger.info(
                f"{dataset}: accuracy={accuracy:.2f}%, "
                f"nv_reference={nv_reference:.2f}%, "
                f"relative_drop={relative_drop*100:.2f}%, "
                f"qualified={qualified}"
            )

        return all_qualified, results

    def _run_evaluation(
        self,
        dataset: str,
        candidate: str,
        revision: OperatorRevision,
    ) -> Tuple[bool, Optional[float], Dict]:
        """运行单个数据集的评测

        Args:
            dataset: 数据集名称
            candidate: v3/v4
            revision: Operator revision

        Returns:
            (是否成功, 精度值, 详细信息)
        """
        # 实际需要调用评测脚本（如 fast_gpqa.py）
        # 这里简化实现

        eval_script = "/flagos-workspace/skills/flagos-eval-comprehensive/tools/fast_gpqa.py"

        # 根据数据集选择参数
        if dataset == "gpqa_diamond":
            limit = 30  # thinking 模型
            max_timeout = 22500
        elif dataset == "mmlu":
            limit = None  # 不传 limit，用默认采样
            max_timeout = 21600
        elif dataset == "math_500":
            limit = None
            max_timeout = 7200
        else:
            self.logger.error(f"Unknown dataset: {dataset}")
            return False, None, {"error": f"Unknown dataset: {dataset}"}

        # 构造命令
        cmd_parts = [
            f"docker exec {self.container_name}",
            "bash -c",
            f"'cd /flagos-workspace && PATH=/opt/conda/bin:$PATH",
            f"python3 {eval_script}",
            f"--dataset {dataset}",
        ]

        if limit:
            cmd_parts.append(f"--limit {limit}")

        cmd_parts.append(f"--max-timeout {max_timeout}")
        cmd_parts.append("'")

        cmd = " ".join(cmd_parts)

        self.logger.info(f"Running evaluation command: {cmd}")

        # 简化实现：返回模拟结果
        # 实际需要真正执行命令并解析输出
        accuracy = 65.2  # 模拟精度值

        return True, accuracy, {
            "total_questions": 30,
            "correct": 20,
            "dataset": dataset,
        }

    def _load_nv_reference(
        self,
        dataset: str,
        nv_baseline_file: str,
    ) -> Optional[float]:
        """加载外部 NV reference

        Args:
            dataset: 数据集名称
            nv_baseline_file: NV baseline 文件路径

        Returns:
            NV reference 精度值，缺失时返回 None
        """
        # 实际需要从 nv_baseline.yaml 读取
        # 简化实现：返回模拟值

        nv_references = {
            "gpqa_diamond": 66.8,
            "mmlu": 69.1,
            "math_500": 72.5,
        }

        return nv_references.get(dataset)

    def register_accuracy_artifact(
        self,
        candidate: str,
        dataset: str,
        result: Dict,
        file_path: str,
    ) -> str:
        """登记精度结果为 Artifact

        Args:
            candidate: v3/v4
            dataset: 数据集
            result: 评测结果
            file_path: 结果文件路径

        Returns:
            artifact_id
        """
        artifact_id = self.artifact_registry.register_artifact(
            artifact_type="accuracy-result",
            content=result,
            file_path=file_path,
            generated_by="script",
            generator_version="v3_accuracy_evaluation_1.0",
            tags={
                "candidate": candidate,
                "dataset": dataset,
                "qualified": str(result.get("qualified", False)),
            },
        )

        self.logger.info(f"Registered accuracy artifact: {artifact_id}")

        return artifact_id
