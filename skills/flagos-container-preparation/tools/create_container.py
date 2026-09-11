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

"""确定性创建容器（把"交给 agent 执行的 docker run 模板"变成可复用工具）

**为什么需要**：容器创建原先只写在 prompt 里、由 agent 读 SKILL.md 后手工执行。
引擎模式要求"不依赖 agent"，就必须有一条确定性的建容器路径；否则引擎只能去
**复用上一次的容器**——而 SKILL.md 明令「镜像模式下禁止复用任何已存在的容器
（复用旧容器=旧镜像跑新任务，产出错误归属，比失败更糟）」。

设计：厂商知识做成**数据**（`container_templates.yaml`，逐字转写 SKILL.md 模板 A–G），
本工具只负责：探测厂商 → 取模板 → 展开变量 → 执行 → 校验容器真的起来了。
加一个厂商 = 加一段数据，不改代码。

用法:
    python3 create_container.py --image <镜像> --model-name Qwen/Qwen3-8B \\
        --container-name Qwen3-8B_flagos --model-path /data/models/Qwen3-8B \\
        [--workspace /data/flagos-workspace/Qwen/Qwen3-8B] [--vendor nvidia] [--json]

退出码: 0=创建成功 · 2=参数/模板缺失（fail-closed，**不回退复用**）· 3=创建后校验失败
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent
TEMPLATES_FILE = HERE / "container_templates.yaml"
DETECT_GPU = REPO_ROOT / "shared" / "detect_gpu.py"

DEFAULT_WORKSPACE_ROOT = "/data/flagos-workspace"


def load_templates() -> Dict:
    with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("vendors") or {}


def detect_vendor(explicit: str = "") -> Tuple[str, str]:
    """探测 GPU 厂商（复用既有 detect_gpu.py，不自己写 nvidia-smi 逻辑）

    Returns:
        (vendor, 说明)  —— vendor 为空串表示探测失败
    """
    if explicit:
        return explicit, "显式指定"
    if not DETECT_GPU.exists():
        return "", f"找不到 {DETECT_GPU}"
    try:
        out = subprocess.run(
            [sys.executable, str(DETECT_GPU), "--json"],
            capture_output=True, text=True, timeout=120,
        )
        info = json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        return "", f"detect_gpu.py 执行/解析失败：{e}"
    vendor = str(info.get("vendor") or "").strip().lower()
    return vendor, (f"detect_gpu.py 探测（{info.get('name', '')}）" if vendor else "探测无结果")


def build_argv(vendor: str, tpl: Dict, values: Dict[str, str]) -> List[str]:
    """把模板 argv 展开成真正的 docker run argv"""
    rendered = []
    for token in tpl.get("argv") or []:
        rendered.append(token.format(**values))
    return ["docker", "run"] + rendered


def container_exists(name: str) -> bool:
    res = subprocess.run(
        ["docker", "inspect", "--type=container", name],
        capture_output=True, text=True,
    )
    return res.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="确定性创建容器（厂商模板数据化）")
    ap.add_argument("--image", required=True, help="镜像地址")
    ap.add_argument("--model-name", required=True, help="模型名（含 vendor，如 Qwen/Qwen3-8B）")
    ap.add_argument("--container-name", required=True, help="容器名（编排层预生成，禁止自行改动）")
    ap.add_argument("--model-path", required=True,
                    help="宿主机模型路径（未找到权重时用预建的空目录，容器内再下载）")
    ap.add_argument("--workspace", default="",
                    help="宿主机工作目录（缺省 /data/flagos-workspace/<model>）")
    ap.add_argument("--vendor", default="", help="显式指定厂商（跳过探测）")
    ap.add_argument("--shm", default="", help="共享内存大小（缺省用模板值）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")
    args = ap.parse_args()

    templates = load_templates()
    vendor, how = detect_vendor(args.vendor)
    if not vendor:
        msg = f"GPU 厂商探测失败（{how}）——fail-closed，不猜测模板、也不复用旧容器"
        print(json.dumps({"success": False, "error": msg}, ensure_ascii=False)
              if args.json else f"✗ {msg}", file=sys.stderr)
        return 2

    tpl = templates.get(vendor)
    if not tpl:
        msg = (f"厂商 {vendor} 没有对应模板（已支持：{', '.join(sorted(templates))}）"
               f"——fail-closed，请手工准备容器后显式传 --container")
        print(json.dumps({"success": False, "error": msg}, ensure_ascii=False)
              if args.json else f"✗ {msg}", file=sys.stderr)
        return 2

    # 镜像模式下**禁止复用已存在容器**：同名冲突必须在编排层解决（本工具拒绝覆盖）
    if container_exists(args.container_name):
        msg = (f"容器 {args.container_name} 已存在——镜像模式禁止复用已存在容器"
               f"（复用旧容器=旧镜像跑新任务，产出错误归属）；请由编排层改名后重试")
        print(json.dumps({"success": False, "error": msg}, ensure_ascii=False)
              if args.json else f"✗ {msg}", file=sys.stderr)
        return 2

    workspace = args.workspace or f"{DEFAULT_WORKSPACE_ROOT}/{args.model_name}"
    values = {
        "container": args.container_name,
        "model_path": args.model_path,
        "container_model_path": args.model_path,  # 宿主机路径原样映射
        "workspace": workspace,
        "image": args.image,
        "shm": args.shm or tpl.get("shm") or "64g",
    }
    argv = build_argv(vendor, tpl, values)

    if args.dry_run:
        print(json.dumps({"success": True, "argv": argv, "vendor": vendor},
                         ensure_ascii=False, indent=2) if args.json else " ".join(argv))
        return 0

    proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    container_id = (proc.stdout or "").strip()[:12]
    if proc.returncode != 0 or not container_exists(args.container_name):
        msg = f"docker run 失败：exit={proc.returncode} {proc.stderr[:300]}"
        print(json.dumps({"success": False, "error": msg, "argv": argv}, ensure_ascii=False)
              if args.json else f"✗ {msg}", file=sys.stderr)
        return 3

    result = {
        "success": True, "container": args.container_name, "container_id": container_id,
        "vendor": vendor, "vendor_label": tpl.get("label", vendor),
        "detected_by": how, "workspace": workspace,
        "visible_devices_env": tpl.get("visible_devices_env", "CUDA_VISIBLE_DEVICES"),
        "argv": argv,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"✓ 已创建容器 {args.container_name}（{tpl.get('label', vendor)}，{container_id}）")
        print(f"  工作目录: {workspace}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
