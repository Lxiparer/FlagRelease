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

"""Service Control - 容器内推理服务的启停纪律

**为什么单独抽出来**：起服务前必须先停掉旧服务，否则两种静默错误：

1. 旧 vLLM 占着端口 → 新服务 bind 失败，但健康检查问的是**旧服务**，它会回答 200 →
   引擎以为"起来了"，接着抽到的是**旧服务的 oplist** → 全程"成功"但算子集是错的。
2. privileged 容器里 vLLM 的 multiprocessing worker 会留僵尸进程占着显存，
   只 `pkill` 未必清干净。

因此（对齐既有编排经验）：
- **停服务用 `docker restart <container>`**（宿主机命令），必要时再 `pkill -9` 兜底；
- 停完**等端口真正空出来**再起新服务。
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from ..engine.command_executor import CommandExecutor, parse_json_output

DEFAULT_SERVICE_PORT = 8000
# vLLM/flagscale 的进程特征（restart 失败时的兜底清理）
SERVICE_PROCESS_PATTERNS = "vllm|flagscale"
_RESTART_TIMEOUT = 600
_PORT_FREE_RETRIES = 6

# 容器内既有工具（SCRIPT_MAP 部署到 scripts/）
TOOLS_DIR = "/flagos-workspace/scripts"
DETECT_GPU = f"{TOOLS_DIR}/detect_gpu.py"
CALC_TP_SIZE = f"{TOOLS_DIR}/calc_tp_size.py"
START_SERVICE = f"{TOOLS_DIR}/start_service.sh"
WAIT_FOR_SERVICE = f"{TOOLS_DIR}/wait_for_service.sh"
SERVICE_LOG = "/flagos-workspace/logs/service.log"

# 等就绪预算（对齐既有编排：180s 无活动超时 + 5760s 绝对上限）
SERVICE_READY_STALL_TIMEOUT = 180
SERVICE_READY_MAX_TIMEOUT = 5760


@dataclass
class ServiceParams:
    """起服务所需的全部参数（由引擎从 context.runtime 派生后传入 domain）"""
    model_path: str
    model_name: str
    port: int = DEFAULT_SERVICE_PORT
    tp_size: int = 0
    max_model_len: int = 32768
    thinking: bool = False
    cuda_visible_devices: str = ""
    vllm_plugins: Optional[str] = None
    env: Optional[Dict[str, str]] = None   # 额外环境变量（如算子白名单），经 docker exec -e 透传


def plan_service_devices(
    executor: CommandExecutor,
    container: str,
    tp_needed: int,
    locked_devices: str = "",
    logger: Optional[logging.Logger] = None,
) -> Tuple[Optional[str], str]:
    """决定用哪几张卡（约束14：卡数优先、可换卡但不能改卡数）

    Args:
        tp_needed: 本次需要的卡数（首次由 TP 派生得出）
        locked_devices: 已锁定的卡列表（逗号分隔）；非空表示后续版本

    Returns:
        (可见卡列表 或 None, 失败原因)
    """
    log = logger or logging.getLogger("workflow.domain.service_control")
    info = detect_free_gpus(executor, container, logger=log)
    if info is None:
        # 不知道哪张卡空着就不起服务——否则会撞上别人正在用的卡
        return None, "GPU 空闲探测失败（fail-closed，不在未知占用下起服务）"

    free = [str(i) for i in (info.get("free_gpus") or [])]
    if locked_devices:
        need = len([d for d in locked_devices.split(",") if d.strip()])
        if len(free) >= need:
            chosen = ",".join(free[:need])
            log.info(f"换用空闲卡 {chosen}（卡数锁定为 {need}）")
            return chosen, ""
        # 约束14：空闲卡不足时**复用上次卡列表硬上**（卡数优先）
        log.warning(f"空闲卡不足 {need} 张（有 {len(free)}），复用上次卡列表 {locked_devices}")
        return locked_devices, ""

    if len(free) < tp_needed:
        return None, f"空闲卡不足：需要 {tp_needed} 张，只有 {len(free)} 张空闲"
    chosen = ",".join(free[:tp_needed])
    log.info(f"首次锁定 {tp_needed} 张卡：{chosen}")
    return chosen, ""


def detect_free_gpus(
    executor: CommandExecutor,
    container: str,
    vendor: str = "",
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict]:
    """探测空闲 GPU（调既有 detect_gpu.py，不自己写 nvidia-smi 逻辑）

    Returns:
        {"vendor", "free_gpus", "busy_gpus", "total", "visible_devices_env", ...}
        探测失败返回 None（调用方应 fail-closed，不要在"不知道哪张卡空着"时起服务）
    """
    log = logger or logging.getLogger("workflow.domain.service_control")
    script = f"python3 {DETECT_GPU} --check-free --json"
    if vendor:
        script += f" --vendor {vendor}"
    res = executor.docker_exec(container, script, timeout=120)
    if not res.ok:
        log.error(f"GPU 空闲探测失败：exit={res.returncode} {res.stderr[:200]}")
        return None
    data = parse_json_output(res.stdout)
    if not isinstance(data, dict) or "free_gpus" not in data:
        log.error(f"GPU 空闲探测输出不可解析：{(res.stdout or '')[:200]}")
        return None
    log.info(
        f"GPU 探测：vendor={data.get('vendor')} "
        f"空闲 {len(data.get('free_gpus') or [])}/{data.get('total')} 张"
    )
    return data


def derive_tp_size(
    executor: CommandExecutor,
    container: str,
    model_path: str,
    logger: Optional[logging.Logger] = None,
) -> Optional[int]:
    """按模型权重大小派生 TP（调既有 calc_tp_size.py，不自己估算）

    Returns:
        TP 大小；失败返回 None（调用方 fail-closed）
    """
    log = logger or logging.getLogger("workflow.domain.service_control")
    res = executor.docker_exec(
        container, f"python3 {CALC_TP_SIZE} --model-path {model_path} --json", timeout=300,
    )
    if not res.ok:
        log.error(f"TP 派生失败：exit={res.returncode} {res.stderr[:200]}")
        return None
    data = parse_json_output(res.stdout)
    tp = (data or {}).get("recommended_tp")
    if not isinstance(tp, int) or tp <= 0:
        log.error(f"TP 派生输出不可解析：{(res.stdout or '')[:200]}")
        return None
    log.info(f"TP 派生：recommended_tp={tp}")
    return tp


def start_service(
    executor: CommandExecutor,
    container: str,
    model_path: str,
    model_name: str,
    port: int = DEFAULT_SERVICE_PORT,
    tp_size: int = 0,
    max_model_len: int = 32768,
    thinking: bool = False,
    cuda_visible_devices: str = "",
    vllm_plugins: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    log_file: str = SERVICE_LOG,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """经既有 `start_service.sh` 启动推理服务（**不内联拼 vllm 命令**）

    该脚本承载了真机打磨过的经验，内联重写会丢掉：
    `--tensor-parallel-size`、`--max-model-len`、thinking 模型的 `--reasoning-parser`、
    `VLLM_PLUGINS` 三级决策（显式 → 持久化 → 探测 fl，避免 ascend/fl 多 platform 冲突）、
    启动前清 Triton/FlagGems 缓存、`logs/service.pid` 与日志软链。
    参数从命令行显式传入（本脚本新增的覆盖项），**不需要回写容器内 context.yaml**。

    Args:
        env: 额外环境变量（如算子白名单 `VLLM_FL_FLAGOS_WHITELIST`），经 `docker exec -e`
             传入；`start_service.sh` 拉起服务时会被继承。
    """
    log = logger or logging.getLogger("workflow.domain.service_control")
    args = [
        f"bash {START_SERVICE}",
        f"--port {port}",
        f"--model-path '{model_path}'",
        f"--model-name '{model_name}'",
        f"--max-model-len {max_model_len}",
        f"--log-file {log_file}",
    ]
    if tp_size > 0:
        args.append(f"--tp-size {tp_size}")
    if thinking:
        args.append("--thinking true")
    if cuda_visible_devices:
        args.append(f"--cuda-visible-devices {cuda_visible_devices}")
    if vllm_plugins is not None:
        args.append(f"--vllm-plugins '{vllm_plugins}'")

    script = f"cd {TOOLS_DIR} && " + " ".join(args)
    # 算子白名单等环境变量经 docker exec -e 传入（start_service.sh 启动的服务会继承）
    res = executor.docker_exec(container, script, env=env or None, timeout=300)
    if not res.ok:
        log.error(f"start_service.sh 失败：exit={res.returncode} {res.stderr[:300]}")
        return False
    log.info(f"已经 start_service.sh 启动服务（port={port}, tp={tp_size}, thinking={thinking}）")
    return True


def parse_env_inline(env_inline: str) -> Dict[str, str]:
    """把 apply_op_config 产出的 `env_inline`（`K=V K=V`）解析成字典"""
    out: Dict[str, str] = {}
    for token in (env_inline or "").split():
        if "=" in token:
            key, _, value = token.partition("=")
            if key:
                out[key] = value
    return out


def wait_for_service_ready(
    runner,
    container: str,
    port: int,
    model_name: str,
    task_id: str = "startup_engine",
    log_path: str = SERVICE_LOG,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """等就绪（走长任务协议 + 既有 `wait_for_service.sh`）

    该脚本是**日志活动感知**的：`--timeout 180`（无新输出多久算卡住）+ `--max-timeout 5760`
    （绝对上限）。早前引擎用 `curl /health` 轮询 300s 一刀切 —— 大模型加载十几分钟会被误判失败，
    而"日志还在动"的慢启动本该继续等（慢 ≠ 死）。
    可能阻塞 1.6 小时，因此必须走 detached + state 轮询，不能前台阻塞。
    """
    log = logger or logging.getLogger("workflow.domain.service_control")
    cmd = (
        f"cd {TOOLS_DIR} && bash {WAIT_FOR_SERVICE} "
        f"--port {port} --model-name '{model_name}' "
        f"--timeout {SERVICE_READY_STALL_TIMEOUT} --max-timeout {SERVICE_READY_MAX_TIMEOUT} "
        f"--log-path {log_path} --mode default"
    )
    if runner is None:  # 未注入长任务执行器 → 前台（仅供不关心阻塞的场景）
        res = None
        _ = cmd
        log.warning("未提供 LongTaskRunner，无法等待服务就绪")
        return False
    result = runner.run(task_id, cmd, timeout=SERVICE_READY_MAX_TIMEOUT)
    if not result.ok:
        log.error(f"服务未就绪：{result.summary()}")
        return False
    log.info(f"服务就绪（{task_id}）")
    return True


def stop_service(
    executor: CommandExecutor,
    container: str,
    port: int = DEFAULT_SERVICE_PORT,
    poll_interval: float = 0.0,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """彻底停掉容器内的推理服务并等端口释放

    Args:
        executor: 命令执行后端
        container: 容器名
        port: 服务端口（等待它变成无人监听）
        poll_interval: 端口探测间隔（测试传 0）
        logger: 日志器

    Returns:
        是否已确认端口释放（restart 失败且 pkill 后仍占用时为 False，但不阻断调用方）
    """
    log = logger or logging.getLogger("workflow.domain.service_control")

    # 1. restart 容器：比 pkill 更彻底（清掉 multiprocessing worker 残留）
    res = executor.run(["docker", "restart", container], timeout=_RESTART_TIMEOUT)
    if res.ok:
        log.info(f"已 restart 容器 {container} 以清理旧服务")
    else:
        log.warning(
            f"docker restart 失败（exit={res.returncode}: {res.stderr[:200]}），"
            f"降级 pkill -9 -f '{SERVICE_PROCESS_PATTERNS}'"
        )
        executor.docker_exec(
            container, f"pkill -9 -f '{SERVICE_PROCESS_PATTERNS}' 2>/dev/null; true",
            timeout=60,
        )

    # 2. 等端口空出来：curl 的 http_code 为 000/空 表示无人监听
    for _ in range(_PORT_FREE_RETRIES):
        probe = executor.docker_exec(
            container,
            f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{port}/health || true",
            timeout=10,
        )
        if (probe.stdout or "").strip() in ("", "000"):
            log.info(f"端口 {port} 已释放")
            return True
        if poll_interval > 0:
            time.sleep(poll_interval)

    log.error(f"端口 {port} 在等待后仍被占用——新服务可能起不来，且健康检查会问到旧服务")
    return False
