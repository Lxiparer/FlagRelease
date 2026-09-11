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

"""V4 Operator Reduction - V4 减算子性能优化

设计原则：
- 从 V3 基线出发，逐个减算子以提升性能
- 追求性能绝对值最大化，达标基准是超越 V3（不与 V1 比较）
- 精度相对退化 ≤ 5% 是 V4 成立前提（硬约束）
- 保底至少保留 1 个算子（即使是 plugin）
- 两阶段策略：阶段1 性能搜索（不测精度），阶段2 精度回溯

阶段1 - 性能搜索（从 V3 基线开始）：
1. 从 V3 达标算子集出发
2. 逐个试禁用算子，仅当禁用后吞吐 > 当前最优才提交
3. 基线动态推进（每次提交后更新基线为新的更高吞吐）
4. 全程不测精度，只追求性能
5. 产出：按吞吐从高到低排序的候选组合列表

阶段2 - 精度回溯：
1. 从性能最优组合开始，按吞吐降序逐个测精度
2. 第一个精度达标的组合即为 v4-final
3. 若全部不达标，回退到 V3 等价（继承 V3 精度结论，不重复终检）
4. V4 成立条件：超越 V3 + 保留≥1算子 + 精度达标

与旧 operator_reduction.py 的区别：
- 新工作流无本地 V1，V4 不与 V1 比较
- 性能基准是 V3，不是 V1
- 精度基准是外部 NV reference（通过 v3_accuracy.py）

架构约定（M1b，见 memory engine-takeover-direction）：
- 本模块只做**算法 + 测量**，经注入的 executor 执行命令；不直接持有 context。
- revision 的创建由 Engine 注入的 `revision_factory` 完成（Engine 是 context 唯一写入者）。
- 阶段1 与阶段2 可能跨进程（步骤11 / 步骤12 分步执行），候选以**可序列化 dict**
  在 artifact 中传递，阶段2 用同一 factory 按 revision_id 取回/重建 revision。
"""

import json
import logging
import os
from typing import Callable, Dict, List, Optional, Tuple

from ..schemas.context_v2 import OperatorRevision
from ..artifacts.registry import ArtifactRegistry
from ..engine.command_executor import CommandExecutor, SubprocessExecutor

# 候选 revision 的创建回调：由 Engine 注入（Engine 是 context 唯一写入者）。
# 签名：(revision_id, parent_revision_id, enabled_ops, disabled_ops_map, note) -> OperatorRevision
# 要求**幂等**：revision_id 已存在时直接返回既有 revision（阶段2 跨进程取回）。
RevisionFactory = Callable[
    [str, Optional[str], List[str], Dict[str, str], str], OperatorRevision
]

# 禁用原因前缀（含 "performance" 以便归类到 disable_reason_categories.v4_performance）
DISABLE_REASON_PREFIX = "v4 performance"


