#!/usr/bin/env python3
"""
生成精度对比文件和算子配置文件

Usage:
    python3 generate_comparison_and_config.py \
        --candidate v3 \
        --dataset gpqa_diamond \
        --nv-baseline /flagos-workspace/shared/nv_baseline.yaml \
        --workspace /flagos-workspace
"""

import argparse
import json
import yaml
import os
from pathlib import Path
import sys


def load_nv_baseline(baseline_file: str, dataset: str):
    """从 nv_baseline.yaml 读取基线

    Args:
        baseline_file: nv_baseline.yaml 路径
        dataset: 数据集名称

    Returns:
        NV baseline 精度值，失败返回 None
    """
    try:
        with open(baseline_file, 'r', encoding='utf-8') as f:
            baseline_data = yaml.safe_load(f)

        # 支持两种格式：
        # 1. {gpqa_diamond: 66.8, mmlu: 69.1}
        # 2. {datasets: {gpqa_diamond: {accuracy: 66.8}}}

        if dataset in baseline_data:
            value = baseline_data[dataset]
            # 如果是字典，提取 accuracy 字段
            if isinstance(value, dict):
                return value.get('accuracy')
            return value

        # 嵌套格式
        datasets = baseline_data.get('datasets', {})
        if dataset in datasets:
            entry = datasets[dataset]
            if isinstance(entry, dict):
                return entry.get('accuracy')
            return entry

        return None

    except Exception as e:
        print(f"✗ Error loading NV baseline: {e}")
        return None


def load_evaluation_result(workspace: str, candidate: str):
    """读取评测结果

    Args:
        workspace: 工作空间根目录
        candidate: v3/v4

    Returns:
        评测结果字典，失败返回 None
    """
    result_file = Path(workspace) / "results" / f"gpqa_{candidate}.json"

    try:
        with open(result_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"✗ Error: Evaluation result not found: {result_file}")
        return None
    except json.JSONDecodeError as e:
        print(f"✗ Error: Invalid JSON in {result_file}: {e}")
        return None


def load_operator_config(workspace: str):
    """从 context.yaml 读取算子配置

    Args:
        workspace: 工作空间根目录

    Returns:
        算子配置字典
    """
    context_file = Path(workspace) / "shared" / "context.yaml"

    try:
        with open(context_file, 'r', encoding='utf-8') as f:
            context = yaml.safe_load(f)

        optimization = context.get("optimization", {})

        return {
            "enabled_ops": optimization.get("enabled_ops", []),
            "disabled_ops": optimization.get("disabled_ops", []),
            "category": optimization.get("category", "unknown"),
            "reason": optimization.get("reason", ""),
        }

    except Exception as e:
        print(f"⚠ Warning: Could not load operator config: {e}")
        return {
            "enabled_ops": [],
            "disabled_ops": [],
            "category": "unknown",
            "reason": "Failed to load from context.yaml",
        }


def generate_comparison_file(
    workspace: str,
    candidate: str,
    dataset: str,
    accuracy: float,
    nv_reference: float,
):
    """生成精度对比文件

    Args:
        workspace: 工作空间根目录
        candidate: v3/v4
        dataset: 数据集名称
        accuracy: 当前精度
        nv_reference: NV baseline 精度

    Returns:
        是否达标
    """
    relative_drop = (nv_reference - accuracy) / nv_reference
    qualified = relative_drop <= 0.05

    comparison = {
        "nv": nv_reference,
        "current": accuracy,
        "rel_drop_pct": relative_drop * 100,
        "aligned": qualified,
        "message": f"{'达标' if qualified else '不达标'} (vs NV {nv_reference:.2f}%, rel_drop {relative_drop*100:.2f}%)",
        "dataset": dataset,
        "candidate": candidate,
    }

    # 主对比文件
    if dataset == "gpqa_diamond":
        output_file = Path(workspace) / "results" / f"accuracy_compare_{candidate}.json"
    else:
        output_file = Path(workspace) / "results" / f"accuracy_compare_{dataset}_{candidate}.json"

    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(comparison, f, indent=2, ensure_ascii=False)

    print(f"✓ Generated: {output_file}")
    return qualified


