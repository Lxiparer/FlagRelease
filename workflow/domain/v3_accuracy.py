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
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from ..artifacts.registry import ArtifactRegistry
from ..schemas.context_v2 import OperatorRevision
from ..engine.command_executor import CommandExecutor, SubprocessExecutor, parse_json_output
from ..engine.long_task import CONDA_PATH, LongTaskRunner


# 评测脚本 / 判定脚本（容器内路径）
# 注意：`setup_workspace.sh` 的 SCRIPT_MAP 只把工具投到 `/flagos-workspace/scripts/`
# （容器里**没有** `skills/` 目录），这里必须用实际部署路径，否则真跑即 file-not-found。
TOOLS_DIR = "/flagos-workspace/scripts"
EVAL_WRAPPER = f"{TOOLS_DIR}/eval_wrapper.py"
EVAL_SCRIPT = f"{TOOLS_DIR}/fast_gpqa.py"
EVAL_CONFIG = "fast_gpqa_config.yaml"  # 相对 TOOLS_DIR（evals 的 cwd 即 scripts/）
ACCURACY_COMPARE = f"{TOOLS_DIR}/accuracy_compare.py"
SERVICE_LOG = "/flagos-workspace/logs/service.log"

# 数据集评测预算（thinking 模型口径，见 CLAUDE.md）
DATASET_BUDGET = {
    "gpqa_diamond": {"limit": 30, "max_timeout": 22500},
    "mmlu": {"limit": None, "max_timeout": 21600},
    "math_500": {"limit": None, "max_timeout": 7200},
}

# 完整性校验的题数下限（防"评测被截断却仍写出合法 JSON"被当成小样本通过）：
# 不传 --limit 时用数据集默认题数（mmlu 57子集×20=1140、math_500 5等级×40=200），
# 这里取下限留出余量；传了 --limit 则以 --limit 为准。
DATASET_MIN_QUESTIONS = {
    "gpqa_diamond": 30,
    "mmlu": 1000,
    "math_500": 150,
}

# accuracy_compare.py 退出码（判定权在该脚本）
EXIT_QUALIFIED = 0
EXIT_NOT_QUALIFIED = 1
EXIT_TOOL_ERROR = 2
EXIT_MISSING_NV = 3