class V4OperatorReduction:
    """V4 减算子性能优化"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
        executor: Optional[CommandExecutor] = None,
        revision_factory: Optional[RevisionFactory] = None,
        reference_model: str = "",
        nv_baseline_file: str = "/flagos-workspace/shared/nv_baseline.yaml",
    ):
        self.workspace_root = workspace_root
        self.container_name = container_name
        self.artifact_registry = artifact_registry or ArtifactRegistry(workspace_root)
        self.executor = executor or SubprocessExecutor()
        self.revision_factory = revision_factory or self._local_revision_factory
        self.reference_model = reference_model
        self.nv_baseline_file = nv_baseline_file
        self.logger = logging.getLogger("workflow.domain.v4_reduction")

    # ------------------------------------------------------------------
    # 阶段1：性能搜索（不测精度）
    # ------------------------------------------------------------------

    def performance_search(
        self,
        v4_r0: OperatorRevision,
        max_trials: Optional[int] = None,
    ) -> Dict:
        """阶段1：性能搜索（不测精度）

        Args:
            v4_r0: v4-r0（v3-final 的不可变克隆），搜索起点与性能基线
            max_trials: 最多试禁用的算子数（None = 不限；用于预算封顶）

        Returns:
            **可序列化**的阶段1 报告（直接作为 artifact 内容跨步传递）：
            {
              execution_success, baseline_throughput, trials,
              candidate_count, candidates: [...], reason,
            }
            candidates 按吞吐降序，每项：
            {revision_id, parent_revision_id, throughput, enabled_ops,
             disabled_ops, enabled_count, disabled_count, performance_artifact, note}
            无任何合法提升时 candidates=[] 且 reason=no_valid_improvement
            （→ V4 不成立，回退 V3）
        """
        self.logger.info("Phase 1: Performance search (no accuracy testing)")

        baseline_ok, v3_throughput, _ = self._measure_throughput(v4_r0)
        if not baseline_ok:
            self.logger.error("V4-r0 baseline throughput measurement failed")
            return {
                "execution_success": False,
                "baseline_revision_id": v4_r0.revision_id,
                "baseline_throughput": 0.0,
                "trials": 0,
                "candidate_count": 0,
                "candidates": [],
                "reason": "baseline_measurement_failed",
            }
        self.logger.info(f"V4-r0 baseline throughput: {v3_throughput:.1f} tokens/s")

        current_baseline = v3_throughput
        current_revision = v4_r0
        candidates: List[Dict] = []
        trials = 0

        # 逐个试禁用算子（遍历起点算子集，保证顺序确定）
        for op_name in v4_r0.enabled_ops:
            if max_trials is not None and trials >= max_trials:
                self.logger.info(f"Reached max_trials ({max_trials}), stopping search")
                break

            # 保底至少保留 1 个算子（plugin 也不例外）
            if len(current_revision.enabled_ops) <= 1:
                self.logger.info("Reached minimum operator count (1), stopping search")
                break

            enabled = [op for op in current_revision.enabled_ops if op != op_name]
            note = f"{DISABLE_REASON_PREFIX}: disable {op_name} (phase1 trial)"
            trial_revision = self.revision_factory(
                f"v4-t{trials + 1}",
                current_revision.revision_id,
                enabled,
                {op_name: note},
                note,
            )
            trials += 1

            ok, trial_throughput, perf_artifact = self._measure_throughput(trial_revision)
            if not ok:
                # 测量失败不提交（不把失败当提升）
                self.logger.warning(f"Trial disable {op_name}: measurement failed, skip")
                continue

            self.logger.info(
                f"Trial disable {op_name}: throughput={trial_throughput:.1f} "
                f"(baseline={current_baseline:.1f})"
            )

            # 仅当绝对性能优于当前最优才推进
            if trial_throughput > current_baseline:
                self.logger.info("Improvement found, advancing baseline")
                current_baseline = trial_throughput
                current_revision = trial_revision
                candidates.append({
                    "revision_id": trial_revision.revision_id,
                    "parent_revision_id": trial_revision.parent_revision_id,
                    "throughput": trial_throughput,
                    "enabled_ops": list(trial_revision.enabled_ops),
                    "disabled_ops": dict(trial_revision.disabled_ops),
                    "enabled_count": len(trial_revision.enabled_ops),
                    "disabled_count": len(trial_revision.disabled_ops),
                    "performance_artifact": perf_artifact,
                    "note": note,
                })

        # 按吞吐降序排序
        candidates.sort(key=lambda x: x["throughput"], reverse=True)

        self.logger.info(f"Phase 1 complete: {len(candidates)} candidates ({trials} trials)")
        return {
            "execution_success": True,
            "baseline_revision_id": v4_r0.revision_id,
            "baseline_throughput": v3_throughput,
            "trials": trials,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "reason": "" if candidates else "no_valid_improvement",
        }

    # ------------------------------------------------------------------
    # 阶段2：精度回溯（含选中候选的全数据集终检）
    # ------------------------------------------------------------------

    def accuracy_backtrack(
        self,
        v4_r0: OperatorRevision,
        candidates: List[Dict],
        datasets: List[str],
    ) -> Tuple[Optional[OperatorRevision], Dict]:
        """阶段2：精度回溯

        按吞吐降序逐个测精度，第一个全数据集达标的组合即为 v4-final。
        该组合的全数据集评测即**最终精度终检**（plan §8.6）——评测以小时计，
        不在选中的候选上重复评测。

        Args:
            v4_r0: 搜索起点（用于回退说明，不参与评测）
            candidates: 阶段1 产出的可序列化候选（按吞吐降序）
            datasets: 精度评测数据集

        Returns:
            (v4-final revision 或 None, 报告 dict)
        """
        self.logger.info(f"Phase 2: Accuracy backtrack ({len(candidates)} candidates)")

        from .v3_accuracy import V3AccuracyEvaluation

        evaluator = V3AccuracyEvaluation(
            workspace_root=self.workspace_root,
            container_name=self.container_name,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
        )

        last_results: Dict[str, Dict] = {}
        for idx, candidate in enumerate(candidates):
            revision = self.revision_factory(
                candidate["revision_id"],
                candidate.get("parent_revision_id"),
                list(candidate.get("enabled_ops", [])),
                dict(candidate.get("disabled_ops", {})),
                candidate.get("note", ""),
            )
            throughput = candidate["throughput"]

            self.logger.info(
                f"Testing candidate {idx + 1}/{len(candidates)}: "
                f"revision={revision.revision_id}, throughput={throughput:.1f}, "
                f"ops={len(revision.enabled_ops)}"
            )

            # 评测精度（每个数据集独立判定，判定权在 accuracy_compare 退出码）
            all_qualified, results = evaluator.evaluate_accuracy(
                candidate="v4",
                revision=revision,
                datasets=datasets,
                reference_model=self.reference_model,
                nv_baseline_file=self.nv_baseline_file,
            )

            last_results = results

            # 逐数据集登记精度 artifact（无论达标与否，证据不覆盖）
            for dataset, result in results.items():
                evaluator.register_accuracy_artifact(
                    "v4",
                    dataset,
                    result,
                    f"results/accuracy/v4-{revision.revision_id}-{dataset}.json",
                )

            if all_qualified:
                self.logger.info(f"Candidate {idx + 1} passed accuracy, V4 established")
                report = {
                    "success": True,
                    "phase1_candidates": len(candidates),
                    "phase2_tested": idx + 1,
                    "v4_throughput": throughput,
                    "v4_revision_id": revision.revision_id,
                    "v4_operator_count": len(revision.enabled_ops),
                    "v4_disabled_count": len(revision.disabled_ops),
                    "accuracy_results": results,
                    "fallback_to_v3": False,
                }
                return revision, report

            self.logger.warning(f"Candidate {idx + 1} failed accuracy")

        # 全部不达标，回退到 V3 等价
        self.logger.warning("All candidates failed accuracy, V4 not established")
        return None, {
            "success": False,
            "phase1_candidates": len(candidates),
            "phase2_tested": len(candidates),
            "execution_success": True,
            "established": False,
            "fallback_to_v3": True,
            "reason": "accuracy_not_met",
            # 末个候选的精度结果（失败原因落证；选中候选的即终检结果）
            "last_accuracy_results": last_results,
            "accuracy_results": {},
        }

    # ------------------------------------------------------------------
    # 两阶段组合入口（同一进程内跑完，供 M4 收敛前 / 单测使用）
    # ------------------------------------------------------------------

    def optimize_v4(
        self,
        v3_final: OperatorRevision,
        datasets: List[str],
    ) -> Tuple[bool, Optional[OperatorRevision], Dict]:
        """V4 减算子优化（阶段1 + 阶段2 组合）

        Returns:
            (V4 是否成立, v4-final revision, 优化报告)

        V4 成立条件：
        1. 性能超越 V3（吞吐 > V3）
        2. 至少保留 1 个算子
        3. 精度相对退化 ≤ 5%（每个数据集独立判定）
        """
        self.logger.info(f"Starting V4 optimization from V3 baseline: {v3_final.revision_id}")

        search = self.performance_search(v3_final)
        candidates = search["candidates"]
        if not candidates:
            self.logger.warning("Phase 1 yielded no performance improvements")
            return False, None, {
                "success": False,
                "execution_success": search["execution_success"],
                "established": False,
                "reason": search["reason"] or "no_valid_improvement",
                "fallback_to_v3": True,
                "phase1_candidates": 0,
                "phase2_tested": 0,
                "accuracy_results": {},
            }

        v4_final, report = self.accuracy_backtrack(v3_final, candidates, datasets)
        report.setdefault("phase1_candidates", len(candidates))
        return (v4_final is not None), v4_final, report

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _measure_throughput(
        self,
        revision: OperatorRevision,
    ) -> Tuple[bool, float, str]:
        """测量吞吐（quick benchmark，经注入的 executor）

        Returns:
            (是否测量成功, throughput tokens/s, performance artifact id)
        """
        from .v3_performance import V3PerformanceMeasurement

        measurer = V3PerformanceMeasurement(
            workspace_root=self.workspace_root,
            container_name=self.container_name,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
        )

        success, perf_data = measurer.measure_performance(
            candidate="v4",
            revision=revision,
            mode="quick",
        )

        if not success:
            self.logger.error(f"Throughput measurement failed for {revision.revision_id}")
            return False, 0.0, ""

        return (
            True,
            perf_data.get("throughput_tokens_per_sec", 0.0),
            perf_data.get("artifact_id", ""),
        )

    @staticmethod
    def _local_revision_factory(
        revision_id: str,
        parent_revision_id: Optional[str],
        enabled_ops: List[str],
        disabled_ops: Dict[str, str],
        note: str,
    ) -> OperatorRevision:
        """默认 factory（未注入 Engine 时）：本地构造 OperatorRevision，不写 context。

        生产路径由 Engine 注入 factory（Engine 是 context 唯一写入者）。
        """
        from datetime import datetime

        return OperatorRevision(
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            created_at=datetime.now().isoformat(),
            enabled_ops=list(enabled_ops),
            disabled_ops=dict(disabled_ops),
            disable_reason_categories={
                "startup": [],
                "accuracy": [],
                "v4_performance": list(disabled_ops.keys()),
            },
            frozen=False,
        )
