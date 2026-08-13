#!/usr/bin/env python3
"""day0 段间门控确定性判定。

day0 流程的段间 gate 全部由本脚本判定，不信任 Claude 自述（与 v1_gate.py 同思路）。
由 run_day0.sh 在段边界从宿主机 context 快照读取判定。

用法:
    python3 day0_gate.py --context <context.yaml> --check smoke|accuracy

退出码: 0=通过（进入下一段）, 1=不通过（跳过后续段，走报告收尾）, 2=context 缺失/解析失败

门控语义（用户 2026-08-13 定稿口径）:
  smoke    段1→段2：冒烟测例通过（day0.smoke.passed=true）且未标记不可修复
  accuracy 段2→段3：精度达标（day0.accuracy_ok=true）才做性能；
           不通过 = 精度报错/无效无法解决，或调优穷尽仍不达标 → 跳过性能，报告收尾
"""

import argparse
import json
import sys

import yaml


def load_context(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            ctx = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(f"ERROR: context 文件不存在: {path}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"ERROR: context 解析失败: {e}", file=sys.stderr)
        sys.exit(2)
    return ctx


def gate_smoke(ctx: dict) -> tuple:
    """段1→段2：冒烟硬闸门。"""
    day0 = ctx.get("day0", {}) or {}
    smoke = day0.get("smoke", {}) or {}
    unfixable = bool(day0.get("unfixable", False))

    if unfixable:
        return False, "不可修复标记已置位（unfixable=true），跳过精度/性能，报告收尾"
    if bool(smoke.get("passed", False)):
        return True, "冒烟测例通过"
    return False, f"冒烟测例未通过（answer={str(smoke.get('answer', ''))[:100]}）"


def gate_accuracy(ctx: dict) -> tuple:
    """段2→段3：精度达标才做性能。"""
    day0 = ctx.get("day0", {}) or {}
    if bool(day0.get("unfixable", False)):
        return False, "不可修复标记已置位（unfixable=true），跳过性能，报告收尾"
    if bool(day0.get("eval_unreachable", False)):
        return False, f"精度报错/无效且无法解决（{str(day0.get('eval_unreachable_reason', ''))[:100]}），跳过性能"
    if bool(day0.get("accuracy_ok", False)):
        return True, "精度达标（相对 NV 退化 ≤5%），进入性能评测"
    return False, "精度未达标且调优穷尽，跳过性能，报告收尾"


def main():
    parser = argparse.ArgumentParser(description="day0 段间门控判定")
    parser.add_argument("--context", required=True, help="context.yaml 路径（宿主机快照）")
    parser.add_argument("--check", required=True, choices=["smoke", "accuracy"],
                        help="门控类型")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ctx = load_context(args.context)

    if args.check == "smoke":
        passed, reason = gate_smoke(ctx)
    else:
        passed, reason = gate_accuracy(ctx)

    result = {"check": args.check, "pass": passed, "reason": reason}

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"gate[{args.check}]: {'✓ 通过' if passed else '✗ 不通过'} — {reason}")

    # 供编排层解析的机器可读标记
    print(f"[DAY0_GATE]{json.dumps(result, ensure_ascii=False)}[/DAY0_GATE]")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
