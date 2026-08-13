#!/usr/bin/env python3
"""day0 快速适配验证报告生成器。

独立于 generate_report.py（量产流程口径），消费 day0 专属 context 与 results：
- 成功场景：常规报告（冒烟/精度/性能/发布信息）
- 问题场景：问题总结报告（结论/现象/排障动作/根因/建议方案），文件名 FAILED_ 前缀
- 修复过但成功：常规报告附修复动作附录

用法:
    python3 generate_day0_report.py --context-yaml <context.yaml> --results-dir <dir> [--json]
    （宿主机兜底调用与容器内 /flagos-workspace/scripts/ 部署副本行为一致）

输出: <results-dir>/[FAILED_]Nvidia_<模型>_day0_<ts>.md
"""

import argparse
import datetime
import json
import os
import sys
from typing import Optional, Tuple

import yaml

# 与 chip_spec.py 同目录部署；宿主机调用时从 shared/ 同级 import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from chip_spec import vendor_en, canonical_chip_with_flag
except ImportError:
    # 容器内 scripts/ 平铺目录，chip_spec.py 与其同目录，由上方 sys.path 兜底
    def vendor_en(raw):  # type: ignore
        return (raw or "unknown").capitalize()

    def canonical_chip_with_flag(vendor, model):  # type: ignore
        return (model or "-", False)


def _load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _tag_ts(ctx: dict) -> str:
    """发布时间戳：优先 release.harbor_image 的 12 位时间戳，回退当前时间。"""
    import re
    img = ctx.get("release", {}) or {}
    for k in ("harbor_image", "image"):
        m = re.search(r"(\d{12})", str(img.get(k, "")))
        if m:
            return m.group(1)
    return datetime.datetime.now().strftime("%Y%m%d%H%M")


def _fmt_dt(s: str) -> str:
    return str(s).replace("T", " ").rstrip("Z") if s else "-"


def collect(ctx: dict, results_dir: str) -> dict:
    """汇总报告数据。"""
    d = ctx.get("day0", {}) or {}
    smoke = d.get("smoke", {}) or {}
    ev = ctx.get("eval", {}) or {}
    perf = ctx.get("perf", {}) or {}
    rel = ctx.get("release", {}) or {}
    wf = ctx.get("workflow", {}) or {}
    model = ctx.get("model", {}) or {}
    gpu = ctx.get("gpu", {}) or {}
    image = ctx.get("image", {}) or {}
    insp = ctx.get("inspection", {}) or {}

    # 问题触发判定（SKILL.md 三选一：unfixable / eval_unreachable / repair 非空）
    unfixable = bool(d.get("unfixable", False))
    eval_unreachable = bool(d.get("eval_unreachable", False))
    repair = d.get("repair", []) or []
    has_repair = len(repair) > 0
    problem = bool(d.get("problem_summary", {}).get("triggered", False)) or unfixable or eval_unreachable

    vendor = str(gpu.get("vendor", "") or "")
    v_display = vendor_en(vendor)
    chip_display, _ = canonical_chip_with_flag(vendor, str(gpu.get("type", "")))

    # 精度明细（results 下的判定结果）
    acc_cmp = _load_json(os.path.join(results_dir, "accuracy_compare_day0.json")) if results_dir else {}
    gpqa = _load_json(os.path.join(results_dir, "day0_gpqa_result.json")) if results_dir else {}

    return {
        "model": str(model.get("name", "")),
        "image": str(image.get("name", "")),
        "vendor": v_display,
        "chip": chip_display,
        "gpu_count": gpu.get("count", 0),
        "flaggems": insp.get("flag_packages", {}).get("flaggems", "-"),
        "vllm_plugin": insp.get("flag_packages", {}).get("vllm_plugin", "-"),
        "flagtree": insp.get("flag_packages", {}).get("flagtree", "-"),
        "smoke_passed": bool(smoke.get("passed", False)),
        "smoke_prompt": str(smoke.get("prompt", "中国首都在哪")),
        "smoke_answer": str(smoke.get("answer", ""))[:200],
        "smoke_retries": smoke.get("retries", 0),
        "accuracy_ok": bool(d.get("accuracy_ok", False)),
        "eval_unreachable": eval_unreachable,
        "eval_unreachable_reason": str(d.get("eval_unreachable_reason", "")),
        "score": ev.get("score", 0),
        "nv_score": ev.get("nv_score", 0),
        "nv_source": str(ev.get("nv_source", "")),
        "rel_drop_pct": ev.get("rel_drop_pct", 0),
        "threshold": ev.get("accuracy_threshold", 5.0),
        "dataset": str(ev.get("dataset", "gpqa_diamond")),
        "excluded_ops": ev.get("excluded_ops_accuracy", []) or [],
        "perf_output_tps": perf.get("output_throughput", 0),
        "perf_total_tps": perf.get("total_throughput", 0),
        "perf_test_case": str(perf.get("test_case", "")),
        "perf_concurrency": perf.get("concurrency", 0),
        "released": bool(wf.get("released", False)),
        "harbor_image": str(rel.get("harbor_image", "")),
        "modelscope_url": str(rel.get("modelscope_url", "")),
        "huggingface_url": str(rel.get("huggingface_url", "")),
        "unfixable": unfixable,
        "unfixable_reason": str(d.get("unfixable_reason", "")),
        "repair": repair,
        "has_repair": has_repair,
        "problem": problem,
        "problem_summary": d.get("problem_summary", {}) or {},
        "acc_cmp": acc_cmp,
        "gpqa": gpqa,
        "tag_ts": _tag_ts(ctx),
        "all_done": bool(wf.get("all_done", False)),
    }


