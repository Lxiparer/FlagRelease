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

"""V4 Operator Reduction - V4 减算子性能优化（Flag-express）

搜索策略（对齐 CLAUDE.md 既有流程与 `operator_reduction.py` 的 `_pick_random_subset`）：
- 每轮从 V3 算子集里**随机选 1~3 个算子，只开启这几个**（其余全关）启动服务
- 性能 > 优化基线（V3 实测吞吐）→ 进精度校验；精度达标即采纳为 v4-final
- **只测两轮**（max_rounds=2）；两轮都拿不到"性能提升"→ 回退 V3，不产出 V4
- 保底：随机选取下限为 1，V4 至少保留 1 个算子

两阶段（plan §7/§「V4」；与引擎步骤11/12 对齐）：
- 阶段1 性能搜索（`performance_search`，不测精度）→ 候选（按吞吐降序）
- 阶段2 精度回溯（`accuracy_backtrack`）→ 首个全数据集达标的候选即 v4-final

随机性可复现：`random.Random(seed)`，seed 由引擎传入并记入 artifact（引擎要确定性，
所以随机必须带种子，否则同一输入两次跑出的 V4 不一样）。

架构约定（M1b，见 memory engine-takeover-direction）：
- 命令经注入的 executor；**每轮重启并核验运行时 oplist**（plan §V4 实施 3，约束27），
  否则测的还是上一轮在跑的服务，试验全部无效
- revision 创建由 Engine 注入的 `revision_factory` 完成（Engine 是 context 唯一写入者）
"""

import json
import logging
import os
import random
from typing import Callable, Dict, List, Optional, Tuple

from ..schemas.context_v2 import OperatorRevision
from ..artifacts.registry import ArtifactRegistry
from ..engine.command_executor import CommandExecutor, SubprocessExecutor
from .v3_startup_tuning import V3StartupTuning

# 候选 revision 的创建回调（由 Engine 注入；幂等，跨进程按 revision_id 取回）
RevisionFactory = Callable[
    [str, Optional[str], List[str], Dict[str, str], str], OperatorRevision
]

# 随机子集规模（对齐 operator_reduction.py：随机选 1~3 个算子只开这几个）
SUBSET_LO = 1
SUBSET_HI = 3

# 禁用原因前缀（含 "performance"/"v4" 以便归类到 disable_reason_categories.v4_performance）
DISABLE_REASON_PREFIX = "v4 performance"

# 运行时 oplist 候选路径（约束27：以运行时 txt 为唯一权威来源）
RUNTIME_OPLIST_CANDIDATES = [
    "/tmp/flaggems_enable_oplist.txt",
    "/tmp/gems.txt",
    "/root/gems.txt",
]


def pick_random_subset(
    pool: List[str],
    rng: random.Random,
    lo: int = SUBSET_LO,
    hi: int = SUBSET_HI,
) -> List[str]:
    """从 pool 里随机选 lo~hi 个算子（V4 只开这几个）。pool 少于 lo 个时全取。

    与 operator_reduction.py 的同名实现语义一致（含排序，保证可复现）。
    """
    k = min(len(pool), rng.randint(lo, min(hi, len(pool))))
    k = max(k, min(1, len(pool)))
    return sorted(rng.sample(pool, k))


