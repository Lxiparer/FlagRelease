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

"""V3 Startup Tuning - 启动兼容性算子调优

职责：
1. 启动服务（清缓存 → 应用算子白名单 → 起服务 → 等就绪/捕获崩溃）
2. 确定性诊断（diagnose_ops.py crash-log）
3. 禁用问题算子 → 派生 child revision → 重试
4. 确定性诊断穷尽时才调用 Agent（M3 边界；未注入 agent 即停并说明原因）

与旧实现的关键差异（M1b，见 memory engine-takeover-direction）：
- 命令全部经注入的 executor（旧的 `_attempt_startup` 是「永远返回失败 + 编造崩溃信息」的 mock）
- 诊断真实执行 `diagnose_ops.py crash-log --json`，并遵守约束18：
  `crashed_ops` 为空时看 `candidate_ops`（低置信候选不能直接判"无算子可归因"）
- revision 创建由 Engine 注入的 factory 完成（Engine 是 context 唯一写入者）；
  旧的 `OperatorRevisionStore()` 是空 store，`create_revision(parent=...)` 必然抛
  「Parent revision not found」——该 bug 因 mock 永不触发而从未暴露
"""

import json
import logging
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..schemas.context_v2 import OperatorRevision
from ..artifacts.registry import ArtifactRegistry
from ..agent.protocol import StartupFailureRequest, AnalysisResult
from ..agent.policy_validator import PolicyValidator
from ..agent.session_manager import AgentSessionManager
from ..engine.command_executor import CommandExecutor, SubprocessExecutor, parse_json_output

# 容器内工具/路径
# 注意：`setup_workspace.sh` 只把工具投到 `/flagos-workspace/scripts/`（容器里没有 skills/ 目录）。
# apply_op_config.py 会 `import flagos_op_config`（同目录兄弟模块，SCRIPT_MAP 也投到 scripts/），
# 所以必须在 scripts/ 目录下执行。
DIAGNOSE_OPS = "/flagos-workspace/scripts/diagnose_ops.py"
APPLY_OP_CONFIG_DIR = "/flagos-workspace/scripts"
APPLY_OP_CONFIG = f"{APPLY_OP_CONFIG_DIR}/apply_op_config.py"
SERVICE_LOG = "/flagos-workspace/logs/service.log"
OPS_CONTROL_FILE = "/root/flaggems_ops_control.json"
SERVICE_PORT = 8000

# 缓存目录（约束25：每次启动前必须清理，确保干净状态下暴露问题算子）
CACHE_DIRS = [
    "/root/.triton/cache/",
    "/tmp/triton_cache/",
    "/root/.flaggems/code_cache/",
]

# revision 工厂（Engine 注入）：(id, parent_id, enabled_ops, disabled_map, note) -> revision
RevisionFactory = Callable[
    [str, Optional[str], List[str], Dict[str, str], str], OperatorRevision
]


