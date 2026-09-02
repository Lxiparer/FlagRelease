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
2. 逐个试禁用算子，仅当禁用后吞吐 > 当前基线才提交
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
"""

import json
import logging
import os
from typing import Dict, List, Optional, Tuple

from ..schemas.context_v2 import OperatorRevision
from ..artifacts.registry import ArtifactRegistry
from ..gates.reducer import GateReducer


class V4OperatorReduction:
    """V4 减算子性能优化"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
        gate_reducer: Optional[GateReducer] = None,
    ):
        self.workspace_root = workspace_root
        self.container_name = container_name
        self.artifact_registry = artifact_registry or ArtifactRegistry(workspace_root)
        self.gate_reducer = gate_reducer or GateReducer(self.artifact_registry)
        self.logger = logging.getLogger("workflow.domain.v4_reduction")

    def optimize_v4(
        self,
        v3_final: OperatorRevision,
        datasets: List[str],
    ) -> Tuple[bool, Optional[OperatorRevision], Dict]:
        """V4 减算子优化（两阶段）

        Args:
            v3_final: V3 最终 revision（作为起点和性能基线）
            datasets: 精度评测数据集列表

        Returns:
            (V4 是否成立, v4-final revision, 优化报告)

        V4 成立条件：
        1. 性能超越 V3（吞吐 > V3）
        2. 至少保留 1 个算子
        3. 精度相对退化 ≤ 5%（每个数据集独立判定）
        """
        self.logger.info(f"Starting V4 optimization from V3 baseline: {v3_final.revision_id}")

        # 阶段1：性能搜索（不测精度）
        candidates = self._phase1_performance_search(v3_final)

        if not candidates:
            self.logger.warning("Phase 1 yielded no performance improvements")
            # V4 不成立（未能超越 V3）
            return False, None, {
                "success": False,
                "reason": "no_performance_improvement",
                "fallback_to_v3": True,
            }

        # 阶段2：精度回溯（按吞吐降序）
        v4_final, report = self._phase2_accuracy_backtrack(
            v3_final,
            candidates,
            datasets,
        )

        if v4_final is None:
            # 全部候选不达标，回退到 V3 等价
            self.logger.warning("Phase 2: all candidates failed accuracy, fallback to V3")
            return False, None, {
                "success": False,
                "reason": "accuracy_not_met",
                "fallback_to_v3": True,
            }

        # V4 成立
        self.logger.info(f"V4 established: {v4_final.revision_id}")
        return True, v4_final, report

    def _phase1_performance_search(
        self,
        v3_baseline: OperatorRevision,
    ) -> List[Dict]:
        """阶段1：性能搜索（不测精度）

        Args:
            v3_baseline: V3 baseline revision

        Returns:
            candidates 列表（按吞吐降序排列）
            每个 candidate: {
                "revision": OperatorRevision,
                "throughput": float,
                "disabled_count": int,
            }
        """
        self.logger.info("Phase 1: Performance search (no accuracy testing)")

        # 测量 V3 baseline 性能
        v3_throughput = self._measure_throughput(v3_baseline)
        self.logger.info(f"V3 baseline throughput: {v3_throughput:.1f} tokens/s")

        current_baseline = v3_throughput
        current_revision = v3_baseline
        candidates = []

        # 逐个试禁用算子
        for op_name in v3_baseline.enabled_ops:
            # 至少保留 1 个算子
            if len(current_revision.enabled_ops) <= 1:
                self.logger.info("Reached minimum operator count (1), stopping search")
                break

            # 创建试探性 revision（禁用 op_name）
            trial_revision = self._create_child_revision_with_disabled(
                parent=current_revision,
                disabled_ops=[op_name],
                reason_category="v4_performance",
                reason=f"Phase 1 trial: disable {op_name}",
            )

            # 测量性能
            trial_throughput = self._measure_throughput(trial_revision)
            self.logger.info(
                f"Trial disable {op_name}: throughput={trial_throughput:.1f} "
                f"(baseline={current_baseline:.1f})"
            )

            # 仅当超越当前基线才提交
            if trial_throughput > current_baseline:
                self.logger.info(f"Improvement found, updating baseline")
                current_baseline = trial_throughput
                current_revision = trial_revision
                candidates.append({
                    "revision": trial_revision,
                    "throughput": trial_throughput,
                    "disabled_count": len(trial_revision.disabled_ops),
                })

        # 按吞吐降序排序
        candidates.sort(key=lambda x: x["throughput"], reverse=True)

        self.logger.info(f"Phase 1 complete: {len(candidates)} candidates")
        return candidates

    def _phase2_accuracy_backtrack(
        self,
        v3_baseline: OperatorRevision,
        candidates: List[Dict],
        datasets: List[str],
    ) -> Tuple[Optional[OperatorRevision], Dict]:
        """阶段2：精度回溯

        Args:
            v3_baseline: V3 baseline（用于回退）
            candidates: 性能排序的候选列表
            datasets: 精度评测数据集

        Returns:
            (v4-final revision, report)
        """
        self.logger.info(f"Phase 2: Accuracy backtrack ({len(candidates)} candidates)")

        from .v3_accuracy import V3AccuracyEvaluation

        evaluator = V3AccuracyEvaluation(
            self.workspace_root,
            self.container_name,
            self.artifact_registry,
        )

        for idx, candidate in enumerate(candidates):
            revision = candidate["revision"]
            throughput = candidate["throughput"]

            self.logger.info(
                f"Testing candidate {idx+1}/{len(candidates)}: "
                f"throughput={throughput:.1f}, ops={len(revision.enabled_ops)}"
            )

            # 评测精度（每个数据集独立判定）
            all_qualified, results = evaluator.evaluate_accuracy(
                candidate="v4",
                revision=revision,
                datasets=datasets,
            )

            if all_qualified:
                # 第一个精度达标的即为 v4-final
                self.logger.info(f"Candidate {idx+1} passed accuracy, V4 established")
                report = {
                    "success": True,
                    "phase1_candidates": len(candidates),
                    "phase2_tested": idx + 1,
                    "v4_throughput": throughput,
                    "v4_operator_count": len(revision.enabled_ops),
                    "accuracy_results": results,
                }
                return revision, report
            else:
                self.logger.warning(f"Candidate {idx+1} failed accuracy")

        # 全部不达标，回退到 V3 等价
        self.logger.warning("All candidates failed accuracy, V4 not established")
        return None, {
            "success": False,
            "phase1_candidates": len(candidates),
            "phase2_tested": len(candidates),
            "reason": "all_candidates_failed_accuracy",
        }

    def _measure_throughput(self, revision: OperatorRevision) -> float:
        """测量吞吐（quick benchmark）

        Args:
            revision: 算子 revision

        Returns:
            throughput (tokens/s)
        """
        # 实际通过 benchmark_runner.py 执行 quick 模式
        # 此处为占位
        from .v3_performance import V3PerformanceMeasurement

        measurer = V3PerformanceMeasurement(
            self.workspace_root,
            self.container_name,
            self.artifact_registry,
        )

        success, perf_data = measurer.measure_performance(
            candidate="v4",
            revision=revision,
            mode="quick",
        )

        if success:
            return perf_data.get("throughput_tokens_per_sec", 0.0)
        else:
            self.logger.error("Throughput measurement failed")
            return 0.0

    def _create_child_revision_with_disabled(
        self,
        parent: OperatorRevision,
        disabled_ops: List[str],
        reason_category: str,
        reason: str,
    ) -> OperatorRevision:
        """创建子 revision（禁用算子）

        Args:
            parent: 父 revision
            disabled_ops: 要禁用的算子列表
            reason_category: 原因分类
            reason: 原因描述

        Returns:
            子 revision
        """
        from ..engine.operator_revision_store import OperatorRevisionStore

        store = OperatorRevisionStore()

        # 合并禁用列表
        new_disabled = parent.disabled_ops.copy()
        new_enabled = parent.enabled_ops.copy()

        for op in disabled_ops:
            if op in new_enabled:
                new_enabled.remove(op)
            if op not in new_disabled:
                new_disabled.append(op)

        # 创建子 revision
        child = store.create_child_revision(
            parent_id=parent.revision_id,
            enabled_ops=new_enabled,
            disabled_ops=new_disabled,
            disable_reason_categories={
                **parent.disable_reason_categories,
                reason_category: parent.disable_reason_categories.get(reason_category, []) + disabled_ops,
            },
            notes=reason,
        )

        return child