class V3AccuracyEvaluation:
    """V3 精度评测"""

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
        # 长任务轮询间隔（None = LongTaskRunner 默认；测试传 0 以不睡）
        self.poll_interval = poll_interval
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
                single_dataset=(len(datasets) == 1),
            )

            if not success:
                self.logger.error(f"Evaluation failed for {dataset}")
                all_qualified = False
                results[dataset] = {
                    "success": False,
                    "qualified": False,
                    # 评测根本没跑成 → 不是"精度不达标"，是**无法评估**
                    "assessed": False,
                    "unassessed_reason": details.get("error", "evaluation failed"),
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

            # 只有 exit 0/1 是"判定结论"；2=脚本错误 3=缺 NV 基线属于**无法评估**。
            # 两者都让 Gate 不通过，但必须区分——否则工具坏掉会伪装成"精度退化"，
            # 报告与发布决策会把它当成模型的问题（plan：NV 缺失 → accuracy unassessed）。
            exit_code = verdict["exit_code"]
            assessed = exit_code in (EXIT_QUALIFIED, EXIT_NOT_QUALIFIED)

            results[dataset] = {
                "success": True,
                "assessed": assessed,
                "unassessed_reason": "" if assessed else verdict.get("reason", ""),
                "accuracy": accuracy,
                "nv_reference_value": verdict.get("nv_reference_value"),
                "nv_reference_identity": f"{reference_model}:{dataset}",
                "relative_drop": verdict.get("relative_drop"),
                "qualified": qualified,
                "exit_code": exit_code,
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
        single_dataset: bool = True,
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

        # 评测必须经 eval_wrapper.py 执行（编排层硬性要求，不要直接调 fast_gpqa.py）：
        # 它负责 stalled/进度停滞/总超时三层看门狗，超时语义在它身上（fast_gpqa 没有
        # --max-timeout 参数）。
        inner = f"python3 {os.path.basename(EVAL_SCRIPT)} --config {EVAL_CONFIG} --dataset {dataset}"
        # `--limit` 是**全局**语义（fast_gpqa 不支持 per-dataset），因此多数据集时一律不传，
        # 由工具按各数据集默认题数评测（既有口径，见 CLAUDE.md「数据集参数」）。
        if single_dataset and budget["limit"] is not None:
            inner += f" --limit {budget['limit']}"
        inner += f" --output {candidate_json}"

        # 顺序：监督者选项在前、被包裹命令在最后（与 `timeout 300 cmd` 同构；
        # 也让"外层参数 vs 内层参数"在命令行上一眼可辨）
        cmd = (
            f"cd {TOOLS_DIR} && {CONDA_PATH} python3 {os.path.basename(EVAL_WRAPPER)} "
            f"--service-log {SERVICE_LOG} "
            f"--stall-timeout 300 --max-timeout {budget['max_timeout']} "
            f"--eval-cmd \"{inner}\""
        )

        eval_started_at = datetime.now()
        # 长任务协议：detached 启动 + state 轮询 + 存活判定 + 断点接管。
        # task_id 必须**按 revision 唯一**——否则步骤07 的第2轮会"接管"第1轮的结果。
        task_id = f"eval_{candidate}_{revision.revision_id}_{dataset}".replace("/", "_")
        runner = LongTaskRunner(
            self.executor, self.container_name, poll_interval=self.poll_interval,
        )
        result = runner.run(task_id, cmd, timeout=budget["max_timeout"])
        if not result.ok:
            return False, candidate_json, None, {
                "error": f"评测任务未成功：{result.summary()}",
                "dataset": dataset,
                "log_tail": result.log_tail[-800:],
            }

        # 结果有效性校验：防"上一轮残留 / 别的工具产出 / 截断的评测"被当成本次结果
        ok, why, total = self._validate_result(
            dataset, budget, candidate_json, eval_started_at, single_dataset=single_dataset,
        )
        if not ok:
            return False, candidate_json, None, {
                "error": f"评测结果无效：{why}", "dataset": dataset,
                "total_questions": total,
            }

        # 证据快照：每次评测都写同一个 `{ds}_flagos_optimized.json`，下一轮会覆盖它；
        # 复制成按 revision 命名的文件，artifact 才指向真实且不被覆盖的证据。
        evidence_rel = self._snapshot_result(dataset, candidate, revision, candidate_json)

        # 精度值仅用于报告富化（判定由 accuracy_compare 退出码给出）；best-effort 解析 stdout
        accuracy = self._parse_accuracy_from_stdout(result.log_tail)
        return True, candidate_json, accuracy, {
            "dataset": dataset, "total_questions": total, "task_status": result.status,
            "evidence_file": evidence_rel,
        }

    def _validate_result(self, dataset: str, budget: Dict, candidate_json: str,
                         started_at: datetime, single_dataset: bool = True):
        """校验结果文件是**本次评测真实产出**的有效结果

        三判据（对齐既有编排经验，缺一不可）：
        1. `_producer == 'fast_gpqa.py'` —— 防别的工具产出或手工拼的文件；
        2. `score` 非空 —— 空分数不能进判定；
        3. `timestamp` **晚于本次评测开始** —— 防上一轮残留 / 断点续跑的旧结果被当成本次结果；
        外加题数校验：不足预期（截断）即无效——否则小样本还会触发噪声容忍被"救"成达标。

        Returns:
            (是否有效, 原因, 实际题数)
        """
        rel = candidate_json.replace("/flagos-workspace/", "", 1)
        host_file = Path(self.workspace_root) / rel
        if not host_file.exists():
            return False, f"结果文件不存在（{rel}）", 0
        try:
            data = json.loads(host_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return False, f"结果文件不可解析：{e}", 0
        if not isinstance(data, dict):
            return False, "结果文件不是 JSON 对象", 0

        producer = data.get("_producer")
        if producer != "fast_gpqa.py":
            return False, f"_producer 不是 fast_gpqa.py（实际 {producer!r}）——疑似其他工具产出", 0

        if data.get("score") is None:
            return False, "结果缺少 score（空分数不参与判定）", 0

        ts = data.get("timestamp")
        if ts:
            try:
                produced = datetime.fromisoformat(str(ts))
            except ValueError:
                produced = None
            if produced is not None and produced < started_at.replace(microsecond=0):
                return False, (f"结果 timestamp={ts} 早于本次评测开始"
                               f"（{started_at.isoformat(timespec='seconds')}）——疑似上一轮残留"), 0
        else:
            return False, "结果缺少 timestamp（无法确认是本次产出）", 0

        total = data.get("total_questions")
        if not isinstance(total, int) or total <= 0:
            return False, "结果缺少 total_questions（无法确认评测完整性）", 0

        # 多数据集时不传 --limit → 期望值取数据集默认题数下限
        expected = (DATASET_MIN_QUESTIONS.get(dataset, 0)
                    if not single_dataset else (budget["limit"] or DATASET_MIN_QUESTIONS.get(dataset, 0)))
        if expected and total < expected:
            return False, f"实际 {total} 题 < 期望 {expected} 题（疑似评测被截断）", total
        return True, "", total

    def _snapshot_result(self, dataset: str, candidate: str, revision: OperatorRevision,
                         candidate_json: str) -> str:
        """把本次评测结果复制成按 revision 命名的证据文件（返回相对 workspace 的路径）

        原因：每次评测都写同一个 `results/{dataset}_flagos_optimized.json`，
        下一个候选/下一轮会把它覆盖掉——artifact 若指向它，事后复核看到的是别人的数据。
        """
        rel_src = candidate_json.replace("/flagos-workspace/", "", 1)
        src = Path(self.workspace_root) / rel_src
        rel_dst = f"results/accuracy/{candidate}-{revision.revision_id}-{dataset}.json"
        dst = Path(self.workspace_root) / rel_dst
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        except OSError as e:
            self.logger.warning(f"证据快照失败（不阻断判定）：{e}")
            return rel_src
        return rel_dst


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
        """从可能混杂日志的 stdout 中提取 JSON 对象（best-effort，共享实现）"""
        return parse_json_output(text)

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