class V4OperatorReduction:
    """V4 减算子性能优化（随机子集 + 两轮上限）"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
        executor: Optional[CommandExecutor] = None,
        revision_factory: Optional[RevisionFactory] = None,
        startup: Optional[V3StartupTuning] = None,
        reference_model: str = "",
        nv_baseline_file: str = "/flagos-workspace/shared/nv_baseline.yaml",
        plugin_mode: bool = True,
    ):
        self.workspace_root = workspace_root
        self.container_name = container_name
        self.artifact_registry = artifact_registry or ArtifactRegistry(workspace_root)
        self.executor = executor or SubprocessExecutor()
        self.revision_factory = revision_factory or self._local_revision_factory
        # 复用启动调优的服务重启能力（清缓存 → 下发白名单 → 起服务 → 等就绪）
        self.startup = startup or V3StartupTuning(
            workspace_root=workspace_root,
            container_name=container_name,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
            revision_factory=self.revision_factory,
            plugin_mode=plugin_mode,
        )
        self.reference_model = reference_model
        self.nv_baseline_file = nv_baseline_file
        self.logger = logging.getLogger("workflow.domain.v4_reduction")

    # ------------------------------------------------------------------
    # 阶段1：性能搜索（随机子集，不测精度）
    # ------------------------------------------------------------------

    def performance_search(
        self,
        v4_r0: OperatorRevision,
        baseline_throughput: Optional[float] = None,
        seed: int = 0,
        max_rounds: int = 2,
    ) -> Dict:
        """阶段1：随机子集性能搜索（不测精度）

        Args:
            v4_r0: v4-r0（v3-final 的不可变克隆），随机采样的算子池
            baseline_throughput: 优化基线（V3 实测吞吐，通常取自步骤08 性能 artifact）。
                为 None 时重启 v4-r0 实测一轮作为基线。
            seed: 随机种子（可复现；记入 artifact）
            max_rounds: 搜索轮数上限（默认 2，对齐 operator_reduction.py）

        Returns:
            **可序列化**的阶段1 报告（直接作为 artifact 内容跨步传递）：
            {
              execution_success, baseline_throughput, baseline_source, seed,
              max_rounds, rounds_probed, candidate_count, candidates: [...], reason
            }
            candidates 按吞吐降序，每项：
            {revision_id, parent_revision_id, throughput, gain_pct, enabled_ops,
             disabled_count, performance_artifact, round, sampled_ops, note}
            两轮都无提升 → candidates=[] 且 reason=no_valid_improvement
            （→ V4 不成立，回退 V3）
        """
        self.logger.info(
            f"Phase 1: Random-subset performance search "
            f"(max {max_rounds} rounds, subset {SUBSET_LO}~{SUBSET_HI} ops, seed={seed})"
        )

        # 1. 优化基线：优先用已登记的 V3 实测吞吐（步骤08），避免重复测量同一配置
        baseline_ok = baseline_throughput is not None and baseline_throughput > 0
        baseline_source = "provided" if baseline_ok else ""
        if not baseline_ok:
            baseline_ok, baseline_throughput, _ = self._measure_throughput(
                v4_r0, output_name="v4_baseline",
            )
            baseline_source = "measured"
        if not baseline_ok:
            self.logger.error("V4 baseline throughput unavailable")
            return {
                "execution_success": False,
                "baseline_revision_id": v4_r0.revision_id,
                "baseline_throughput": 0.0,
                "baseline_source": "",
                "seed": seed,
                "max_rounds": max_rounds,
                "rounds_probed": 0,
                "candidate_count": 0,
                "candidates": [],
                "reason": "baseline_measurement_failed",
            }
        self.logger.info(
            f"V4 baseline throughput: {baseline_throughput:.1f} tokens/s ({baseline_source})"
        )

        # 2. 随机子集搜索（种子可复现）
        rng = random.Random(seed)
        candidates: List[Dict] = []
        trials: List[Dict] = []
        pool = list(v4_r0.enabled_ops)

        for round_num in range(1, max_rounds + 1):
            sampled = pick_random_subset(pool, rng)
            if len(sampled) < 1:
                self.logger.warning("Empty sample, stopping search")
                break
            self.logger.info(
                f"[Round {round_num}/{max_rounds}] 随机只开 {len(sampled)} 个算子: {sampled}"
            )

            note = f"{DISABLE_REASON_PREFIX}: round {round_num} sampled subset"
            # V4 = 只开采样到的这几个算子，其余全关
            disabled_map = {
                op: f"{note} (only enable {','.join(sampled)})"
                for op in pool if op not in sampled
            }
            trial = self.revision_factory(
                f"v4-r{round_num}", v4_r0.revision_id, sampled, disabled_map, note,
            )

            ok, throughput, perf_artifact, err, verify = self._restart_and_measure(
                trial, output_name=f"v4_probe_round{round_num}",
            )
            record = {
                "round": round_num,
                "revision_id": trial.revision_id,
                "sampled_ops": sampled,
                "enabled_count": len(sampled),
                "service_ok": ok,
                "error": err,
                "throughput": throughput,
                "oplist_mismatch": bool(verify.get("mismatch")),
                "runtime_op_count": (len(verify["runtime_ops"])
                                     if verify.get("runtime_ops") is not None else None),
            }
            if not ok:
                self.logger.warning(
                    f"Round {round_num}: 服务无法启动 → 本轮作废（{err[:200]}）"
                )
                record["outcome"] = "service_fail"
                trials.append(record)
                continue

            improved = throughput > baseline_throughput
            gain_pct = (
                (throughput - baseline_throughput) / baseline_throughput * 100
                if baseline_throughput > 0 else 0.0
            )
            record["gain_pct"] = round(gain_pct, 2)
            record["outcome"] = "improved" if improved else "no_gain"
            self.logger.info(
                f"Round {round_num}: {throughput:.1f} tok/s (相对基线 {gain_pct:+.2f}%) → "
                f"{'性能提升，进精度校验' if improved else '未超基线'}"
            )
            trials.append(record)

            if improved:
                candidates.append({
                    "revision_id": trial.revision_id,
                    "parent_revision_id": v4_r0.revision_id,
                    "throughput": throughput,
                    "gain_pct": round(gain_pct, 2),
                    "enabled_ops": list(trial.enabled_ops),
                    "disabled_count": len(trial.disabled_ops),
                    "performance_artifact": perf_artifact,
                    "round": round_num,
                    "sampled_ops": sampled,
                    "note": note,
                })

        candidates.sort(key=lambda x: x["throughput"], reverse=True)
        self.logger.info(
            f"Phase 1 complete: {len(candidates)} candidates / {len(trials)} rounds probed"
        )
        return {
            "execution_success": True,
            "baseline_revision_id": v4_r0.revision_id,
            "baseline_throughput": baseline_throughput,
            "baseline_source": baseline_source,
            "seed": seed,
            "max_rounds": max_rounds,
            "rounds_probed": len(trials),
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
        每轮先按候选算子集**重启服务并核验 oplist**（否则测的还是上一轮的配置），
        再评测。该评测即最终精度终检（plan §V4 实施 6）——评测以小时计，不重复跑。

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

            # 1. 按候选算子集重启 + 核验运行时 oplist（约束27）
            ok, err, _verify = self._restart_and_verify(revision)
            if not ok:
                self.logger.error(
                    f"Candidate {idx + 1} 重启失败，跳过：{err[:200]}"
                )
                continue

            # 2. 评测精度（每个数据集独立判定，判定权在 accuracy_compare 退出码）
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

        # 全部不达标（或全部重启失败），回退到 V3
        self.logger.warning("All candidates failed accuracy/restart, V4 not established")
        return None, {
            "success": False,
            "phase1_candidates": len(candidates),
            "phase2_tested": len(candidates),
            "execution_success": True,
            "established": False,
            "fallback_to_v3": True,
            "reason": "accuracy_not_met",
            "last_accuracy_results": last_results,
            "accuracy_results": {},
        }

    # ------------------------------------------------------------------
    # 两阶段组合入口（同一进程内跑完，供单测使用）
    # ------------------------------------------------------------------

    def optimize_v4(
        self,
        v3_final: OperatorRevision,
        datasets: List[str],
        baseline_throughput: Optional[float] = None,
        seed: int = 0,
        max_rounds: int = 2,
    ) -> Tuple[bool, Optional[OperatorRevision], Dict]:
        """V4 减算子优化（阶段1 + 阶段2 组合）

        Returns:
            (V4 是否成立, v4-final revision, 优化报告)
        """
        self.logger.info(f"Starting V4 optimization from V3 baseline: {v3_final.revision_id}")

        search = self.performance_search(
            v3_final, baseline_throughput=baseline_throughput,
            seed=seed, max_rounds=max_rounds,
        )
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
                "search": search,
            }

        v4_final, report = self.accuracy_backtrack(v3_final, candidates, datasets)
        report.setdefault("phase1_candidates", len(candidates))
        return (v4_final is not None), v4_final, report

    # ------------------------------------------------------------------
    # 内部：重启 + 核验 + 测量
    # ------------------------------------------------------------------

    def _restart_and_verify(self, revision: OperatorRevision) -> Tuple[bool, str, Dict]:
        """按 revision 的算子集重启服务并核验运行时 oplist（约束27）

        Returns:
            (是否成功, 错误信息, 核验信息)
            核验信息 {"runtime_ops": [...], "effective_ops": [...], "mismatch": bool}

        Note:
            运行时 txt 与请求集不一致**属正常**（plugin 模式下 txt 含 FlagGems 全量注册
            算子，实际过滤靠 VLLM_FL_FLAGOS_WHITELIST；已与用户确认）。故 mismatch 只作为
            证据记录进 trial，**不阻塞流程、不判该轮无效**。
        """
        ok, crash_info = self.startup.attempt_startup(revision)
        if not ok:
            return False, (crash_info or {}).get("error_message", "startup failed"), {}

        runtime_ops = self._read_runtime_oplist()
        if runtime_ops is None:
            self.logger.warning("未找到运行时 oplist 文件，跳过核验")
            return True, "", {"runtime_ops": None, "effective_ops": None, "mismatch": False}

        requested = set(revision.enabled_ops)
        effective = [op for op in runtime_ops if op in requested]
        mismatch = set(runtime_ops) != requested
        if mismatch:
            # 正常现象（txt = FlagGems 全量注册算子），仅留痕不阻断
            self.logger.info(
                f"运行时 oplist 与请求不同：请求 {len(requested)} 个，运行时 {len(runtime_ops)} 个"
                f"（交集 {len(effective)}）——以运行时为准，不影响本轮测量"
            )
        return True, "", {
            "runtime_ops": runtime_ops, "effective_ops": effective, "mismatch": mismatch,
        }

    def _read_runtime_oplist(self) -> Optional[List[str]]:
        """读运行时 oplist（约束27 的唯一权威来源）；无文件返回 None"""
        for oplist_file in RUNTIME_OPLIST_CANDIDATES:
            res = self.executor.docker_exec(
                self.container_name, f"cat {oplist_file}", timeout=10,
            )
            if not res.ok:
                continue
            ops = [line.strip() for line in (res.stdout or "").split("\n") if line.strip()]
            if ops:
                return ops
        return None

    def _restart_and_measure(
        self,
        revision: OperatorRevision,
        output_name: str,
    ) -> Tuple[bool, float, str, str, Dict]:
        """重启 → 核验 → quick benchmark

        Returns:
            (是否成功, throughput, performance artifact id, 错误信息, 核验信息)
        """
        ok, err, verify = self._restart_and_verify(revision)
        if not ok:
            return False, 0.0, "", err, verify

        from .v3_performance import V3PerformanceMeasurement

        measurer = V3PerformanceMeasurement(
            workspace_root=self.workspace_root,
            container_name=self.container_name,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
        )
        success, perf_data = measurer.measure_performance(
            candidate="v4", revision=revision, mode="quick", output_name=output_name,
        )
        if not success:
            return False, 0.0, "", "benchmark failed", verify
        return (
            True,
            perf_data.get("throughput_tokens_per_sec", 0.0),
            perf_data.get("artifact_id", ""),
            "",
            verify,
        )

    def _measure_throughput(
        self,
        revision: OperatorRevision,
        output_name: str,
    ) -> Tuple[bool, float, str]:
        """测量吞吐（重启 + quick benchmark）"""
        ok, throughput, artifact_id, _, _ = self._restart_and_measure(revision, output_name)
        return ok, throughput, artifact_id

    @staticmethod
    def _local_revision_factory(
        revision_id: str,
        parent_revision_id: Optional[str],
        enabled_ops: List[str],
        disabled_ops: Dict[str, str],
        note: str,
    ) -> OperatorRevision:
        """默认 factory（未注入 Engine 时）：本地构造 OperatorRevision，不写 context"""
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
