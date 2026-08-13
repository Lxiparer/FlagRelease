#!/usr/bin/env python3
"""FlagOS 冒烟测例工具 — 首都问答判定

问服务"中国首都在哪"，检查回答是否含预期关键词（北京/Beijing）。
day0 流程（快速适配验证）的硬闸门：测例通过且服务不崩溃才进入精度评测。
同时也是 baseline_selector.py（V1 三选状态机）的冒烟复用模块。

用法:
    python3 smoke_test.py --port 8000 --model-name Qwen3-8B [--json]
    python3 smoke_test.py --port 8000 --model-name Qwen3-8B --prompt "中国首都在哪"
    python3 smoke_test.py --port 8000 --model-name Qwen3-8B --log-dir /flagos-workspace/logs

退出码: 0=通过, 1=未通过/请求失败, 2=参数错误

作为模块被 baseline_selector.py import 时（from smoke_test import smoke_test）：
默认 SMOKE_PROMPT 与历史行为一致（"中国的首都是哪里？"），行为不变。
day0 场景用 --prompt "中国首都在哪" 显式传需求措辞，两者语义等价。
"""

import argparse
import json
import os
import sys
import urllib.request
from typing import Tuple

# 默认冒烟题目与历史 baseline_selector 行为一致；day0 用 --prompt 覆盖为需求措辞
DEFAULT_SMOKE_PROMPT = "中国的首都是哪里？"
# 冒烟判定：回答中包含以下任一关键词即视为模型语义正常
SMOKE_KEYWORDS = ["北京", "Beijing", "beijing"]

# 默认日志目录（服务端口回写文件 logs/service_port 所在处）
DEFAULT_LOG_DIR = "/flagos-workspace/logs"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ARGS = 2


def resolve_service_port(default_port: int, log_dir: str = DEFAULT_LOG_DIR) -> int:
    """读取服务实际监听端口。

    start_service.sh 的端口来自 context.yaml 且**会因端口占用自动递增**，最终端口
    回写到 logs/service_port。冒烟必须用这个实际端口，不能假设 --port（默认
    8000），否则可能连不上（误判失败）或连到占用同端口的其他服务（误判成功/答非所问）。
    读不到文件时回退到传入的 default_port，保证不比原逻辑差。
    """
    port_file = os.path.join(log_dir, "service_port")
    try:
        with open(port_file, "r", encoding="utf-8") as f:
            actual = int(f.read().strip())
        if actual != default_port:
            print(f"  [port] 服务实际端口 {actual}（≠ 请求端口 {default_port}），冒烟改用实际端口")
        return actual
    except (OSError, ValueError):
        return default_port


def resolve_served_model_id(port: int, model_name: str) -> str:
    """动态解析 vLLM 实际注册的模型 id。

    vLLM 以 served_model_name 注册模型（start_service.sh 用 name.split('/')[-1]
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


def smoke_test(port: int, model_name: str,
               prompt: str = DEFAULT_SMOKE_PROMPT,
               keywords=None) -> Tuple[bool, str]:
    """冒烟测例：问"中国的首都"，检查回答含关键词。返回 (passed, answer)。"""
    keywords = keywords if keywords is not None else SMOKE_KEYWORDS
    url = f"http://localhost:{port}/v1/chat/completions"
    served_name = resolve_served_model_id(port, model_name)
    payload = {
        "model": served_name,
        "messages": [{"role": "user", "content": prompt}],
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
        passed = any(kw in answer for kw in keywords)
        return passed, answer.strip()[:200]
    except Exception as e:
        return False, f"请求失败: {e}"


def main():
    parser = argparse.ArgumentParser(description="FlagOS 冒烟测例（首都问答判定）")
    parser.add_argument("--port", type=int, default=8000,
                        help="请求端口（实际端口自动读 logs/service_port 回写值）")
    parser.add_argument("--model-name", default="", help="模型名（自动解析 served id）")
    parser.add_argument("--prompt", default=DEFAULT_SMOKE_PROMPT,
                        help="冒烟问题（day0 场景建议传 '中国首都在哪'）")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR,
                        help="日志目录（读 logs/service_port 端口回写）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    if not args.model_name:
        print("ERROR: --model-name 不能为空", file=sys.stderr)
        sys.exit(EXIT_ARGS)

    actual_port = resolve_service_port(args.port, args.log_dir)
    passed, answer = smoke_test(actual_port, args.model_name, args.prompt)

    result = {
        "passed": passed,
        "answer": answer,
        "prompt": args.prompt,
        "port": actual_port,
        "model": resolve_served_model_id(actual_port, args.model_name),
    }

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"冒烟测例：{'✓ 通过' if passed else '✗ 未通过'}")
        print(f"  端口: {actual_port}")
        print(f"  问题: {args.prompt}")
        print(f"  回答: {answer}")

    # 供编排层解析的机器可读标记
    print(f"[SMOKE_RESULT]{json.dumps(result, ensure_ascii=False)}[/SMOKE_RESULT]")

    sys.exit(EXIT_PASS if passed else EXIT_FAIL)


if __name__ == "__main__":
    main()
