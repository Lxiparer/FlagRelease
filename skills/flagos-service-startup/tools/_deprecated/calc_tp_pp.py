#!/usr/bin/env python3
"""
跨节点 TP/PP 自动计算工具

单机模式：只计算 TP，PP=1
多机模式：单节点内 TP，跨节点 PP

策略：
1. 优先单节点 TP（节点内通信开销小）
2. 单节点显存不够时启用 PP（跨节点）
3. TP/PP 均取 2 的幂
"""

import argparse
import json
import math
import sys


def next_power_of_2(n):
    """返回 >= n 的最小 2 的幂"""
    if n <= 1:
        return 1
    return 2 ** math.ceil(math.log2(n))


def calc_tp_single_node(model_size_gb, gpu_memory_gb, total_gpus):
    """单机模式：只计算 TP

    Args:
        model_size_gb: 模型参数量（GB）
        gpu_memory_gb: 单卡显存（GB）
        total_gpus: 可用 GPU 总数

    Returns:
        (tp_size, reason)
    """
    estimated_required_gb = model_size_gb * 1.2
    tp_needed = math.ceil(estimated_required_gb / gpu_memory_gb)

    if tp_needed > total_gpus:
        return (
            next_power_of_2(total_gpus),
            f"模型需要 {tp_needed} 卡，但只有 {total_gpus} 卡可用，TP={next_power_of_2(total_gpus)}（可能显存不足）"
        )

    tp = next_power_of_2(tp_needed)
    return (
        tp,
        f"模型 {model_size_gb}B × 1.2 = {estimated_required_gb:.1f}GB，单卡 {gpu_memory_gb}GB，需要 {tp_needed} 卡，TP={tp}"
    )


def calc_tp_pp_multi_node(model_size_gb, gpu_memory_gb, nnode, gpus_per_node):
    """多机模式：跨节点 TP/PP 分解

    策略：
    1. 优先单节点 TP（节点内通信开销小）
    2. 单节点显存不够时启用 PP（跨节点）
    3. PP 必须 <= nnode

    Args:
        model_size_gb: 模型参数量（GB）
        gpu_memory_gb: 单卡显存（GB）
        nnode: 节点总数
        gpus_per_node: 每节点 GPU 数量

    Returns:
        (tp_size, pp_size, reason)
    """
    estimated_required_gb = model_size_gb * 1.2

    # 单节点能否容纳
    single_node_capacity_gb = gpus_per_node * gpu_memory_gb
    tp_needed_single = math.ceil(estimated_required_gb / gpu_memory_gb)

    if tp_needed_single <= gpus_per_node:
        # 单节点足够，不启用 PP
        tp = next_power_of_2(tp_needed_single)
        pp = 1
        reason = f"单节点 {gpus_per_node} GPU × {gpu_memory_gb}GB = {single_node_capacity_gb}GB 足够容纳模型 {estimated_required_gb:.1f}GB，TP={tp}, PP=1"
        return tp, pp, reason

    # 需要跨节点 PP
    # TP：单节点打满（2 的幂）
    tp = next_power_of_2(gpus_per_node)

    # PP：总 GPU 数 / TP，向下取 2 的幂，不超过节点数
    total_gpus = nnode * gpus_per_node
    pp_raw = total_gpus // tp
    pp = min(next_power_of_2(pp_raw), nnode)

    # 验证是否足够
    total_capacity_gb = tp * pp * gpu_memory_gb
    if total_capacity_gb < estimated_required_gb:
        reason = f"警告：跨 {nnode} 节点，单节点 TP={tp}，PP={pp}，总容量 {total_capacity_gb}GB < 模型需求 {estimated_required_gb:.1f}GB（可能显存不足）"
    else:
        reason = f"跨 {nnode} 节点：单节点 TP={tp}，Pipeline PP={pp}，总 {tp*pp} GPU，容量 {total_capacity_gb}GB"

    return tp, pp, reason


def main():
    parser = argparse.ArgumentParser(description="计算 TP/PP 配置（单机/多机自适应）")
    parser.add_argument("--model-size", type=float, required=True, help="模型参数量（GB）")
    parser.add_argument("--gpu-memory", type=float, required=True, help="单卡显存（GB）")
    parser.add_argument("--total-gpus", type=int, help="单机模式：可用 GPU 总数")
    parser.add_argument("--nnode", type=int, help="多机模式：节点总数")
    parser.add_argument("--gpus-per-node", type=int, help="多机模式：每节点 GPU 数量")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")

    args = parser.parse_args()

    # 判断单机/多机模式
    if args.nnode and args.nnode > 1:
        # 多机模式
        if not args.gpus_per_node:
            print(json.dumps({"error": "多机模式需要 --gpus-per-node"}), file=sys.stderr)
            sys.exit(1)

        tp, pp, reason = calc_tp_pp_multi_node(
            args.model_size,
            args.gpu_memory,
            args.nnode,
            args.gpus_per_node
        )
        world_size = tp * pp

        result = {
            "mode": "multi",
            "tp_size": tp,
            "pp_size": pp,
            "world_size": world_size,
            "nnode": args.nnode,
            "gpus_per_node": args.gpus_per_node,
            "reason": reason
        }
    else:
        # 单机模式
        if not args.total_gpus:
            print(json.dumps({"error": "单机模式需要 --total-gpus"}), file=sys.stderr)
            sys.exit(1)

        tp, reason = calc_tp_single_node(
            args.model_size,
            args.gpu_memory,
            args.total_gpus
        )

        result = {
            "mode": "single",
            "tp_size": tp,
            "pp_size": 1,
            "world_size": tp,
            "reason": reason
        }

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"TP={result['tp_size']}, PP={result['pp_size']}, world_size={result['world_size']}")
        print(f"原因: {result['reason']}")


if __name__ == "__main__":
    main()