class V3StartupTuning:
    """V3 启动兼容性算子调优"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        workflow_run_id: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
        executor: Optional[CommandExecutor] = None,
        revision_factory: Optional[RevisionFactory] = None,
        agent=None,  # M3 接线；未注入时确定性诊断穷尽即停
        policy_validator: Optional[PolicyValidator] = None,
        session_manager: Optional[AgentSessionManager] = None,
        model_path: str = "",
        plugin_mode: bool = True,
        startup_timeout: int = 300,
        poll_interval: float = 5.0,
    ):
        self.workspace_root = Path(workspace_root)
        self.container_name = container_name
        self.workflow_run_id = workflow_run_id

        self.artifact_registry = artifact_registry or ArtifactRegistry(str(workspace_root))
        self.executor = executor or SubprocessExecutor()
        self.revision_factory = revision_factory or self._local_revision_factory
        self.agent = agent
        self.policy_validator = policy_validator or PolicyValidator()
        self.session_manager = session_manager
        self.model_path = model_path
        self.plugin_mode = plugin_mode
        self.startup_timeout = startup_timeout
        self.poll_interval = poll_interval
        self.logger = logging.getLogger("workflow.domain.startup_tuning")

    def tune_startup_compatibility(
        self,
        v3_discovered: OperatorRevision,
        max_rounds: int = 5,
    ) -> Tuple[bool, OperatorRevision, Dict]:
        """启动兼容性调优（确定性优先，Agent 兜底）

        Args:
            v3_discovered: v3-discovered revision（启动稳定的起点）
            max_rounds: 最大调优轮次

        Returns:
            (是否稳定, 最终 revision, 报告 dict)
            报告含 rounds / reason / attempts / disabled_by_round / agent_sessions
        """
        self.logger.info(f"Starting startup compatibility tuning (max {max_rounds} rounds)")

        current_revision = v3_discovered
        agent_sessions: List[str] = []
        attempts: List[Dict] = []
        disabled_by_round: Dict[str, List[str]] = {}
        reason = "max_rounds_exhausted"

        for round_num in range(1, max_rounds + 1):
            self.logger.info(f"=== Startup tuning round {round_num}/{max_rounds} ===")

            # 1. 尝试启动（清缓存 → 应用白名单 → 起服务 → 等就绪/取崩溃日志）
            success, crash_info = self.attempt_startup(current_revision)
            attempt = {
                "round": round_num,
                "revision_id": current_revision.revision_id,
                "startup_ok": success,
                "error_type": "" if success else (crash_info or {}).get("error_type", ""),
                "error_message": "" if success else (crash_info or {}).get("error_message", ""),
                "ops_disabled": [],
            }

            if success:
                self.logger.info(f"Service started successfully after {round_num} rounds")
                attempt["ops_disabled"] = []
                attempts.append(attempt)
                reason = "stable"
                break

            # 2. 启动失败 - 确定性诊断
            self.logger.warning(
                f"Startup failed: {(crash_info or {}).get('error_type')}: "
                f"{(crash_info or {}).get('error_message', '')[:200]}"
            )
            diagnosed_ops = self._deterministic_diagnosis(crash_info, current_revision)

            if diagnosed_ops:
                # 确定性诊断命中 - 禁用后重试（每轮都能定位到新算子就继续，不限轮次语义见约束18）
                self.logger.info(f"Deterministic diagnosis found: {diagnosed_ops}")
                attempt["ops_disabled"] = diagnosed_ops
                attempts.append(attempt)
                disabled_by_round[str(round_num)] = diagnosed_ops
                current_revision = self._create_child_revision_with_disabled(
                    current_revision,
                    diagnosed_ops,
                    reason="startup crash (deterministic)",
                )
                continue

            # 3. 确定性诊断穷尽 - 交给 Agent（M3 边界）
            attempts.append(attempt)
            if self.agent is None:
                self.logger.error(
                    "Deterministic diagnosis exhausted and no AnalysisAgent injected "
                    "(M3 boundary); stopping startup tuning"
                )
                reason = "diagnosis_exhausted_agent_unavailable"
                break

            self.logger.info("Deterministic diagnosis exhausted, invoking Agent")
            agent_result, session_id = self._invoke_agent_for_startup_failure(
                current_revision, crash_info,
            )
            if session_id:
                agent_sessions.append(session_id)

            if agent_result.status != "hypothesis_available":
                self.logger.error(f"Agent returned status: {agent_result.status}")
                reason = f"agent_status:{agent_result.status}"
                break

            passed, errors = self._validate_agent_output(
                agent_result, current_revision, crash_info,
            )
            if not passed:
                self.logger.error(f"Agent output validation failed: {errors}")
                reason = "agent_output_rejected"
                break

            verified, message, new_revision = self._run_agent_experiment(
                current_revision, agent_result, session_id,
            )
            if verified and new_revision is not None:
                current_revision = new_revision
                continue
            self.logger.warning(f"Verification failed: {message}")

        stable = reason == "stable"
        report = {
            "success": stable,
            "rounds": len(attempts),
            "reason": reason,
            "stable_revision_id": current_revision.revision_id if stable else "",
            "final_revision_id": current_revision.revision_id,
            "enabled_count": len(current_revision.enabled_ops),
            "disabled_count": len(current_revision.disabled_ops),
            "attempts": attempts,
            "disabled_by_round": disabled_by_round,
            "agent_sessions": agent_sessions,
            "execution_success": True,
        }
        if not stable:
            self.logger.error(f"Startup tuning failed: {reason}")
        return stable, current_revision, report

    # ------------------------------------------------------------------
    # 启动 / 诊断
    # ------------------------------------------------------------------

    def attempt_startup(
        self,
        revision: OperatorRevision,
    ) -> Tuple[bool, Optional[Dict]]:
        """尝试启动服务并等待就绪

        Returns:
            (是否成功, 崩溃信息)  —— 崩溃信息含 error_type/error_message/service_log/log_tail
        """
        self.logger.info(f"Attempting startup with revision {revision.revision_id}")

        # 0. 先停旧服务（约束15：模式切换/重启前必须停服务释放 GPU）
        #    调优轮次会反复重启，上一轮若留下半启动的 vLLM 会占住端口 → 本轮必然失败
        self._stop_service()

        # 1. 清缓存（约束25）
        self._clear_caches()

        # 2. 应用算子白名单（约束26：统一白名单，禁用 toggle_flaggems 全量重置）
        ok, err = self._apply_op_whitelist(revision.enabled_ops, revision.revision_id)
        if not ok:
            return False, {
                "error_type": "op_config",
                "error_message": err,
                "service_log": SERVICE_LOG,
            }

        # 3. 启动服务（detached：起服务本身耗时长，不能前台阻塞）
        ok, err = self._start_service()
        if not ok:
            return False, {
                "error_type": "start_command",
                "error_message": err,
                "service_log": SERVICE_LOG,
            }

        # 4. 等就绪
        if self._wait_for_service_ready():
            return True, None

        # 5. 未就绪 → 读服务日志尾，供确定性诊断
        log_tail = self._read_service_log_tail()
        return False, {
            "error_type": "crash",
            "error_message": (log_tail.strip().split("\n")[-1] if log_tail.strip()
                              else "service not ready; no log output"),
            "service_log": SERVICE_LOG,
            "log_tail": log_tail,
        }

    def _stop_service(self):
        """停掉容器内的 vLLM 服务并等端口释放（约束15）

        只杀 vLLM 进程，不 `docker restart` 整个容器——容器里的模型缓存/环境不必重建，
        重启容器留给编排层在段结束时做。
        """
        self.executor.docker_exec(
            self.container_name, "pkill -f vllm 2>/dev/null; true", timeout=60,
        )
        # 等端口释放：curl 的 http_code 为 000/空 表示无人监听
        for _ in range(6):
            res = self.executor.docker_exec(
                self.container_name,
                f"curl -s -o /dev/null -w '%{{http_code}}' "
                f"http://localhost:{SERVICE_PORT}/health || true",
                timeout=10,
            )
            if (res.stdout or "").strip() in ("", "000"):
                self.logger.info("旧服务已停止，端口已释放")
                return
            if self.poll_interval > 0:
                time.sleep(self.poll_interval)
        self.logger.warning("旧服务 30s 内未释放端口，继续启动（新服务可能失败）")

    def _clear_caches(self):
        """清理 Triton/FlagGems 编译缓存（约束25）"""
        for cache_dir in CACHE_DIRS:
            res = self.executor.docker_exec(self.container_name, f"rm -rf {cache_dir}")
            if res.ok:
                self.logger.info(f"Cleared cache: {cache_dir}")
            else:
                self.logger.warning(f"Failed to clear {cache_dir}: {res.stderr[:200]}")

    def _apply_op_whitelist(self, enabled_ops: List[str], revision_id: str) -> Tuple[bool, str]:
        """把本 revision 的算子白名单应用到容器（约束26）

        - plugin 场景：apply_op_config.py --mode custom --flagos-whitelist（产出 env_inline，
          起服务时内联注入；plugin 模式禁用控制文件）
        - 非 plugin 场景：写 /root/flaggems_ops_control.json {"include": [...]}
        """
        if not enabled_ops:
            return False, f"revision {revision_id} 的启用算子为空，拒绝启动"

        if self.plugin_mode:
            whitelist = ",".join(enabled_ops)
            script = (
                f"cd {APPLY_OP_CONFIG_DIR} && python3 {APPLY_OP_CONFIG} "
                f"--mode custom --flagos-whitelist '{whitelist}'"
            )
            res = self.executor.docker_exec(self.container_name, script, timeout=300)
            if not res.ok:
                return False, f"apply_op_config exit={res.returncode}: {res.stderr[:300]}"
            data = self._safe_json(res.stdout)
            if not isinstance(data, dict) or not data.get("env_inline"):
                return False, "apply_op_config 未产出 env_inline"
            self._env_inline = data["env_inline"]
            self.logger.info(
                f"Applied plugin whitelist ({len(enabled_ops)} ops) for {revision_id}"
            )
            return True, ""

        # 非 plugin：控制文件即唯一权威输入
        payload = json.dumps({"include": enabled_ops}, ensure_ascii=False)
        script = f"cat > {OPS_CONTROL_FILE} << 'OPS_EOF'\n{payload}\nOPS_EOF"
        res = self.executor.docker_exec(self.container_name, script, timeout=60)
        if not res.ok:
            return False, f"写算子控制文件失败 exit={res.returncode}: {res.stderr[:300]}"
        self._env_inline = ""
        self.logger.info(f"Applied ops control file ({len(enabled_ops)} ops) for {revision_id}")
        return True, ""

    def _start_service(self) -> Tuple[bool, str]:
        """启动 vLLM 服务（detached）"""
        env_inline = getattr(self, "_env_inline", "") or "VLLM_PLUGINS=fl USE_FLAGGEMS=1"
        prefix = f"{env_inline} " if env_inline else ""
        script = (
            f"cd /flagos-workspace && "
            f"{prefix}python3 -m vllm.entrypoints.openai.api_server "
            f"--model {self.model_path} --port {SERVICE_PORT} > {SERVICE_LOG} 2>&1"
        )
        res = self.executor.docker_exec(self.container_name, script, detach=True)
        if not res.ok:
            return False, f"服务启动命令失败 exit={res.returncode}: {res.stderr[:300]}"
        return True, ""

    def _wait_for_service_ready(self) -> bool:
        """轮询健康检查直到就绪或超时（timeout=0 → 只探一次，供单测/快速探测）"""
        deadline = time.time() + self.startup_timeout
        while True:
            res = self.executor.docker_exec(
                self.container_name,
                f"curl -s http://localhost:{SERVICE_PORT}/health",
                timeout=10,
            )
            if res.ok:
                self.logger.info("Service is ready")
                return True
            if time.time() >= deadline:
                self.logger.error(f"Service not ready after {self.startup_timeout}s")
                return False
            if self.poll_interval > 0:
                time.sleep(self.poll_interval)

    def _read_service_log_tail(self, lines: int = 200) -> str:
        """读服务日志尾部（崩溃诊断的输入）"""
        res = self.executor.docker_exec(
            self.container_name, f"tail -n {lines} {SERVICE_LOG}", timeout=30,
        )
        return res.stdout if res.ok else ""

    def _deterministic_diagnosis(
        self,
        crash_info: Optional[Dict],
        revision: Optional[OperatorRevision] = None,
    ) -> Optional[List[str]]:
        """确定性诊断：diagnose_ops.py crash-log → 问题算子

        约束18：`crashed_ops` 为空不等于"无算子可归因"——先看 `candidate_ops`
        （正则命中但白名单外的低置信候选）。两者皆空才算诊断穷尽（返回 None）。

        Returns:
            待禁用算子列表；诊断穷尽返回 None
        """
        crash_info = crash_info or {}
        # 只有真正的服务崩溃才做崩溃日志诊断；配置/启动命令错误不是算子问题
        if crash_info.get("error_type") != "crash":
            self.logger.info(
                f"error_type={crash_info.get('error_type')} 非崩溃，跳过算子诊断"
            )
            return None

        log_path = crash_info.get("service_log")
        if not log_path:
            # 无日志路径 → 无法确定性诊断（视为穷尽）
            self.logger.info("No service log path in crash info, diagnosis exhausted")
            return None

        self.logger.info("Running deterministic diagnosis (diagnose_ops.py crash-log)")
        script = f"python3 {DIAGNOSE_OPS} crash-log --log-path {log_path}"

        ops_file = self._write_ops_file(revision) if revision is not None else ""
        if ops_file:
            script += f" --ops-file {ops_file}"
        script += " --json"

        # 注意：crash-log 在 crashed_ops 为空时以 exit 1 退出，退出码不是错误信号，
        # 必须解析 stdout。
        res = self.executor.docker_exec(self.container_name, script, timeout=600)
        data = self._safe_json(res.stdout)
        if not isinstance(data, dict):
            self.logger.warning(
                f"diagnose_ops 输出不可解析: exit={res.returncode}, {res.stderr[:200]}"
            )
            return None

        crashed = list(data.get("crashed_ops") or [])
        candidates = list(data.get("candidate_ops") or [])

        # 只保留本 revision 实际启用的算子（已禁用的不重复禁）
        if revision is not None:
            enabled = set(revision.enabled_ops)
            crashed = [op for op in crashed if op in enabled]
            candidates = [op for op in candidates if op in enabled]

        ops = crashed or candidates
        if not ops:
            self.logger.info("Diagnosis exhausted: no crashed_ops / candidate_ops")
            return None
        if not crashed and candidates:
            self.logger.info(f"No crashed_ops; falling back to candidate_ops: {candidates}")
        return ops

    def _write_ops_file(self, revision: OperatorRevision) -> str:
        """把本 revision 的启用算子落盘为 ops_list.json（提高 diagnose_ops 匹配准确率）"""
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

    @staticmethod
    def _safe_json(text: str):
        """从可能混杂日志的 stdout 中提取 JSON 对象（best-effort，共享实现）"""
        return parse_json_output(text)

    # ------------------------------------------------------------------
    # revision 派生
    # ------------------------------------------------------------------

    def _create_child_revision_with_disabled(
        self,
        parent: OperatorRevision,
        ops_to_disable: List[str],
        reason: str,
    ) -> OperatorRevision:
        """创建禁用指定算子的 child revision（经注入的 factory，Engine 落 context）"""
        additional_disabled = {op: reason for op in ops_to_disable}
        new_enabled_ops = [op for op in parent.enabled_ops if op not in ops_to_disable]
        child_id = self._generate_child_revision_id(parent.revision_id)

        return self.revision_factory(
            child_id, parent.revision_id, new_enabled_ops, additional_disabled, reason,
        )

    def _generate_child_revision_id(self, parent_id: str) -> str:
        """生成 child revision ID（v3-startup-rN 递增）"""
        if "-r" in parent_id:
            base, seq = parent_id.rsplit("-r", 1)
            try:
                return f"{base}-r{int(seq) + 1}"
            except ValueError:
                pass
        return f"{parent_id}-r1"

    @staticmethod
    def _local_revision_factory(
        revision_id: str,
        parent_revision_id: Optional[str],
        enabled_ops: List[str],
        disabled_ops: Dict[str, str],
        note: str,
    ) -> OperatorRevision:
        """默认 factory（未注入 Engine 时）：本地构造，不写 context"""
        from datetime import datetime

        return OperatorRevision(
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            created_at=datetime.now().isoformat(),
            enabled_ops=list(enabled_ops),
            disabled_ops=dict(disabled_ops),
            disable_reason_categories={
                "startup": list(disabled_ops.keys()),
                "accuracy": [],
                "v4_performance": [],
            },
            frozen=False,
        )

    # ------------------------------------------------------------------
    # Agent 分支（M3）：诊断穷尽时的受约束分析器
    # ------------------------------------------------------------------

    def _invoke_agent_for_startup_failure(
        self,
        revision: OperatorRevision,
        crash_info: Dict,
    ) -> Tuple[AnalysisResult, Optional[str]]:
        """调用 Agent 分析启动失败"""
        request = StartupFailureRequest(
            schema_version="1.0",
            analysis_type="startup_failure",
            workflow_run_id=self.workflow_run_id,
            candidate="v3",
            operator_revision=revision.revision_id,
            input_artifacts=[],
            operator_constraints={
                "discovered_set": revision.enabled_ops,
                "allow_fallback_to_installed_catalog": False,
                "require_direct_log_evidence_for_fallback": True,
            },
            allowed_experiments=["disable_ops_and_restart"],
            limits={
                "max_candidate_ops": 3,
                "max_tool_rounds": 12,
                "timeout_seconds": 900,
            },
        )

        session = self.session_manager.create_session(request)
        result = self.agent.analyze_startup_failure(request)
        self.session_manager.update_session_result(session.session_id, result)
        return result, session.session_id

    def _validate_agent_output(
        self,
        result: AnalysisResult,
        revision: OperatorRevision,
        crash_info: Dict,
    ) -> Tuple[bool, List[str]]:
        """校验 Agent 输出（PolicyValidator，Agent 不能越权）"""
        request = StartupFailureRequest(
            workflow_run_id=self.workflow_run_id,
            operator_revision=revision.revision_id,
            operator_constraints={"discovered_set": revision.enabled_ops},
            allowed_experiments=["disable_ops_and_restart"],
            limits={"max_candidate_ops": 3},
        )
        return self.policy_validator.validate_analysis_result(
            result, request, installed_operator_catalog=None,
        )

    def _run_agent_experiment(
        self,
        revision: OperatorRevision,
        agent_result: AnalysisResult,
        session_id: Optional[str],
    ) -> Tuple[bool, str, Optional[OperatorRevision]]:
        """执行 Agent 建议的实验并返回新 revision

        TODO(M3)：VerificationExperimentExecutor 尚未回传新建的 child revision，
        当前仅复用本轮「禁用 + 重启」结果——Agent 分支接线时一并收敛（见 plan §4.2）。
        """
        from ..engine.verification_executor import VerificationExperimentExecutor

        executor = VerificationExperimentExecutor(str(self.workspace_root), self.artifact_registry)
        success, message, artifact_id = executor.execute_experiment(
            parent_revision=revision,
            experiment=agent_result.recommended_experiment,
            agent_result=agent_result,
        )
        if session_id and self.session_manager is not None:
            self.session_manager.update_verification_result(
                session_id,
                verification_status="success" if success else "failed",
                verification_artifact=artifact_id,
            )
        if not success:
            return False, message, None

        # 验证成功：禁用 Agent 建议的算子，派生新 revision
        suggested = list(getattr(agent_result.recommended_experiment, "disabled_ops", []) or [])
        if not suggested:
            return True, message, None
        new_revision = self._create_child_revision_with_disabled(
            revision, suggested, reason=f"startup crash (agent verified): {message[:120]}",
        )
        return True, message, new_revision