def export_operator_config(
    workspace: str,
    candidate: str,
    op_config: dict,
):
    """导出算子配置

    Args:
        workspace: 工作空间根目录
        candidate: v3/v4
        op_config: 算子配置字典
    """
    config = {
        "enabled_ops": op_config.get("enabled_ops", []),
        "disabled_ops": op_config.get("disabled_ops", []),
        "category": op_config.get("category", "unknown"),
        "reason": op_config.get("reason", ""),
        "revision_id": f"{candidate}_final",
        "candidate": candidate,
    }

    output_file = Path(workspace) / "results" / f"operator_config_{candidate}.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"✓ Generated: {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate comparison and config files for report"
    )
    parser.add_argument(
        "--candidate",
        required=True,
        choices=["v3", "v4"],
        help="Candidate version (v3 or v4)"
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="Dataset name (e.g., gpqa_diamond, mmlu, math_500)"
    )
    parser.add_argument(
        "--nv-baseline",
        required=True,
        help="Path to nv_baseline.yaml"
    )
    parser.add_argument(
        "--workspace",
        default="/flagos-workspace",
        help="Workspace root directory"
    )

    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  Generating comparison and config for {args.candidate}")
    print(f"{'='*60}\n")

    # 1. 读取 NV baseline
    print(f"[1/4] Loading NV baseline from {args.nv_baseline}...")
    nv_reference = load_nv_baseline(args.nv_baseline, args.dataset)

    if nv_reference is None:
        print(f"✗ Error: NV baseline not found for dataset '{args.dataset}'")
        print(f"  Please check that {args.nv_baseline} contains an entry for '{args.dataset}'")
        sys.exit(1)

    print(f"  ✓ NV baseline: {nv_reference:.2f}%")

    # 2. 读取评测结果
    print(f"\n[2/4] Loading evaluation result...")
    eval_result = load_evaluation_result(args.workspace, args.candidate)

    if eval_result is None:
        print(f"✗ Error: Could not load evaluation result for {args.candidate}")
        sys.exit(1)

    accuracy = eval_result.get('accuracy')
    if accuracy is None:
        print(f"✗ Error: 'accuracy' field not found in evaluation result")
        print(f"  Available fields: {list(eval_result.keys())}")
        sys.exit(1)

    print(f"  ✓ {args.candidate} accuracy: {accuracy:.2f}%")

    # 3. 生成对比文件
    print(f"\n[3/4] Generating comparison file...")
    qualified = generate_comparison_file(
        args.workspace,
        args.candidate,
        args.dataset,
        accuracy,
        nv_reference,
    )

    # 4. 读取并导出算子配置
    print(f"\n[4/4] Exporting operator configuration...")
    op_config = load_operator_config(args.workspace)
    export_operator_config(args.workspace, args.candidate, op_config)

    # 总结
    print(f"\n{'='*60}")
    print(f"  Summary for {args.candidate}")
    print(f"{'='*60}")
    print(f"  Dataset:       {args.dataset}")
    print(f"  Accuracy:      {accuracy:.2f}%")
    print(f"  NV Baseline:   {nv_reference:.2f}%")
    print(f"  Relative Drop: {((nv_reference - accuracy) / nv_reference * 100):.2f}%")
    print(f"  Qualified:     {'✓ YES' if qualified else '✗ NO'}")
    print(f"  Enabled Ops:   {len(op_config.get('enabled_ops', []))} ops")
    print(f"  Disabled Ops:  {len(op_config.get('disabled_ops', []))} ops")
    print(f"{'='*60}\n")

    sys.exit(0 if qualified else 1)


if __name__ == "__main__":
    main()
