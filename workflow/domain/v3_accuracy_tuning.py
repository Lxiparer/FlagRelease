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

"""V3 Accuracy Tuning - V3 精度算子调优

确定性优先的精度调优（plan §「V3 精度」实施 7）：
1. 测当前精度（全数据集，判定走 accuracy_compare 退出码）
2. 达标即停
3. 不达标 → 由 diagnose_ops.py accuracy-groups 生成**累积禁用候选组**
   （工具生成候选，引擎不手拼搜索循环）
4. 逐组：按候选禁用算子集重启服务 → 核验运行时 oplist（约束27）→ 重新评测
5. 达标即提交该 revision；预算用尽 → 返回失败（流程继续，标记 accuracy_ok=false）
6. 确定性搜索不收敛时才调 Agent（M3 边界；未注入 agent 即按预算耗尽收尾）

与旧实现的差异（M1b，见 memory engine-takeover-direction）：
- 评测/重启全部经注入的 executor；旧的 tuning 循环里精度评测用的是未注入 executor
  的评测器，且达标分支直接 `break` 不更新 revision
- 候选生成委托已固化工具（约束6 的同构原则），不手拼搜索循环
- revision 由 Engine 注入的 factory 创建（Engine 是 context 唯一写入者）；
  旧的 `OperatorRevisionStore()` 是空 store，禁用派生必然抛 Parent not found
"""

import json
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..schemas.context_v2 import OperatorRevision
from ..artifacts.registry import ArtifactRegistry
from ..agent.protocol import AccuracyRegressionRequest, AnalysisResult
from ..agent.policy_validator import PolicyValidator
from ..agent.session_manager import AgentSessionManager
from ..engine.command_executor import CommandExecutor, SubprocessExecutor
from .v3_startup_tuning import V3StartupTuning

# 容器内工具路径
DIAGNOSE_OPS = "/flagos-workspace/scripts/diagnose_ops.py"
RUNTIME_OPLIST_CANDIDATES = [
    "/tmp/flaggems_enable_oplist.txt",
    "/tmp/gems.txt",
    "/root/gems.txt",
]

# revision 工厂（Engine 注入）：(id, parent_id, enabled_ops, disabled_map, note) -> revision
RevisionFactory = Callable[
    [str, Optional[str], List[str], Dict[str, str], str], OperatorRevision
]


