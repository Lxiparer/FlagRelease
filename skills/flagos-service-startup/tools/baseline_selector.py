#!/usr/bin/env python3
"""
baseline_selector.py — V1 基线选择 — 分支 B (gems+tree+plugin) 专用（sglang 分支二态）

sglang 分支：无 vllm 式"厂商 platform plugin"概念（sglang_fl 是算子调度插件、
不注册 platform），V1 基线收敛为二态：

  v1.1  原生 sglang 启动 = 不加载 sglang_fl 插件（SGLANG_PLUGINS=''）
        + 不开 flaggems（USE_FLAGGEMS=0，SGLANG_FL_PREFER=reference）
  none  依赖 flaggems 无法起服务（sglang_fl 与 sglang 深度耦合），
        精度基线回退 NV（nv_baseline.yaml），性能基线由 V2 初始性能合成

vllm 分支的三选（纯净 / 厂商 plugin / fl plugin 不开 flaggems）在 sglang 场景
收敛为二选：插件要么加载（flagos 模式）要么不加载（基线），无"厂商插件半加载"
中间态。SGLANG_PLUGINS='' 过滤 entry_points 自动加载（sglang.srt.plugins），
USE_FLAGGEMS=0 为 Layer 1 总开关双保险。

Usage:
    python3 baseline_selector.py \
        --service-startup-cmd "bash /flagos-workspace/scripts/start_service.sh" \
        --wait-script /flagos-workspace/scripts/wait_for_service.sh \
        --port 30000 --model-name "Qwen3.6-35B-A3B" \
        --max-timeout 1800 --json
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

# 默认服务启动/等待脚本（容器内部署位置）
DEFAULT_START_SCRIPT = "/flagos-workspace/scripts/start_service.sh"
DEFAULT_WAIT_SCRIPT = "/flagos-workspace/scripts/wait_for_service.sh"
DEFAULT_LOG_DIR = "/flagos-workspace/logs"

# 冒烟测例：问"中国的首都"，检查回答含关键词
SMOKE_PROMPT = "中国的首都是哪个城市？请简要回答。"
SMOKE_KEYWORDS = ["北京", "Beijing", "beijing"]

# 退出码
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_V1 = 2

# v1.1 纯净基线 env（不加载插件 + 不开 flaggems）
BASELINE_ENV = {
    "SGLANG_PLUGINS": "",
    "USE_FLAGGEMS": "0",
    "SGLANG_FL_PREFER": "reference",
}


def run_cmd(cmd: str, timeout: int = 300) -> tuple:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"超时 ({timeout}s)"
    except Exception as e:
        return -1, "", str(e)


def stop_service():
    """停止残留服务，释放 GPU"""
    subprocess.run(
        "pkill -9 -f 'sglang.launch_server|sglang serve|python3 -m sglang|multiproc_worker' 2>/dev/null",
        shell=True, capture_output=True)
    time.sleep(5)


def clear_caches():
    for d in ["/root/.triton/cache/", "/tmp/triton_cache/", "/root/.flaggems/code_cache/"]:
        if os.path.exists(d):
            subprocess.run(f"rm -rf {d}", shell=True, capture_output=True)


def env_to_inline(env: Dict[str, str]) -> str:
    """env dict → 内联前缀字符串（含空串值显式置空）"""
    parts = []
    for k, v in env.items():
        parts.append(f"{k}={v}")
    return " ".join(parts)


def _persist_state(result: Dict[str, Any]) -> Dict[str, Any]:
    """选定后确定性落盘（不靠编排层转记）：

    1. v1.1 场景持久化 SGLANG_PLUGINS='' + USE_FLAGGEMS=0 + SGLANG_FL_PREFER=reference
       → start_service.sh 后续启动（V2 等）未显式传参时继承，V2 强制走 plugin 调度路径
    2. none 场景持久化 USE_FLAGGEMS=1 + SGLANG_FL_PREFER=flagos
       → V2 强制 flagos 路径，与 V3 同镜像（2.2 双 tag 前提），避免 V2/V3 拆成
         两次独立评测产生噪声跨比误判
    3. baseline.* 写入 context.yaml
    """
    persisted: Dict[str, Any] = {"env_persisted": False, "context": False}

    try:
        from flagos_op_config import persist_env, clear_env
    except ImportError:
        # 宿主机/未部署共享模块时的最小实现（容器内 scripts/ 平铺目录可直接 import）
        ETC = "/etc/environment"

        def persist_env(key, value):
            lines = []
            if os.path.exists(ETC):
                with open(ETC) as f:
                    lines = [l for l in f.readlines() if not l.startswith(f"{key}=")]
            lines.append(f"{key}={value}\n")
            with open(ETC, "w") as f:
                f.writelines(lines)
            os.environ[key] = value

        def clear_env(key):
            if os.path.exists(ETC):
                with open(ETC) as f:
                    lines = [l for l in f.readlines() if not l.startswith(f"{key}=")]
                with open(ETC, "w") as f:
                    f.writelines(lines)
            os.environ.pop(key, None)

    try:
        if result["v1_variant"] == "none":
            # 依赖 flaggems 无法起原生服务：持久化 flagos 全开路径（V2 与 V3
            # 走同一插件调度路径，2.2 同镜像双 tag 前提，消除准入镜像默认带没带
            # 该变量的不确定性）
            persist_env("USE_FLAGGEMS", "1")
            persist_env("SGLANG_FL_PREFER", "flagos")
            clear_env("SGLANG_PLUGINS")
            persisted["env_persisted"] = True
            print("  ✓ V1=none：持久化 USE_FLAGGEMS=1 + SGLANG_FL_PREFER=flagos（V2 强制 flagos 路径，与 V3 同镜像）")
        else:
            # v1.1 纯净基线：持久化 SGLANG_PLUGINS='' + 关闭 flaggems
            persist_env("SGLANG_PLUGINS", "")
            persist_env("USE_FLAGGEMS", "0")
            persist_env("SGLANG_FL_PREFER", "reference")
            persisted["env_persisted"] = True
            print("  ✓ 持久化 SGLANG_PLUGINS='' + USE_FLAGGEMS=0 + SGLANG_FL_PREFER=reference（V2 起强制 flagos 覆盖）")
    except Exception as e:
        print(f"  WARN: 基线状态持久化失败: {e}")

    # context.yaml 落盘（update_context.py 与本脚本在容器内同目录）
    update_ctx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_context.py")
    if os.path.isfile(update_ctx):
        rc, _, err = run_cmd(
            f"{sys.executable} {update_ctx}"
            f" --set 'baseline.v1_variant={result['v1_variant']}'"
            f" --set 'baseline.sglang_plugins={result.get('sglang_plugins', '')}'"
            f" --set 'baseline.v1_available={str(result['v1_available']).lower()}'",
            timeout=60,
        )
        persisted["context"] = rc == 0
        if rc != 0:
            print(f"  WARN: context 写入失败: {err.strip()[:200]}")
    else:
        print(f"  WARN: 未找到 update_context.py（{update_ctx}），跳过 context 写入")

    return persisted


def start_variant(service_cmd: str, env: Dict[str, str],
                  wait_script: str, port: int, model_name: str,
                  log_path: str, max_timeout: int) -> bool:
    """按指定 env（SGLANG_PLUGINS / USE_FLAGGEMS / SGLANG_FL_PREFER）启动服务并等待就绪。"""
    stop_service()
    clear_caches()

    # 清除上一个 variant 的端口回写文件，避免本次启动尚未写入时读到残留端口，
    # 导致 wait/冒烟连到旧端口。start_service.sh 启动后会重新写入实际端口。
    try:
        os.remove(os.path.join(DEFAULT_LOG_DIR, "service_port"))
    except OSError:
        pass

    # 组装启动命令：显式传 --sglang-plugins（含空串），USE_FLAGGEMS 通过 mode 控制
    mode = "native" if env.get("USE_FLAGGEMS") == "0" else "flagos"
    sglang_plugins = env.get("SGLANG_PLUGINS", "")
    # --log-file 让 start_service.sh 把服务日志写入本 variant 的独立文件，
    # 与下方 wait_for_service --log-path 监控同一文件（否则 start_service.sh 默认
    # 写 startup_${mode}.log，三个 variant 互相覆盖且监控端抓不到真实日志 → 恒判失败）。
    # 用 shell 引号安全传递空串
    cmd = (f"{service_cmd} --mode {mode} --sglang-plugins '{sglang_plugins}'"
           f" --log-file '{log_path}'")
    env_prefix = env_to_inline(env) + " "

    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    # 服务日志由 start_service.sh 经 --log-file 写入 log_path；此处 start_service.sh
    # 前台部分的少量 echo 无需保留，丢弃以免与其内部 nohup 重定向混淆。
    subprocess.Popen(
        env_prefix + cmd, shell=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
    )

    # start_service.sh 在后台启动、走完端口探测后才写 service_port（端口可能被自动
    # 递增）。短轮询等待该文件出现，拿到服务实际端口后同时用于 wait 与冒烟，确保三者
    # 端口严格一致，不会连到占用原端口的其他服务。文件迟迟不出现则回退请求端口。
    actual_port = port
    for _ in range(30):  # 最多等 ~15s
        resolved = resolve_service_port(port)
        if os.path.exists(os.path.join(DEFAULT_LOG_DIR, "service_port")):
            actual_port = resolved
            break
        time.sleep(0.5)

    # --from-start：本 variant 是全新启动，start_service.sh 的 nohup 以 truncate 方式
    # 重写 log_path，wait 必须从 offset 0 读起，才能捕获本次启动的进度信号
    # （loading_weights/service_ready 等）。否则沿用文件残留大小作 offset → 读不到
    # 进度信号 → 端口虽已响应仍被误判为"残留服务"(stale_service) → start_variant 误返回失败。
    wait_cmd = (
        f"{wait_script} --port {actual_port} --timeout 300 --max-timeout {max_timeout}"
        f" --log-path {log_path} --mode {mode} --from-start"
    )
    if model_name:
        wait_cmd += f" --model-name '{model_name}'"
    rc, _, _ = run_cmd(wait_cmd, timeout=max_timeout + 60)
    return rc == 0


def resolve_service_port(default_port: int) -> int:
    """读取服务实际监听端口。

    start_service.sh 的端口来自 context.yaml 且**会因端口占用自动递增**，最终端口
    回写到 logs/service_port。冒烟/查找必须用这个实际端口，不能假设 --port（默认
    8000），否则可能连不上（误判失败）或连到占用同端口的其他服务（误判成功/答非所问）。
    读不到文件时回退到传入的 default_port，保证不比原逻辑差。
    """
    port_file = os.path.join(DEFAULT_LOG_DIR, "service_port")
    try:
        with open(port_file, "r", encoding="utf-8") as f:
            actual = int(f.read().strip())
        if actual != default_port:
            print(f"  [port] 服务实际端口 {actual}（≠ 请求端口 {default_port}），冒烟改用实际端口")
        return actual
    except (OSError, ValueError):
        return default_port


def resolve_served_model_id(port: int, model_name: str) -> str:
    """动态解析 sglang 实际注册的模型 id。

    sglang serve 以 --served-model-name 注册模型（start_service.sh 用 name.split('/')[-1]
    去掉了 org 前缀，如 upstage/）。冒烟请求若用带前缀的全名会命中不存在的 model
    触发 404，被误判为冒烟/启动失败。这里先查 /v1/models 取服务实际注册的 id：
      1. 若返回的 id 列表里能匹配到传入名（全名或去前缀短名），用匹配到的那个；
      2. 单模型服务则直接用列表里唯一的 id；
      3. 查询失败时回退到静态去前缀名，保证不比原逻辑更差。
    """
    fallback = (model_name or "default").split("/")[-1]
    try:
        req = urllib.request.Request(
            f"http://localhost:{port}/v1/models", method="GET"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        served_ids = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    except Exception:
        return fallback

    if not served_ids:
        return fallback
    # 传入名（全名或去前缀）能精确命中已注册 id 则优先用之
    for candidate in (model_name, fallback):
        if candidate and candidate in served_ids:
            return candidate
    # 单模型服务：直接用唯一注册 id
    if len(served_ids) == 1:
        return served_ids[0]
    return fallback


def smoke_test(port: int, model_name: str) -> tuple:
    """冒烟测例：问"中国的首都"，检查回答含关键词。返回 (passed, answer)。"""
    url = f"http://localhost:{port}/v1/chat/completions"
    served_name = resolve_served_model_id(port, model_name)
    payload = {
        "model": served_name,
        "messages": [{"role": "user", "content": SMOKE_PROMPT}],
        "max_tokens": 64,
        "temperature": 0.0,
    }
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        answer = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        passed = any(kw in answer for kw in SMOKE_KEYWORDS)
        return passed, answer.strip()[:200]
    except Exception as e:
        return False, f"请求失败: {e}"


def try_variant(variant: str, env: Dict[str, str],
                service_cmd: str, wait_script: str, port: int, model_name: str,
                max_timeout: int) -> Dict[str, Any]:
    """尝试一个 V1 变体：启动 + 冒烟。返回 attempt 记录。"""
    log_path = os.path.join(DEFAULT_LOG_DIR, f"startup_{variant}.log")
    print(f"\n{'=' * 56}")
    print(f"  尝试 {variant}: {env_to_inline(env)}")
    print(f"{'=' * 56}")

    attempt = {
        "variant": variant,
        "sglang_plugins": env.get("SGLANG_PLUGINS", ""),
        "use_flaggems": env.get("USE_FLAGGEMS", "0"),
        "service_ok": False,
        "smoke_passed": False,
        "smoke_answer": "",
        "reason": "",
    }

    service_ok = start_variant(service_cmd, env,
                               wait_script, port, model_name, log_path, max_timeout)
    attempt["service_ok"] = service_ok
    if not service_ok:
        attempt["reason"] = "服务启动失败"
        print(f"  ✗ 服务启动失败")
        return attempt

    print(f"  ✓ 服务已就绪，运行冒烟测例...")
    # 服务已就绪，service_port 必已写入 → 用实际监听端口冒烟，与启动/wait 端口一致
    smoke_port = resolve_service_port(port)
    passed, answer = smoke_test(smoke_port, model_name)
    attempt["smoke_passed"] = passed
    attempt["smoke_answer"] = answer
    if passed:
        attempt["reason"] = "冒烟通过"
        print(f"  ✓ 冒烟通过：{answer[:80]}")
    else:
        attempt["reason"] = "冒烟未通过（回答不含预期关键词）"
        print(f"  ✗ 冒烟未通过：{answer[:80]}")
    return attempt


def select_v1(service_cmd: str, wait_script: str,
              port: int, model_name: str, max_timeout: int) -> Dict[str, Any]:
    """按 v1.1 → none 依次尝试，返回选择结果（sglang 二态）。"""
    # 候选列表：仅 v1.1（纯净基线，不加载插件 + 不开 flaggems）
    candidates = [
        ("v1.1", dict(BASELINE_ENV)),
    ]

    attempts: List[Dict[str, Any]] = []
    selected: Optional[Dict[str, Any]] = None

    for variant, env in candidates:
        attempt = try_variant(variant, env, service_cmd,
                              wait_script, port, model_name, max_timeout)
        attempts.append(attempt)
        if attempt["smoke_passed"]:
            selected = attempt
            break

    stop_service()

    if selected:
        result = {
            "v1_variant": selected["variant"],
            "sglang_plugins": selected["sglang_plugins"],
            "use_flaggems": selected["use_flaggems"],
            "v1_available": True,
            "smoke_passed": True,
            "nv_baseline_used": False,
            "attempts": attempts,
            "message": f"选定 V1 变体: {selected['variant']} (SGLANG_PLUGINS='{selected['sglang_plugins']}', USE_FLAGGEMS={selected['use_flaggems']})",
        }
    else:
        # v1.1 失败 → 无独立 V1，强依赖 flaggems，精度基线回退 NV
        result = {
            "v1_variant": "none",
            "sglang_plugins": "",
            "use_flaggems": "1",
            "v1_available": False,
            "smoke_passed": False,
            "nv_baseline_used": True,
            "attempts": attempts,
            "message": "v1.1 失败 → 无独立 V1（强依赖 flaggems），精度基线回退 NV",
        }

    return result


def main():
    parser = argparse.ArgumentParser(description="V1 基线选择（分支 B，sglang 二态）")
    parser.add_argument("--service-startup-cmd", required=True,
                        help="服务启动命令（不含 --mode/--sglang-plugins，本脚本自动追加）")
    parser.add_argument("--wait-script", default=DEFAULT_WAIT_SCRIPT)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model-name", default="")
    parser.add_argument("--max-timeout", type=int, default=1800)
    parser.add_argument("--output", help="结果 JSON 输出路径")
    parser.add_argument("--no-persist", action="store_true",
                        help="跳过 env 持久化与 context 写入（调试用）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = select_v1(
        service_cmd=args.service_startup_cmd,
        wait_script=args.wait_script,
        port=args.port,
        model_name=args.model_name,
        max_timeout=args.max_timeout,
    )

    # 选定后确定性落盘：env 持久化 + context 写入
    if not args.no_persist:
        result["persisted"] = _persist_state(result)

    if args.output:
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"\n{'#' * 56}")
        print(f"# {result['message']}")
        print(f"{'#' * 56}")

    # 供编排层解析的机器可读标记
    print(f"[V1_SELECTION]{json.dumps(result, ensure_ascii=False)}[/V1_SELECTION]")

    sys.exit(EXIT_OK if result["v1_available"] else EXIT_NO_V1)


if __name__ == "__main__":
    main()
