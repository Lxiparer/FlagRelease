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

"""Issue 出口 —— 确定性故障事实 → 本地 issue 记录

**为什么需要**：引擎接管后，启动崩溃/精度异常/性能异常这些**本该留痕**的事件
在引擎模式下没有任何出口（legacy 至少还能靠会话写）。plan §9 与工作段10 要求
issue 由**确定性故障事实**生成，并同时落两处：

1. `logs/issues_<category>.log`（追加，人读，格式见 CLAUDE.md）；
2. `results/issue_*.md`（经既有 `issue_reporter.py` 生成；约束4：issue 只能经它生成，
   禁止手工拼 `gh issue create`，且只保存本地 markdown）。

四类分流（plan §9）：
- `startup`     discovery / startup tuning 异常
- `accuracy`    V3/V4 精度异常
- `performance` V4 搜索异常 / benchmark 工具错误
- `analysis`    Agent schema/policy 错误、runtime 不可用、unresolved（M3 预留）

注意：V3 性能数值偏低**不是** issue（性能不设 Gate、不阻断）。
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from ..engine.command_executor import CommandExecutor

TOOLS_DIR = "/flagos-workspace/scripts"
ISSUE_REPORTER = f"{TOOLS_DIR}/issue_reporter.py"

# 分类 → (issues 日志文件, issue_reporter --type, 目标仓库)
CATEGORY_SPEC = {
    "startup": ("issues_startup.log", "operator-crash", ""),
    "accuracy": ("issues_accuracy.log", "accuracy-degraded", ""),
    "performance": ("issues_performance.log", "performance-degraded", ""),
    # plugin 阶段的问题一律进 vllm-plugin-FL 仓库（非 FlagGems）
    "analysis": ("issues_analysis.log", "plugin-error", "flagos-ai/vllm-plugin-FL"),
}


def write_issue(
    workspace_root: str,
    category: str,
    summary: str,
    detail: str = "",
    action: str = "",
    result: str = "",
    version: str = "V3",
    executor: Optional[CommandExecutor] = None,
    container: str = "",
    model_name: str = "",
    extra_tool_args: Optional[Dict[str, str]] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict:
    """记录一条 issue（日志文件必写；markdown 经既有 issue_reporter 尽力生成）

    Args:
        category: startup / accuracy / performance / analysis
        summary: 一句话问题摘要
        detail/action/result: 与 CLAUDE.md 的 issues 日志格式对应
        executor/container: 提供时才会调容器内 issue_reporter.py 生成 markdown
        extra_tool_args: 追加给 issue_reporter 的参数（如 --disabled-ops）

    Returns:
        {"log_file", "markdown", "reporter_ok"}
    """
    log = logger or logging.getLogger("workflow.domain.issue")
    if category not in CATEGORY_SPEC:
        raise ValueError(f"unknown issue category: {category}")
    log_file_name, issue_type, repo = CATEGORY_SPEC[category]

    # 1) 追加 issues_<category>.log（格式对齐 CLAUDE.md）
    log_dir = Path(workspace_root) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / log_file_name
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"[{ts}] {version} | {summary}\n"
        f"  详情: {detail or '—'}\n"
        f"  操作: {action or '—'}\n"
        f"  结果: {result or '—'}\n"
    )
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(entry)
        log.info(f"issue 已记录：{log_file}")
    except OSError as e:
        log.warning(f"写 issue 日志失败：{e}")

    # 2) 经既有 issue_reporter.py 生成 markdown（约束4：issue 只能经它生成）
    out = {"log_file": str(log_file), "markdown": "", "reporter_ok": False}
    if executor is None or not container:
        return out

    args = [f"python3 {ISSUE_REPORTER} full", f"--type {issue_type}"]
    if repo:
        args.append(f"--repo {repo}")
    if model_name:
        args.append(f"--model-name '{model_name}'")
    for k, v in (extra_tool_args or {}).items():
        args.append(f"{k} '{v}'")
    res = executor.docker_exec(container, f"cd {TOOLS_DIR} && " + " ".join(args), timeout=600)
    if res.ok:
        out["reporter_ok"] = True
        out["markdown"] = (res.stdout or "").strip()[-500:]
        log.info(f"issue markdown 已生成（{issue_type}）")
    else:
        log.warning(f"issue_reporter 失败（非阻断）：exit={res.returncode} {res.stderr[:200]}")
    return out