def build_md(r: dict) -> str:
    failed = r["unfixable"] or r["eval_unreachable"]
    lines = []
    if failed:
        lines.append(f"# day0 迁移分析报告：{r['model']}")
        lines.append("")
        lines.append(f"# 迁移结果：❌ 失败（{'不可修复' if r['unfixable'] else '精度无效'}）")
    else:
        lines.append(f"# day0 迁移报告：{r['model']}")
        lines.append("")
        if r["released"]:
            lines.append("# 迁移结果：✅ 成功（已私有发布）")
        elif r["has_repair"]:
            lines.append("# 迁移结果：✅ 成功（已修复问题，发布状态见下）")
        else:
            lines.append("# 迁移结果：⚠ 未完成（详见各节）")
    lines.append("")

    # 基础信息
    lines.append("## 基础信息")
    lines.append(f"- 模型名: {r['model']}")
    lines.append(f"- 镜像: {r['image']}")
    lines.append(f"- 芯片: {r['vendor']} {r['chip']} x {r['gpu_count']}")
    lines.append(f"- 组件: flaggems={r['flaggems']}, vllm-plugin-FL={r['vllm_plugin']}, flagtree={r['flagtree']}")
    lines.append("")

    # 冒烟
    lines.append("## 冒烟测例（硬闸门）")
    lines.append(f"- 问题: {r['smoke_prompt']}")
    lines.append(f"- 结果: {'✓ 通过' if r['smoke_passed'] else '✗ 未通过'}")
    if not r["smoke_passed"]:
        lines.append(f"- 回答: {r['smoke_answer'] or '-'}")
    lines.append(f"- 排障重试轮数: {r['smoke_retries']}")
    lines.append("")

    # 精度
    lines.append("## 精度评测（全量 gpqa_diamond，基线 = NV 参考）")
    if r["unfixable"]:
        lines.append("- 未执行（冒烟硬闸门未过，按 day0 口径终止于排障）")
    elif r["eval_unreachable"]:
        lines.append(f"- 结果: ⚠ 评测无效（{r['eval_unreachable_reason']}）")
    else:
        lines.append(f"- 本模型得分: {r['score']}%")
        nv_src = f"（{r['nv_source']}）" if r.get("nv_source") else ""
        lines.append(f"- NV 基线得分: {r['nv_score']}%{nv_src}")
        lines.append(f"- 相对退化: {r['rel_drop_pct']}% （阈值 {r['threshold']}%）")
        lines.append(f"- 判定: {'✓ 达标' if r['accuracy_ok'] else '✗ 未达标'}")
    if r["excluded_ops"]:
        lines.append(f"- 因精度关闭的算子: {', '.join(r['excluded_ops'])}")
    lines.append("")

    # 性能
    lines.append("## 性能评测（单轮采集）")
    if r["eval_unreachable"] or r["unfixable"]:
        lines.append("- 未执行（精度无效/不可修复，按 day0 口径跳过性能）")
    elif r["perf_output_tps"]:
        lines.append(f"- 用例: {r['perf_test_case']}（并发 {r['perf_concurrency']}）")
        lines.append(f"- Output 吞吐: {r['perf_output_tps']} tok/s")
        lines.append(f"- Total 吞吐: {r['perf_total_tps']} tok/s")
    else:
        lines.append("- 无数据")
    lines.append("")

    # 发布
    lines.append("## 发布（Harbor + ModelScope + HuggingFace 全私有）")
    if r["released"]:
        lines.append(f"- Harbor: {r['harbor_image'] or '-'}")
        lines.append(f"- ModelScope: {r['modelscope_url'] or '-'}")
        lines.append(f"- HuggingFace: {r['huggingface_url'] or '-'}")
    else:
        if r["unfixable"]:
            lines.append("- 未发布（不可修复，按 day0 口径仅出问题总结报告）")
        elif r["eval_unreachable"]:
            lines.append("- 未发布（精度无效跳过性能与发布，按 day0 口径仅出问题总结报告）")
        else:
            lines.append("- 未完成")
    lines.append("")

    # 排障动作（修复过必列）
    if r["has_repair"]:
        lines.append("## 排障动作记录（day0.repair）")
        for i, act in enumerate(r["repair"], 1):
            if not isinstance(act, dict):
                continue
            ops = act.get("ops") or act.get("files") or ""
            lines.append(f"{i}. round={act.get('round', '-')} action={act.get('action', '-')}"
                         f" {('[' + str(ops) + ']') if ops else ''} → {act.get('result', '-')}")
            if act.get("note"):
                lines.append(f"   note: {act['note']}")
        lines.append("")

    # 问题总结（问题场景核心交付物）
    if r["problem"]:
        ps = r["problem_summary"]
        lines.append("## 问题总结")
        if r["unfixable"]:
            lines.append(f"- 结论: ❌ 无法适配（不可修复）")
            lines.append(f"- 原因摘要: {r['unfixable_reason'] or '-'}")
        elif r["eval_unreachable"]:
            lines.append(f"- 结论: ⚠ 精度无效（{r['eval_unreachable_reason'] or '-'}）")
        else:
            lines.append("- 结论: ✅ 已修复（修复动作见上节）")
        if ps.get("phenomenon"):
            lines.append("### 问题现象")
            lines.append(str(ps["phenomenon"]))
        if ps.get("root_cause"):
            lines.append("### 根因分析")
            lines.append(str(ps["root_cause"]))
        if ps.get("suggestion"):
            lines.append("### 建议修复方案")
            lines.append(str(ps["suggestion"]))
        lines.append("")

    # 精度判定明细（有则附）
    if r["acc_cmp"]:
        lines.append("## 精度判定明细（accuracy_compare_day0.json）")
        keys = ("aligned", "rel_drop_pct", "noise_zone", "message")
        for k in keys:
            if k in r["acc_cmp"]:
                lines.append(f"- {k}: {r['acc_cmp'][k]}")
        lines.append("")

    lines.append(f"---")
    lines.append(f"生成时间: {_fmt_dt(datetime.datetime.now().isoformat())}")
    lines.append(f"tag 时间戳: {r['tag_ts']}")
    return "\n".join(lines)


