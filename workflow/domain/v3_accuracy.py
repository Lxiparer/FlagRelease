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
from ..engine.command_executor import CommandExecutor, SubprocessExecutor


# 评测脚本 / 判定脚本（容器内路径）
EVAL_SCRIPT = "/flagos-workspace/skills/flagos-eval-comprehensive/tools/fast_gpqa.py"
ACCURACY_COMPARE = "/flagos-workspace/skills/flagos-eval-comprehensive/tools/accuracy_compare.py"

# 数据集评测预算（thinking 模型口径，见 CLAUDE.md）
DATASET_BUDGET = {
    "gpqa_diamond": {"limit": 30, "max_timeout": 22500},
    "mmlu": {"limit": None, "max_timeout": 21600},
    "math_500": {"limit": None, "max_timeout": 7200},
}


class V3AccuracyEvaluation:
    """V3 精度评测"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
        executor: Optional[CommandExecutor] = None,
    ):
        self.workspace_root = Path(workspace_root)
        self.container_name = container_name
        self.artifact_registry = artifact_registry or ArtifactRegistry(str(workspace_root))
        self.executor = executor or SubprocessExecutor()
        self.logger = logging.getLogger("workflow.domain.accuracy")

    def evaluate_accuracy(
        self,
        candidate: str,
        revision: OperatorRevision,
        datasets: List[str],
        reference_model: str = "",
        nv_baseline_file: str = "/flagos-workspace/shared/nv_baseline.yaml",
    ) -> Tuple[bool, Dict[str, Dict]]:
        """评测精度并交由 accuracy_compare.py 判定（消费退出码，判定权在脚本）

        Args:
            candidate: v3 或 v4
            revision: 当前 operator revision
            datasets: 数据集列表（如 ["gpqa_diamond", "mmlu"]）
            reference_model: NV 参考模型名（accuracy_compare --reference）
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

            # 1. 运行评测（真实执行，产出候选结果 JSON）
            success, candidate_json, accuracy, details = self._run_evaluation(
                dataset, candidate, revision, reference_model,
            )

            if not success:
                self.logger.error(f"Evaluation failed for {dataset}")
                all_qualified = False
                results[dataset] = {
                    "success": False,
                    "qualified": False,
                    "error": details.get("error", "evaluation failed"),
                }
                continue

            # 2. 判定：交给已固化的 accuracy_compare.py，消费退出码
            #    0=达标 1=不达标 2=错误 3=缺 NV（fail-closed）。判定权在脚本，不内联重算。
            verdict = self._judge_with_accuracy_compare(
                dataset, candidate, candidate_json, reference_model, nv_baseline_file,
            )
            qualified = verdict["qualified"]
            if not qualified:
                all_qualified = False

            results[dataset] = {
                "success": True,
                "accuracy": accuracy,
                "nv_reference_value": verdict.get("nv_reference_value"),
                "nv_reference_identity": f"{reference_model}:{dataset}",
                "relative_drop": verdict.get("relative_drop"),
                "qualified": qualified,
                "exit_code": verdict["exit_code"],
                "reason": verdict.get("reason", ""),
                "details": details,
            }

            self.logger.info(
                f"{dataset}: exit_code={verdict['exit_code']} qualified={qualified} "
                f"rel_drop={verdict.get('relative_drop')}"
            )

        return all_qualified, results

    def _run_evaluation(
        self,
        dataset: str,
        candidate: str,
        revision: OperatorRevision,
        reference_model: str,
    ) -> Tuple[bool, str, Optional[float], Dict]:
        """运行单个数据集的评测（真实执行，经注入的 executor）

        Returns:
            (是否成功, 候选结果 JSON 路径, 精度值 or None, 详细信息)
        """
        budget = DATASET_BUDGET.get(dataset)
        if budget is None:
            self.logger.error(f"Unknown dataset: {dataset}")
            return False, "", None, {"error": f"Unknown dataset: {dataset}"}

        # 候选结果落盘路径（容器内 = 挂载点；V3 标准命名 flagos_optimized）
        candidate_json = f"/flagos-workspace/results/{dataset}_flagos_optimized.json"

        script = (
            f"cd /flagos-workspace && python3 {EVAL_SCRIPT} "
            f"--dataset {dataset} --output {candidate_json}"
        )
        if budget["limit"] is not None:
            script += f" --limit {budget['limit']}"
        script += f" --max-timeout {budget['max_timeout']}"

        res = self.executor.docker_exec(
            self.container_name, script, timeout=budget["max_timeout"] + 600
        )
        if not res.ok:
            return False, candidate_json, None, {
                "error": f"eval exit={res.returncode}: {res.stderr[:500]}",
                "dataset": dataset,
            }

        # 精度值仅用于报告富化（判定由 accuracy_compare 退出码给出）；best-effort 解析 stdout
        accuracy = self._parse_accuracy_from_stdout(res.stdout)
        return True, candidate_json, accuracy, {"dataset": dataset}

    def _judge_with_accuracy_compare(
        self,
        dataset: str,
        candidate: str,
        candidate_json: str,
        reference_model: str,
        nv_baseline_file: str,
    ) -> Dict:
        """交由已固化的 accuracy_compare.py 判定，消费退出码。

        退出码语义（脚本 docstring）：0=达标 · 1=不达标 · 2=参数/文件错 · 3=缺 NV（fail-closed）。
        判定权在脚本，本方法不内联重算 rel_drop。
        """
        compare_out = f"/flagos-workspace/results/accuracy_compare_{dataset}_{candidate}.json"
        script = (
            f"cd /flagos-workspace && python3 {ACCURACY_COMPARE} "
            f"--candidate {candidate_json} --reference {reference_model} --metric {dataset} "
            f"--nv-baseline-file {nv_baseline_file} --output {compare_out} --json"
        )
        res = self.executor.docker_exec(self.container_name, script, timeout=600)

        # 退出码是权威判定
        qualified = res.returncode == 0
        reason_map = {
            0: "qualified (accuracy_compare exit 0)",
            1: "not qualified (accuracy_compare exit 1)",
            2: "accuracy_compare error (exit 2)",
            3: "NV reference missing (exit 3, fail-closed)",
        }
        reason = reason_map.get(res.returncode, f"accuracy_compare exit {res.returncode}")

        # best-effort 从 --json stdout 提取 nv 分数 / rel_drop 用于 artifact 富化
        nv_value, rel_drop = None, None
        parsed = self._safe_json(res.stdout)
        if isinstance(parsed, dict):
            nv_value = (parsed.get("nv") or {}).get("score")
            rel_drop = parsed.get("rel_drop")

        return {
            "qualified": qualified,
            "exit_code": res.returncode,
            "nv_reference_value": nv_value,
            "relative_drop": rel_drop,
            "reason": reason,
        }

    @staticmethod
    def _safe_json(text: str):
        """从可能混杂日志的 stdout 中提取最后一个 JSON 对象（best-effort）"""
        text = (text or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            pass
        # 回退：取最后一个以 { 开头的行块
        start = text.rfind("{")
        if start >= 0:
            try:
                return json.loads(text[start:])
            except Exception:
                return None
        return None

    def _parse_accuracy_from_stdout(self, stdout: str) -> Optional[float]:
        """从评测 stdout 尽力解析精度值（仅用于报告，不参与判定）"""
        parsed = self._safe_json(stdout)
        if isinstance(parsed, dict):
            for k in ("score", "accuracy", "acc"):
                if isinstance(parsed.get(k), (int, float)):
                    return float(parsed[k])
        return None

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