class V3AccuracyTuning:
    """V3 精度算子调优（确定性分组搜索优先）"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        workflow_run_id: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
        executor: Optional[CommandExecutor] = None,
        revision_factory: Optional[RevisionFactory] = None,
        startup: Optional[V3StartupTuning] = None,
        agent=None,  # M3 接线
        policy_validator: Optional[PolicyValidator] = None,
        session_manager: Optional[AgentSessionManager] = None,
        plugin_mode: bool = True,
        poll_interval: Optional[float] = None,
    ):
        self.workspace_root = Path(workspace_root)
        self.container_name = container_name
        self.workflow_run_id = workflow_run_id

        self.artifact_registry = artifact_registry or ArtifactRegistry(str(workspace_root))
        self.executor = executor or SubprocessExecutor()
        self.revision_factory = revision_factory or V3StartupTuning._local_revision_factory
        # 复用启动调优的服务重启能力（清缓存 → 下发白名单 → 起服务 → 等就绪）
        self.startup = startup or V3StartupTuning(
            workspace_root=str(workspace_root),
            container_name=container_name,
            workflow_run_id=workflow_run_id,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
            revision_factory=self.revision_factory,
            plugin_mode=plugin_mode,
        )
        self.plugin_mode = plugin_mode
        # 长任务（评测）轮询间隔；测试传 0 以免真的 sleep
        self.poll_interval = poll_interval
        self.agent = agent
        self.policy_validator = policy_validator or PolicyValidator()
        self.session_manager = session_manager
        self.logger = logging.getLogger("workflow.domain.accuracy_tuning")

    def tune_accuracy(
        self,
        current_revision: OperatorRevision,
        datasets: List[str],
        threshold: float = 0.05,
        max_rounds: int = 3,
    ) -> Tuple[bool, OperatorRevision, Dict]:
        """精度算子调优（累积禁用候选组，最多 max_rounds 组）

        Args:
            current_revision: 当前 revision（v3-startup-stable）
            datasets: 数据集列表（每个独立判定，全部达标才成立）
            threshold: 相对退化阈值（默认 5%，仅用于报告展示）
            max_rounds: 最多尝试的候选组数（CLAUDE.md 约束：精度调优最多 3 轮）

        Returns:
            (是否达标, 最终 revision, 报告 dict)
        """
        self.logger.info(
            f"Starting accuracy tuning (threshold={threshold*100}%, max {max_rounds} groups)"
        )

        attempts: List[Dict] = []
        agent_sessions: List[str] = []

        # 1. 先测当前状态（可能已被上游调优带达标）
        qualified, results = self._evaluate(current_revision, datasets)
        attempts.append(self._attempt_record(0, "baseline", current_revision, [], qualified, results))
        if qualified:
            self.logger.info("Accuracy already qualified, no tuning needed")
            return True, current_revision, self._report(True, "already_qualified", attempts,
                                                        agent_sessions, current_revision)

        # 2. 不达标 → 取候选组（工具生成，引擎不手拼搜索循环）
        groups = self._accuracy_groups(current_revision)
        if not groups:
            self.logger.error("No accuracy groups available for deterministic tuning")
            return False, current_revision, self._report(
                False, "no_candidate_groups", attempts, agent_sessions, current_revision,
            )

        # 3. 逐组累积禁用 + 重启 + 核验 + 评测
        base_revision = current_revision
        round_num = 0
        for group in groups:
            if round_num >= max_rounds:
                self.logger.warning(f"Reached max_rounds ({max_rounds}), stopping")
                break
            round_num += 1

            disabled = list(group.get("cumulative_disabled_ops") or [])
            if not disabled:
                continue
            enabled = [op for op in base_revision.enabled_ops if op not in disabled]
            if not enabled:
                # 保底：不能把算子全关（约束：全关等价于关掉 FlagGems）
                self.logger.warning(f"Group {group.get('name')} would disable all ops, skip")
                continue

            self.logger.info(
                f"=== Accuracy tuning round {round_num}/{max_rounds}: "
                f"group={group.get('name')} (cumulative disabled={len(disabled)}) ==="
            )

            note = (
                f"accuracy regression (group {group.get('name')}): "
                f"disable {len(disabled)} ops"
            )
            # 候选 revision（plan §8 命名：v3-accuracy-rN）
            trial = self.revision_factory(
                f"v3-accuracy-r{round_num}", base_revision.revision_id,
                enabled, {op: note for op in disabled}, note,
            )

            # 重启服务（清缓存 + 下发白名单 + 等就绪）
            ok, crash_info = self.startup.attempt_startup(trial)
            if not ok:
                self.logger.error(
                    f"Restart failed for {trial.revision_id}: "
                    f"{(crash_info or {}).get('error_message', '')[:200]}"
                )
                attempts.append(self._attempt_record(
                    round_num, group.get("name", ""), trial, disabled, False, {},
                    restart_ok=False,
                    error=(crash_info or {}).get("error_message", "")[:300],
                ))
                continue

            # 核验运行时 oplist（约束27：以运行时 txt 为唯一权威来源）
            runtime_ops, effective = self._verify_runtime_oplist(trial)
            if effective is not None:
                trial = self._apply_runtime_ops(trial, effective)
            if runtime_ops is not None and len(runtime_ops) <= 1:
                self.logger.warning("Runtime oplist has <=1 op, treat as framework-level issue")

            # 评测
            qualified, results = self._evaluate(trial, datasets)
            attempts.append(self._attempt_record(
                round_num, group.get("name", ""), trial, disabled, qualified, results,
                runtime_op_count=len(runtime_ops) if runtime_ops is not None else None,
            ))
            if qualified:
                self.logger.info(f"Accuracy qualified with group {group.get('name')}")
                return True, trial, self._report(True, "qualified", attempts,
                                                 agent_sessions, trial)

        # 4. 预算用尽 / 全部候选不达标 → Agent 兜底（M3）
        reason = "budget_exhausted"
        if self.agent is None:
            self.logger.error(
                "Deterministic accuracy tuning did not converge and no AnalysisAgent "
                "injected (M3 boundary)"
            )
            reason = "not_converged_agent_unavailable"
        else:
            reason = self._escalate_to_agent(base_revision, datasets, attempts, agent_sessions)

        return False, base_revision, self._report(False, reason, attempts,
                                                  agent_sessions, base_revision)

    # ------------------------------------------------------------------
    # 评测 / 候选 / 核验
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        revision: OperatorRevision,
        datasets: List[str],
    ) -> Tuple[bool, Dict[str, Dict]]:
        """评测精度（判定走 accuracy_compare 退出码）"""
        from .v3_accuracy import V3AccuracyEvaluation

        evaluator = V3AccuracyEvaluation(
            workspace_root=str(self.workspace_root),
            container_name=self.container_name,
            artifact_registry=self.artifact_registry,
            executor=self.executor,
            poll_interval=self.poll_interval,
        )
        all_qualified, results = evaluator.evaluate_accuracy(
            candidate="v3", revision=revision, datasets=datasets,
        )

        # 逐数据集登记精度证据（调优轮次的证据不覆盖，供 Gate 复核/审计）
        for dataset, result in results.items():
            evaluator.register_accuracy_artifact(
                "v3", dataset, result,
                (result.get("details") or {}).get(
                    "evidence_file", f"results/{dataset}_flagos_optimized.json"),
            )
        return all_qualified, results

    def _accuracy_groups(self, revision: OperatorRevision) -> List[Dict]:
        """取累积禁用候选组（diagnose_ops.py accuracy-groups，工具生成候选）"""
        ops_file = self._write_ops_file(revision)
        if not ops_file:
            return []

        script = f"python3 {DIAGNOSE_OPS} accuracy-groups --ops-file {ops_file} --json"
        if not self.plugin_mode:
            script += " --no-plugin"
        res = self.executor.docker_exec(self.container_name, script, timeout=600)
        data = V3StartupTuning._safe_json(res.stdout)
        if not isinstance(data, dict):
            self.logger.error(
                f"accuracy-groups 输出不可解析: exit={res.returncode}, {res.stderr[:200]}"
            )
            return []
        groups = data.get("groups") or []
        self.logger.info(f"Got {len(groups)} cumulative disable groups for accuracy tuning")
        return groups

    def _write_ops_file(self, revision: OperatorRevision) -> str:
        """把当前启用算子落盘为 ops_list.json（accuracy-groups 的输入）"""
        rel = f"results/operator-configs/{revision.revision_id}-ops.json"
        full = self.workspace_root / rel
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                json.dump({"ops": revision.enabled_ops}, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.logger.warning(f"写 ops_file 失败: {e}")
            return ""
        return f"/flagos-workspace/{rel}"

    def _verify_runtime_oplist(
        self,
        revision: OperatorRevision,
    ) -> Tuple[Optional[List[str]], Optional[List[str]]]:
        """核验运行时 oplist（约束27）

        Returns:
            (运行时算子列表 或 None, 生效算子 或 None)
            运行时 txt 不存在时返回 (None, None)——不代表失败，只表示无法核验

        Note:
            运行时 txt 与请求集不同**属正常**（plugin 模式下 txt 含 FlagGems 全量注册算子，
            实际过滤靠 VLLM_FL_FLAGOS_WHITELIST；已与用户确认）。故只留痕不阻断。
            生效集取「请求 ∩ 运行时」——能抓到"请求了运行时不存在的算子"这种真问题。
        """
        for oplist_file in RUNTIME_OPLIST_CANDIDATES:
            res = self.executor.docker_exec(
                self.container_name, f"cat {oplist_file}", timeout=10,
            )
            if not res.ok:
                continue
            ops = [line.strip() for line in (res.stdout or "").split("\n") if line.strip()]
            if not ops:
                continue
            # 实际生效 = 运行时 txt ∩ 本次请求的启用集（txt 是权威来源）
            requested = set(revision.enabled_ops)
            effective = [op for op in ops if op in requested]
            if len(effective) != len(revision.enabled_ops):
                # 正常现象（txt = FlagGems 全量注册算子），仅留痕不阻断
                self.logger.info(
                    f"运行时 oplist 与请求不同：请求 {len(revision.enabled_ops)} 个，"
                    f"运行时 {len(ops)} 个（交集 {len(effective)}）——生效集取交集"
                )
            return ops, effective
        self.logger.warning("未找到运行时 oplist 文件，跳过核验")
        return None, None

    def _apply_runtime_ops(
        self,
        revision: OperatorRevision,
        effective: List[str],
    ) -> OperatorRevision:
        """按运行时核验结果修正 revision 的启用集（不改动已存在 revision 对象）"""
        if set(effective) == set(revision.enabled_ops):
            return revision
        return self.revision_factory(
            f"{revision.revision_id}-verified",
            revision.parent_revision_id,
            effective,
            revision.disabled_ops,
            f"runtime oplist verified ({len(effective)} ops)",
        )

    # ------------------------------------------------------------------
    # 报告
    # ------------------------------------------------------------------

    @staticmethod
    def _attempt_record(
        round_num: int,
        group: str,
        revision: OperatorRevision,
        disabled: List[str],
        qualified: bool,
        results: Dict[str, Dict],
        restart_ok: bool = True,
        error: str = "",
        runtime_op_count: Optional[int] = None,
    ) -> Dict:
        return {
            "round": round_num,
            "group": group,
            "revision_id": revision.revision_id,
            "disabled_ops": disabled,
            "disabled_count": len(disabled),
            "qualified": qualified,
            "restart_ok": restart_ok,
            "error": error,
            "runtime_op_count": runtime_op_count,
            "exit_codes": {
                ds: res.get("exit_code") for ds, res in (results or {}).items()
            },
        }

    @staticmethod
    def _report(
        success: bool,
        reason: str,
        attempts: List[Dict],
        agent_sessions: List[str],
        final_revision: OperatorRevision,
    ) -> Dict:
        return {
            "success": success,
            "reason": reason,
            "rounds": len([a for a in attempts if a["round"] > 0]),
            "final_revision_id": final_revision.revision_id,
            "enabled_count": len(final_revision.enabled_ops),
            "disabled_count": len(final_revision.disabled_ops),
            "attempts": attempts,
            "agent_sessions": agent_sessions,
            "execution_success": True,
        }

    # ------------------------------------------------------------------
    # Agent 分支（M3）：确定性搜索不收敛时
    # ------------------------------------------------------------------

    def _escalate_to_agent(
        self,
        revision: OperatorRevision,
        datasets: List[str],
        attempts: List[Dict],
        agent_sessions: List[str],
    ) -> str:
        """确定性搜索不收敛 → 受约束分析器给候选（不得直接写 disabled_ops / gate）"""
        failed = [a for a in attempts if a["round"] > 0 and not a["qualified"]]
        if not failed:
            return "not_converged"
        dataset = datasets[0] if datasets else "gpqa_diamond"
        self.logger.info(f"Deterministic tuning not converged, invoking Agent ({dataset})")

        request = AccuracyRegressionRequest(
            schema_version="1.0",
            analysis_type="accuracy_regression",
            workflow_run_id=self.workflow_run_id,
            candidate="v3",
            operator_revision=revision.revision_id,
            dataset=dataset,
            input_artifacts=[],
            operator_constraints={
                "discovered_set": revision.enabled_ops,
                "allow_fallback_to_installed_catalog": False,
                "require_direct_log_evidence_for_fallback": True,
            },
            allowed_experiments=["disable_ops_and_restart"],
            limits={"max_candidate_ops": 3, "max_tool_rounds": 12, "timeout_seconds": 900},
        )
        session = self.session_manager.create_session(request)
        result: AnalysisResult = self.agent.analyze_accuracy_regression(request)
        self.session_manager.update_session_result(session.session_id, result)
        agent_sessions.append(session.session_id)

        if result.status != "hypothesis_available":
            return f"agent_status:{result.status}"

        passed, errors = self.policy_validator.validate_analysis_result(
            result, request, installed_operator_catalog=None,
        )
        if not passed:
            self.logger.error(f"Agent output validation failed: {errors}")
            return "agent_output_rejected"

        # 建议必须经实测验证才能提交（Agent 不得直接写 disabled_ops / 精度 Gate）
        return "agent_hypothesis_pending_verification"