def build_basename(r: dict) -> str:
    """[FAILED_]Nvidia_<模型>_day0_<ts>.md（厂商英文名与量产报告口径一致）"""
    vendor = r["vendor"] or "unknown"
    model = (r["model"] or "unknown").split("/")[-1]
    prefix = "FAILED_" if (r["unfixable"] or r["eval_unreachable"]) else ""
    return f"{prefix}{vendor}_{model}_day0_{r['tag_ts']}.md"


def main():
    parser = argparse.ArgumentParser(description="day0 报告生成器")
    parser.add_argument("--context-yaml", required=True, help="day0 context.yaml 路径")
    parser.add_argument("--results-dir", default="", help="results 目录（读取精度判定明细）")
    parser.add_argument("--output", help="输出文件路径（默认 <results-dir>/<自动命名>.md）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ctx = _load_yaml(args.context_yaml)
    r = collect(ctx, args.results_dir)
    md = build_md(r)
    basename = build_basename(r)

    out_path = args.output or os.path.join(args.results_dir, basename) if args.results_dir or args.output \
        else basename
    if not args.output and args.results_dir:
        os.makedirs(args.results_dir, exist_ok=True)
        out_path = os.path.join(args.results_dir, basename)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)

    summary = {
        "output": out_path,
        "failed": r["unfixable"] or r["eval_unreachable"],
        "smoke_passed": r["smoke_passed"],
        "accuracy_ok": r["accuracy_ok"],
        "released": r["released"],
    }
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(f"报告已生成: {out_path}")
        print(f"  失败标记: {summary['failed']} / 冒烟: {summary['smoke_passed']} / "
              f"精度: {summary['accuracy_ok']} / 发布: {summary['released']}")


if __name__ == "__main__":
    main()
