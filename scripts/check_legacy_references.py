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

"""旧流程引用静态审计（plan 工作段 12 第 1 步 / 正式切换条件第 9 条）

**只审计不删除**。列出全仓仍引用旧双 pipeline / V1 / V2 语义的位置，作为后续物理删除的
待办基线。plan 工作段 12 的验收要求「全仓新运行路径不得引用」下列符号：

    baseline_selector / workflow.performance_ok / plugin_workflow /
    synthesize_perf_baseline / pipeline_branch: A / V1.1|V1.2|V1.3 / native_performance.json

分类规则：
- `code`（.py/.sh）：**新运行路径**，命中即视为待清理（退出码 1）
- `data`（.yaml/.yml/.json）：配置/数据，命中需人工判断（不影响退出码）
- `docs`（.md/.html）：历史文档，plan 明确「历史文档中的明确历史引用可豁免」（不影响退出码）

用法:
    python3 scripts/check_legacy_references.py            # 人读报告
    python3 scripts/check_legacy_references.py --json     # 机读（供 CI）
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent

# plan 工作段 12 验收清单（正则；注意 escape 掉 `.`）
FORBIDDEN = [
    ("baseline_selector", r"baseline_selector"),
    ("workflow.performance_ok", r"workflow\.performance_ok"),
    ("plugin_workflow", r"plugin_workflow"),
    ("synthesize_perf_baseline", r"synthesize_perf_baseline"),
    ("pipeline_branch A", r"pipeline_branch\s*[:=]\s*['\"]?A\b"),
    ("V1.1/V1.2/V1.3", r"V1\.[123]\b"),
    ("native_performance.json", r"native_performance\.json"),
]

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "archive", "node_modules", ".idea"}

CODE_SUFFIXES = {".py", ".sh"}
DATA_SUFFIXES = {".yaml", ".yml", ".json"}
DOC_SUFFIXES = {".md", ".html", ".txt"}

SELF = Path(__file__).resolve()


def classify(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in CODE_SUFFIXES:
        return "code"
    if suffix in DATA_SUFFIXES:
        return "data"
    if suffix in DOC_SUFFIXES:
        return "docs"
    return "other"


def scan(patterns) -> List[Dict]:
    hits: List[Dict] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.resolve() == SELF:
            continue  # 本脚本自身就是清单的载体，不参与自检
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in (CODE_SUFFIXES | DATA_SUFFIXES | DOC_SUFFIXES):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, regex in patterns:
                if re.search(regex, line):
                    hits.append({
                        "symbol": name,
                        "category": classify(path),
                        "file": str(path.relative_to(REPO_ROOT)),
                        "line": lineno,
                        "text": line.strip()[:160],
                    })
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="旧流程引用静态审计（只审计不删除）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--only-code", action="store_true", help="只报代码（.py/.sh）命中")
    args = parser.parse_args()

    hits = scan(FORBIDDEN)
    if args.only_code:
        hits = [h for h in hits if h["category"] == "code"]

    code_hits = [h for h in hits if h["category"] == "code"]
    data_hits = [h for h in hits if h["category"] == "data"]
    doc_hits = [h for h in hits if h["category"] == "docs"]

    if args.json:
        print(json.dumps({
            "repo_root": str(REPO_ROOT),
            "patterns": [name for name, _ in FORBIDDEN],
            "counts": {"code": len(code_hits), "data": len(data_hits), "docs": len(doc_hits)},
            "code_hits": code_hits,
            "data_hits": data_hits,
            "doc_hits": doc_hits,
            "verdict": "clean" if not code_hits else "legacy_references_found",
        }, ensure_ascii=False, indent=2))
        return 1 if code_hits else 0

    print("═" * 72)
    print("  旧流程引用静态审计（plan 工作段 12 第 1 步）——只审计不删除")
    print("═" * 72)
    print(f"  仓库根：{REPO_ROOT}")
    print(f"  待清符号：{'、'.join(name for name, _ in FORBIDDEN)}")
    print()

    def dump(title: str, items: List[Dict], note: str):
        print(f"── {title}（{len(items)} 处）{note}")
        if not items:
            print("   （无）")
        for h in items:
            print(f"   {h['file']}:{h['line']}  [{h['symbol']}]  {h['text'][:90]}")
        print()

    dump("代码（新运行路径，需清理）", code_hits, " ← 命中即退出码 1")
    dump("配置/数据（需人工判断）", data_hits, "（不影响退出码）")
    dump("文档（历史引用可豁免）", doc_hits, "（不影响退出码）")

    print("═" * 72)
    if code_hits:
        print(f"  结论：仍有 {len(code_hits)} 处代码引用旧语义 → 物理删除的前置条件未满足")
        print("  说明：plan 工作段 12 要求删除发生在「至少一次成功 V3 + 一次 V4 成功/正常")
        print("        fallback + report/batch/notification 已迁移」之后，本脚本只提供待办基线。")
        return 1
    print("  结论：代码侧无旧语义引用 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
