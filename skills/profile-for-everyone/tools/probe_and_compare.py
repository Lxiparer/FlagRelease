#!/usr/bin/env python3
"""probe_and_compare.py — 输入两个 Perfetto trace，自动 probe + compare 出 dashboard。

Usage:
  python probe_and_compare.py \
    --gems-trace  gems.pt.trace.json.gz \
    --vendor-trace vendor.pt.trace.json.gz \
    --schema ./hygon_h100_compare_v19_llm_io_log/probe/schema.json \
    --platform ./hygon_h100_compare_v19_llm_io_log/probe/platform.json \
    --decode-idx 100 \
    --out-dir ./output

Output:
  <out-dir>/
    compare_dashboard.html
    compare_nodes.csv
    compare_templates.csv
    compare_hotspots.csv
    compare_unmatched.json
    src/gems/   (probe artifacts)
    src/vendor/ (probe artifacts)
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Probe two traces → compare dashboard.")
    ap.add_argument("--gems-trace", required=True, help="Gems .pt.trace.json.gz")
    ap.add_argument("--vendor-trace", required=True, help="Vendor .pt.trace.json.gz")
    ap.add_argument("--schema", default=str(root / "probe" / "schema.json"), help="schema.json")
    ap.add_argument("--platform", default=str(root / "probe" / "platform.json"), help="platform.json")
    ap.add_argument("--decode-idx", type=int, default=100)
    ap.add_argument("--decode-boundary-mode", choices=["phase", "marker"], default="phase")
    ap.add_argument("--decode-anchor-lookaround-ms", type=float, default=50.0)
    ap.add_argument("--cpu-stack-tree-detail",
                    choices=["module_vllm", "module", "concise", "source", "verbose"],
                    default="module_vllm")
    ap.add_argument("--include-comm-time", action="store_true")
    ap.add_argument("--trace-processor-shell", default=str(root / "probe" / "trace_processor_shell"))
    ap.add_argument("--override", default="", help="Manual module name override JSON")
    ap.add_argument("--out-dir", default=str(root / "output"))
    ap.add_argument("--clean-output", action="store_true")
    args = ap.parse_args()

    probe_dir = root / "probe"
    compare_script = root / "clean_compare_and_generate_dashboard.py"

    for p in [probe_dir / "clear_probe.py", probe_dir / "probe_trace.py", compare_script]:
        if not p.exists():
            sys.exit(f"[ERROR] Required file not found: {p}")

    # Import probe modules
    if str(probe_dir) not in sys.path:
        sys.path.insert(0, str(probe_dir))
    import clear_probe as v1
    import probe_trace as probe_mod

    # Import compare module
    spec = importlib.util.spec_from_file_location("clean_compare", str(compare_script))
    compare_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compare_mod)

    # Output dirs
    out_dir = Path(args.out_dir).resolve()
    if args.clean_output and out_dir.exists():
        print(f"[*] Cleaning: {out_dir}")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src_dir = out_dir / "src"
    src_dir.mkdir(exist_ok=True)

    schema = v1.load_schema(args.schema)
    platform = v1.load_platform(args.platform)
    tp_shell = args.trace_processor_shell or None

    # ---- Probe one side ----
    def probe_one(side: str, trace_path: str) -> tuple:
        side_dir = src_dir / side
        side_dir.mkdir(exist_ok=True)
        prefix = str(side_dir / f"final{args.decode_idx}_")
        slice_path = str(side_dir / f"final{args.decode_idx}_.decode_slice.json.gz")

        t0 = time.perf_counter()
        print(f"\n{'='*60}")
        print(f"[*] Probing {side}: {trace_path}")
        print(f"{'='*60}")

        tree_json, cpu_tree_json, template_json, kernels, boundary_debug, unaccounted, slice_info = \
            probe_mod.probe_trace(
                trace_path,
                schema=schema,
                platform=platform,
                decode_idx=args.decode_idx,
                decode_marker=None,
                include_kernels_in_tree_json=True,
                boundary_mode=args.decode_boundary_mode,
                boundary_lookaround_ms=args.decode_anchor_lookaround_ms,
                cpu_stack_tree_detail=args.cpu_stack_tree_detail,
                include_comm_time=args.include_comm_time,
                trace_processor_shell=tp_shell,
                slice_output_path=slice_path,
            )

        # Write all probe outputs
        tree_path = f"{prefix}.normalized_tree.json"
        cpu_tree_path = f"{prefix}.cpu_stack.normalized_tree.json"
        template_path = f"{prefix}.semantic_template.json"
        csv_path = f"{prefix}.kernel_timeline.csv"
        html_path = f"{prefix}.semantic_tree.html"
        cpu_html_path = f"{prefix}.cpu_stack.html"
        boundary_path = f"{prefix}.boundary_debug.csv"
        unaccounted_path = f"{prefix}.unaccounted_gpu_activity.json"

        probe_mod.write_json(tree_path, tree_json)
        probe_mod.write_json(cpu_tree_path, cpu_tree_json)
        probe_mod.write_json(template_path, template_json)
        probe_mod.write_kernel_csv(csv_path, kernels, args.decode_idx)
        probe_mod.write_boundary_debug_csv(boundary_path, boundary_debug)
        probe_mod.write_json(unaccounted_path, unaccounted, pretty=True)
        probe_mod.render_html(tree_path, html_path, schema, title=f"Semantic decode tree — {side} — decode #{args.decode_idx}")
        probe_mod.render_html(cpu_tree_path, cpu_html_path, schema, title=f"CPU-stack decode tree — {side} — decode #{args.decode_idx}")

        elapsed = time.perf_counter() - t0
        print(f"[+] {side} done: {len(kernels)} kernels, {elapsed:.1f}s")
        return cpu_tree_path, cpu_html_path, slice_path

    # ---- Run probe for both sides ----
    gems_tree_path, gems_html_path, gems_slice_path = probe_one("gems", args.gems_trace)
    vendor_tree_path, vendor_html_path, vendor_slice_path = probe_one("vendor", args.vendor_trace)

    # ---- Compare ----
    print(f"\n{'='*60}")
    print(f"[*] Compare + Dashboard")
    print(f"{'='*60}")

    gems_json = compare_mod.load_json(gems_tree_path)
    vendor_json = compare_mod.load_json(vendor_tree_path)
    override = compare_mod.load_json(args.override) if args.override else {}

    # HTML paths relative to out_dir for drill links
    def rel(p):
        try:
            return str(Path(p).resolve().relative_to(out_dir))
        except ValueError:
            return str(Path(p).resolve())

    payload = compare_mod.build_payload(
        vendor_json, gems_json,
        override=override,
        gems_html=rel(gems_html_path),
        vendor_html=rel(vendor_html_path),
        gems_trace_slice=gems_slice_path,
        vendor_trace_slice=vendor_slice_path,
    )

    # Derived aliases
    for mode in ["folded", "exact"]:
        derived = payload["derived_aliases"].get(mode, {})
        if derived:
            print(f"[+] Derived aliases ({mode}): {derived}")

    # Write CSVs
    compare_prefix = str(out_dir / "compare_")

    rows = []
    compare_mod.flatten_compare_tree(payload["compare"]["exact"], [], rows)
    rows.sort(key=lambda r: (r["status"] != "both", -abs(int(r["delta_ns"])), r["path"]))
    compare_mod.write_csv(compare_prefix + "nodes.csv", rows)

    folded_rows = []
    compare_mod.flatten_compare_tree(payload["compare"]["folded"], [], folded_rows)
    compare_mod.write_csv(compare_prefix + "templates.csv", folded_rows)

    hotspot_rows = []
    for h in payload["hotspots"]["exact"]:
        hotspot_rows.append({
            "path": h["path"],
            "display_path": h.get("display_path", h["path"]),
            "name": h["name"],
            "ratio_gems_over_vendor": "" if h.get("ratio_gems_over_vendor") is None else f"{h['ratio_gems_over_vendor']:.6f}",
            "gems_kernel_ns": h["gems_ns"],
            "vendor_kernel_ns": h["vendor_ns"],
            "delta_gems_minus_vendor_ns": h["delta_gems_minus_vendor_ns"],
            "abs_delta_ms": h.get("abs_delta_ms", 0),
            "gems_kernel_count": h["gems_count"],
            "vendor_kernel_count": h["vendor_count"],
            "evidence_tags": ";".join(h.get("evidence_tags", [])),
            "diagnosis": h.get("diagnosis", ""),
        })
    compare_mod.write_csv(compare_prefix + "hotspots.csv", hotspot_rows)
    compare_mod.write_json(compare_prefix + "unmatched.json", {"unmatched": payload["unmatched"]})

    # Dashboard HTML
    safe_payload = compare_mod.sanitize_for_json(payload)
    b64 = base64.b64encode(
        json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).decode("ascii")
    html = compare_mod.HTML.replace("__DATA_B64__", b64)
    out_html = str(out_dir / "compare_dashboard.html")
    Path(out_html).write_text(html, encoding="utf-8")

    # ---- Summary ----
    matched = sum(1 for r in rows if r["status"] == "both")
    one_sided = len(rows) - matched
    folded_matched = sum(1 for r in folded_rows if r["status"] == "both")
    folded_one_sided = len(folded_rows) - folded_matched

    print(f"\n{'='*60}")
    print(f"[+] Done!")
    print(f"{'='*60}")
    print(f"    Dashboard: {out_html}")
    print(f"    Gems tree: {gems_tree_path}")
    print(f"    Vendor tree: {vendor_tree_path}")
    print(f"[*] exact:  nodes={len(rows)} matched={matched} one_sided={one_sided}")
    print(f"[*] folded: nodes={len(folded_rows)} matched={folded_matched} one_sided={folded_one_sided}")
    print(f"[*] vendor: {payload['vendor']['kernel_ms']:.3f} ms ({payload['vendor']['kernel_count']} kernels)")
    print(f"[*] gems:   {payload['gems']['kernel_ms']:.3f} ms ({payload['gems']['kernel_count']} kernels)")

    hotspots = payload["hotspots"]["folded"][:10]
    if hotspots:
        print(f"\n[*] Top hotspots (G/V ratio):")
        print(f"    {'Path':<50} {'G/V':>8} {'Gems ms':>10} {'Vendor ms':>10} {'Delta ms':>10}")
        for h in hotspots:
            ratio = h.get("ratio_gems_over_vendor")
            ratio_s = f"{ratio:.3f}x" if ratio else "-"
            print(f"    {h['display_path']:<50} {ratio_s:>8} {h['gems_ms']:>10.3f} {h['vendor_ms']:>10.3f} {h['delta_gems_minus_vendor_ms']:>10.3f}")


if __name__ == "__main__":
    main()
