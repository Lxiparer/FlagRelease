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

"""Workflow Engine CLI —— 确定性 15 步流程入口（薄入口，plan 工作段 2）

引擎在**宿主侧**运行，经注入的 CommandExecutor（docker exec / subprocess）操作容器；
容器本身必须已存在（容器创建仍是宿主机编排职责，见 plan 已确认边界）。

用法:
    python3 -m workflow.cli.main --workspace /data/flagos-workspace/Qwen/Qwen3-8B \
        --container qwen3-8b_flagos --model Qwen/Qwen3-8B --model-path /models/Qwen3-8B \
        [--datasets gpqa_diamond] [--state-file PATH] [--v4-seed N] [--v4-max-rounds N]

退出码（旧 shell 几乎恒 0，这里补上阶段级语义）:
    0  15 步走完（success / skipped 均算走完）
    1  有步骤 failed（打印步骤 id 与原因）
    2  前置校验失败或参数错误（含步骤01 的前置校验）
    3  状态文件损坏／不可解析
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

# 允许直接 `python3 workflow/cli/main.py` 与 `python3 -m workflow.cli.main` 两种调用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from workflow.engine.workflow_engine import WorkflowEngine, WORKFLOW_STEPS  # noqa: E402
from workflow.schemas.context_v2 import ContextValidationError  # noqa: E402

EXIT_OK = 0
EXIT_STEP_FAILED = 1
EXIT_PRECONDITION = 2
EXIT_STATE_CORRUPT = 3

# 步骤01 是引擎侧的前置校验（容器/工作区就绪），其失败按"前置条件不满足"归类
_PRECONDITION_STEP = "01_container_preparation"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m workflow.cli.main",
        description="FlagOS Plugin-only 确定性工作流引擎（15 步：迁移→评测→发布）",
    )
    parser.add_argument(
        "--workspace", required=True,
        help="宿主机上的模型工作目录（挂载到容器 /flagos-workspace），"
             "例如 /data/flagos-workspace/Qwen/Qwen3-8B",
    )
    parser.add_argument("--container", required=True, help="目标容器名（须已存在）")
    parser.add_argument("--model", required=True, help="模型名（含 vendor，如 Qwen/Qwen3-8B）")
    parser.add_argument("--model-path", default="", help="容器内模型权重路径")
    parser.add_argument(
        "--datasets", default="gpqa_diamond",
        help="精度评测数据集（逗号分隔；每个独立判定，全部达标才成立）",
    )
    parser.add_argument(
        "--state-file", default="",
        help="引擎状态文件路径（缺省 <workspace>/config/engine/context.yaml；"
             "该目录每轮被宿主编排归档，保证每轮干净起步）",
    )
    parser.add_argument(
        "--artifacts-root", default="",
        help="Artifact 台账根目录（缺省 <workspace>/config/engine/artifacts）",
    )
    parser.add_argument("--v4-seed", type=int, default=0, help="V4 随机子集搜索种子（可复现）")
    parser.add_argument("--v4-max-rounds", type=int, default=2, help="V4 搜索轮数上限")
    parser.add_argument("--verbose", action="store_true", help="DEBUG 级日志")
    return parser


def _resolve_workspace(raw: str) -> Optional[Path]:
    """校验工作目录：必须是已存在的目录（容器准备阶段产出的目录结构）"""
    path = Path(raw).expanduser()
    if not path.is_dir():
        print(f"✗ 工作目录不存在或不是目录：{path}", file=sys.stderr)
        return None
    return path


def _print_summary(ctx) -> None:
    """打印步骤状态与 Gate 摘要（人读）"""
    print("\n" + "=" * 66)
    print("  步骤状态")
    print("=" * 66)
    for step_id, step_name in WORKFLOW_STEPS:
        step = ctx.steps.get(step_id)
        status = step.status if step else "missing"
        mark = {"success": "✓", "skipped": "–", "failed": "✗"}.get(status, "?")
        note = step.skip_reason or step.fail_reason if step else ""
        print(f"  {mark} {step_id:<26} {status:<8} {step_name}"
              + (f"  [{note[:60]}]" if note else ""))

    if ctx.gates:
        print("\n" + "=" * 66)
        print("  Gate")
        print("=" * 66)
        for gate_id, gate in sorted(ctx.gates.items()):
            print(f"  {gate_id:<28} {gate.status:<8} {gate.reason[:60]}")

    if ctx.operator_revisions:
        print("\n" + "=" * 66)
        print("  算子 revision")
        print("=" * 66)
        for rid, rev in sorted(ctx.operator_revisions.items()):
            print(f"  {rid:<26} ops={len(rev.enabled_ops):<4} "
                  f"disabled={len(rev.disabled_ops):<4} frozen={rev.frozen}")
    print()


def _first_failed_step(ctx) -> Optional[str]:
    """按流程顺序找第一个 failed 步骤"""
    for step_id, _ in WORKFLOW_STEPS:
        step = ctx.steps.get(step_id)
        if step and step.status == "failed":
            return step_id
    return None


def exit_code_for_context(ctx) -> int:
    """把引擎终态映射成进程退出码（纯函数，便于单测）

    注意：**Gate failed ≠ 非零退出码**。精度不达标按设计不阻断流程（走私有发布），
    只有"某步 failed"才算流程失败；达标与否看报告/摘要，不看退出码。
    """
    failed = _first_failed_step(ctx)
    if failed is None:
        return EXIT_OK
    return EXIT_PRECONDITION if failed == _PRECONDITION_STEP else EXIT_STEP_FAILED


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logger = logging.getLogger("workflow.cli")

    workspace = _resolve_workspace(args.workspace)
    if workspace is None:
        return EXIT_PRECONDITION

    datasets = [d.strip() for d in args.datasets.split(",") if d.strip()]
    if not datasets:
        print("✗ --datasets 不能为空", file=sys.stderr)
        return EXIT_PRECONDITION

    # 构造引擎（状态文件隔离：默认写 engine_context.yaml，不碰 legacy context.yaml）
    try:
        engine = WorkflowEngine(
            workspace_root=str(workspace),
            datasets=datasets,
            state_file=args.state_file or None,
            artifacts_root=args.artifacts_root or None,
        )
    except ContextValidationError as e:
        print(f"✗ 引擎状态文件校验失败：{e}", file=sys.stderr)
        return EXIT_STATE_CORRUPT
    except Exception as e:  # YAML 解析失败等
        print(f"✗ 引擎状态文件不可用（{engine_state_path(args, workspace)}）："
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_STATE_CORRUPT

    # 回填运行时信息（CLI 的入参是引擎的唯一运行时输入源）
    engine.context.runtime.container_name = args.container
    engine.context.runtime.model_name = args.model
    if args.model_path:
        engine.context.runtime.model_path = args.model_path
    engine.v4_seed = args.v4_seed
    engine.v4_max_rounds = args.v4_max_rounds

    logger.info(
        f"引擎启动：workspace={workspace} container={args.container} "
        f"model={args.model} datasets={datasets} state={engine.context_file}"
    )

    ctx = engine.run()
    _print_summary(ctx)

    rc = exit_code_for_context(ctx)
    if rc == EXIT_OK:
        logger.info("流程走完（无 failed 步骤）")
        return rc

    failed = _first_failed_step(ctx)
    print(f"✗ 步骤失败：{failed}\n  原因：{ctx.steps[failed].fail_reason}", file=sys.stderr)
    return rc


def engine_state_path(args, workspace: Path) -> Path:
    """引擎实际使用的状态文件（用于错误提示）"""
    return (Path(args.state_file) if args.state_file
            else workspace / "config" / "engine" / "context.yaml")


if __name__ == "__main__":
    sys.exit(main())
