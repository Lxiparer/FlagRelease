#!/usr/bin/env python3
"""semantic_perf_query.py — structured query layer for Gems vs Vendor triage agents.

This script consumes outputs produced by run_compare_from_db.py:

  <run-dir>/gems/final*.cpu_stack.normalized_tree.json
  <run-dir>/vendor/final*.cpu_stack.normalized_tree.json
  <run-dir>/compare_compare_*.csv/json/html

It builds an Agent-friendly index and exposes focused subcommands:

  build-index   create agent_triage_queue.jsonl + agent_semantic_index.json
  list-targets  show Gems-slower semantic optimization queue
  describe      full diagnostic packet for one semantic node
  kernels       kernel mix for one semantic node
  stacks        CPU stack clusters for one semantic node
  source        print local source context for profile source locations

The compare/probe code remains independent; this is only a read-only query layer.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


NS_PER_MS = 1_000_000


def load_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def ns_to_s(ns: float) -> str:
    ns = float(ns or 0)
    if abs(ns) >= 1_000_000:
        return f"{ns / 1_000_000:.3f} ms"
    if abs(ns) >= 1_000:
        return f"{ns / 1_000:.3f} µs"
    return f"{ns:.0f} ns"


def safe_ratio(num: float, den: float) -> Optional[float]:
    if not den:
        return None
    return float(num) / float(den)


def find_first(patterns: List[str], root: Path) -> Optional[Path]:
    for pat in patterns:
        hits = sorted(root.glob(pat))
        if hits:
            return hits[0]
    return None


def resolve_run_files(run_dir: Path) -> Dict[str, Path]:
    gems_json = find_first([
        "gems/final*.cpu_stack.normalized_tree.json",
        "src/gems/final*.cpu_stack.normalized_tree.json",
    ], run_dir)
    vendor_json = find_first([
        "vendor/final*.cpu_stack.normalized_tree.json",
        "src/vendor/final*.cpu_stack.normalized_tree.json",
    ], run_dir)
    if not gems_json or not vendor_json:
        raise FileNotFoundError(
            f"Could not find Gems/Vendor cpu_stack.normalized_tree.json under {run_dir}. "
            "Expected [src/]gems/final*.cpu_stack.normalized_tree.json and [src/]vendor/final*.cpu_stack.normalized_tree.json."
        )
    return {
        "gems_json": gems_json,
        "vendor_json": vendor_json,
        "index_json": run_dir / "agent_semantic_index.json",
        "queue_jsonl": run_dir / "agent_triage_queue.jsonl",
    }


def import_compare_module(repo_root: Path):
    mod_path = repo_root / "clean_compare_and_generate_dashboard.py"
    if not mod_path.exists():
        raise FileNotFoundError(f"Cannot find clean_compare_and_generate_dashboard.py at {mod_path}")
    spec = importlib.util.spec_from_file_location("clean_compare", mod_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module spec for {mod_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def walk_tree(node: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    yield node
    for c in node.get("children", []) or []:
        yield from walk_tree(c)


def walk_kernel_records(node: Dict[str, Any], path: List[Dict[str, Any]]) -> Iterable[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    cur = path + [{"name": node.get("name", ""), "key": node.get("key", ""), "path": node.get("path", "") }]
    for k in node.get("kernels", []) or []:
        yield k, cur
    for c in node.get("children", []) or []:
        yield from walk_kernel_records(c, cur)


def parse_source_location(frame_name: str) -> Optional[Dict[str, Any]]:
    # Examples:
    #   /vllm-workspace/vllm/vllm/v1/sample/sampler.py(131): apply_temperature
    #   flag_gems/ops/div.py(57): true_divide_
    m = re.search(r"(?P<file>[^\s]+\.py)\((?P<line>\d+)\):\s*(?P<func>.+)$", frame_name or "")
    if not m:
        return None
    return {"file": m.group("file"), "line": int(m.group("line")), "func": m.group("func").strip()}


def high_signal_frame(frame: Dict[str, Any]) -> bool:
    name = str(frame.get("name") or "")
    compact = str(frame.get("compact") or frame.get("func") or name)
    low = name.lower()
    if frame.get("is_module"):
        return True
    if any(x in low for x in ["/vllm", "vllm_ascend", "flag_gems", "torch/_ops.py", "torch/ops.py", "triton", "flaggems"]):
        return True
    if compact.startswith(("aclnn", "aten::", "npu::", "mm_kernel", "cuda::", "hip::")):
        return True
    return False


def normalize_stack_signature(frames: List[Dict[str, Any]], *, max_frames: int = 16) -> Tuple[str, List[str]]:
    selected: List[str] = []
    for f in frames:
        if high_signal_frame(f):
            selected.append(str(f.get("compact") or f.get("func") or f.get("name") or ""))
    if not selected:
        selected = [str(f.get("compact") or f.get("func") or f.get("name") or "" ) for f in frames]
    selected = [x for x in selected if x]
    if len(selected) > max_frames:
        selected = selected[:8] + ["..."] + selected[-7:]
    return " > ".join(selected), selected


def build_kernel_map(data: Dict[str, Any], side: str) -> Dict[int, Dict[str, Any]]:
    stacks = data.get("cpu_stacks", {}) or {}
    out: Dict[int, Dict[str, Any]] = {}
    for k, path_entries in walk_kernel_records(data["root"], []):
        gid = k.get("gpu_id")
        if gid is None:
            continue
        kk = dict(k)
        kk["_tree_path"] = "/".join(str(e.get("name") or e.get("key") or "") for e in path_entries if e.get("name") or e.get("key"))
        sid = kk.get("cpu_stack_id")
        frames = stacks.get(str(sid), []) if sid is not None else []
        kk["_cpu_frames"] = frames
        sig, compact = normalize_stack_signature(frames)
        kk["_stack_signature"] = sig
        kk["_stack_compact_frames"] = compact
        locs = []
        seen = set()
        for f in frames:
            loc = parse_source_location(str(f.get("name") or ""))
            if loc:
                key = (loc["file"], loc["line"], loc["func"])
                if key not in seen:
                    seen.add(key)
                    locs.append(loc)
        kk["_source_locations"] = locs
        kk["_side"] = side
        out[int(gid)] = kk
    return out


def get_shape_summary(k: Dict[str, Any]) -> str:
    shape = k.get("shape") or {}
    return str(shape.get("summary") or f"{shape.get('input_shapes','')} -> {shape.get('output_shapes','')}").strip()


def summarize_kernels(kernels: List[Dict[str, Any]], *, top: int = 30) -> Dict[str, Any]:
    by_name: Dict[str, Dict[str, Any]] = {}
    by_shape: Counter[str] = Counter()
    by_stack: Dict[str, Dict[str, Any]] = {}
    source_locs: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    total_ns = 0
    for k in kernels:
        dur = int(k.get("dur_ns", 0) or 0)
        total_ns += dur
        name = str(k.get("name") or "?")
        slot = by_name.setdefault(name, {"name": name, "count": 0, "time_ns": 0, "shape_examples": Counter(), "gpu_ids": []})
        slot["count"] += 1
        slot["time_ns"] += dur
        slot["gpu_ids"].append(k.get("gpu_id"))
        shp = get_shape_summary(k)
        if shp:
            slot["shape_examples"][shp] += 1
            by_shape[shp] += 1
        sig = k.get("_stack_signature") or "<no stack>"
        st = by_stack.setdefault(sig, {"signature": sig, "count": 0, "time_ns": 0, "frames": k.get("_stack_compact_frames", []), "gpu_ids": []})
        st["count"] += 1
        st["time_ns"] += dur
        st["gpu_ids"].append(k.get("gpu_id"))
        for loc in k.get("_source_locations", []) or []:
            key = (loc["file"], int(loc["line"]), loc["func"])
            rec = source_locs.setdefault(key, {"file": loc["file"], "line": int(loc["line"]), "func": loc["func"], "count": 0, "time_ns": 0})
            rec["count"] += 1
            rec["time_ns"] += dur
    kernel_mix = []
    for x in by_name.values():
        x = dict(x)
        x["shape_examples"] = [{"shape": s, "count": c} for s, c in x["shape_examples"].most_common(5)]
        x["time_pct"] = x["time_ns"] / total_ns if total_ns else 0.0
        kernel_mix.append(x)
    kernel_mix.sort(key=lambda x: (-x["time_ns"], x["name"]))
    stacks = sorted(by_stack.values(), key=lambda x: (-x["time_ns"], -x["count"], x["signature"]))
    shapes = [{"shape": s, "count": c} for s, c in by_shape.most_common(top)]
    locs = sorted(source_locs.values(), key=lambda x: (-x["time_ns"], -x["count"], x["file"], x["line"]))
    return {
        "total_ns": total_ns,
        "kernel_count": len(kernels),
        "kernel_mix": kernel_mix[:top],
        "shape_stats": shapes,
        "stack_clusters": stacks[:top],
        "source_locations": locs[:top],
    }


def compare_shapes(gems: Dict[str, Any], vendor: Dict[str, Any]) -> Dict[str, Any]:
    gc = Counter({x["shape"]: x["count"] for x in gems.get("shape_stats", [])})
    vc = Counter({x["shape"]: x["count"] for x in vendor.get("shape_stats", [])})
    common = []
    for s in sorted(set(gc) & set(vc)):
        common.append({"shape": s, "gems_count": gc[s], "vendor_count": vc[s]})
    gems_only = [{"shape": s, "count": c} for s, c in (gc - vc).most_common(10)]
    vendor_only = [{"shape": s, "count": c} for s, c in (vc - gc).most_common(10)]
    status = "same" if not gems_only and not vendor_only else "different"
    return {"status": status, "common": common[:10], "gems_only": gems_only, "vendor_only": vendor_only}


def collect_compare_nodes(node: Dict[str, Any], path: List[str], out: Dict[str, Dict[str, Any]]) -> None:
    p = path + [node.get("key", "root")]
    if node.get("key") != "root":
        key = "/".join(p[1:])
        out[key] = {
            "semantic_key": key,
            "name": node.get("name", ""),
            "status": node.get("status", ""),
            "gems_kernel_ns": int(node.get("gems_ns", 0) or 0),
            "vendor_kernel_ns": int(node.get("vendor_ns", 0) or 0),
            "delta_gems_minus_vendor_ns": int((node.get("gems_ns", 0) or 0) - (node.get("vendor_ns", 0) or 0)),
            "ratio_gems_over_vendor": safe_ratio(float(node.get("gems_ns", 0) or 0), float(node.get("vendor_ns", 0) or 0)),
            "gems_kernel_count": int(node.get("gems_count", 0) or 0),
            "vendor_kernel_count": int(node.get("vendor_count", 0) or 0),
            "gems_kernel_ids": list(node.get("gems_kernel_ids", []) or []),
            "vendor_kernel_ids": list(node.get("vendor_kernel_ids", []) or []),
        }
    for c in node.get("children", []) or []:
        collect_compare_nodes(c, p, out)


def node_display_path(key: str) -> str:
    return key.replace("/", " / ")


def build_index(run_dir: Path, repo_root: Path) -> Dict[str, Any]:
    paths = resolve_run_files(run_dir)
    cmp = import_compare_module(repo_root)
    gems_json = load_json(paths["gems_json"])
    vendor_json = load_json(paths["vendor_json"])
    payload = cmp.build_payload(vendor_json, gems_json, gems_html="", vendor_html="")
    compare_nodes: Dict[str, Dict[str, Any]] = {}
    collect_compare_nodes(payload["compare"]["exact"], [], compare_nodes)

    gems_kernels = build_kernel_map(gems_json, "gems")
    vendor_kernels = build_kernel_map(vendor_json, "vendor")

    # Use non-overlapping hotspot frontier first.  If a hotspot is absent from
    # exact compare keys for any reason, keep a minimal queue item anyway.
    queue: List[Dict[str, Any]] = []
    seen = set()
    for h in payload.get("hotspots", {}).get("exact", []) or []:
        key = str(h.get("path") or "")
        if not key or key in seen:
            continue
        g = int(h.get("gems_ns", 0) or 0)
        v = int(h.get("vendor_ns", 0) or 0)
        delta = g - v
        if delta <= 0:
            continue
        seen.add(key)
        queue.append({
            "rank": 0,
            "semantic_key": key,
            "display_path": h.get("display_path") or node_display_path(key),
            "name": h.get("name", ""),
            "priority_reason": "non-overlapping hotspot frontier; Gems slower by absolute kernel time",
            "gems_kernel_ns": g,
            "vendor_kernel_ns": v,
            "delta_gems_minus_vendor_ns": delta,
            "ratio_gems_over_vendor": safe_ratio(g, v),
            "gems_kernel_count": int(h.get("gems_count", 0) or 0),
            "vendor_kernel_count": int(h.get("vendor_count", 0) or 0),
            "evidence_tags": h.get("evidence_tags", []),
        })

    if not queue:
        for key, n in compare_nodes.items():
            if n["status"] != "both":
                continue
            delta = n["delta_gems_minus_vendor_ns"]
            if delta <= 0:
                continue
            queue.append({
                "rank": 0,
                "semantic_key": key,
                "display_path": node_display_path(key),
                "name": n.get("name", ""),
                "priority_reason": "Gems slower by absolute kernel time",
                "gems_kernel_ns": n["gems_kernel_ns"],
                "vendor_kernel_ns": n["vendor_kernel_ns"],
                "delta_gems_minus_vendor_ns": delta,
                "ratio_gems_over_vendor": n["ratio_gems_over_vendor"],
                "gems_kernel_count": n["gems_kernel_count"],
                "vendor_kernel_count": n["vendor_kernel_count"],
                "evidence_tags": [],
            })
    queue.sort(key=lambda x: (-int(x["delta_gems_minus_vendor_ns"]), x["semantic_key"]))
    for i, row in enumerate(queue, 1):
        row["rank"] = i

    index = {
        "schema_version": 1,
        "kind": "gpu_gems_vendor_agent_index",
        "run_dir": str(run_dir),
        "gems_json": str(paths["gems_json"]),
        "vendor_json": str(paths["vendor_json"]),
        "compare_node_count": len(compare_nodes),
        "queue_count": len(queue),
        "compare_nodes": compare_nodes,
        "queue": queue,
    }
    write_json(paths["index_json"], index)
    write_jsonl(paths["queue_jsonl"], queue)
    return index


def load_or_build_index(run_dir: Path, repo_root: Path, rebuild: bool = False) -> Dict[str, Any]:
    paths = resolve_run_files(run_dir)
    if rebuild or not paths["index_json"].exists() or not paths["queue_jsonl"].exists():
        return build_index(run_dir, repo_root)
    return load_json(paths["index_json"])


def resolve_semantic_key(index: Dict[str, Any], semantic_key: Optional[str], rank: Optional[int]) -> str:
    if semantic_key:
        if semantic_key in index.get("compare_nodes", {}):
            return semantic_key
        # allow display-path style with spaces around slash
        compact = semantic_key.replace(" / ", "/")
        if compact in index.get("compare_nodes", {}):
            return compact
        # suffix fallback, but only if unique
        matches = [k for k in index.get("compare_nodes", {}) if k.endswith(compact)]
        if len(matches) == 1:
            return matches[0]
        raise KeyError(f"Semantic key not found or ambiguous: {semantic_key!r}; matches={matches[:10]}")
    if rank is not None:
        for row in index.get("queue", []):
            if int(row.get("rank", -1)) == int(rank):
                return row["semantic_key"]
        raise KeyError(f"No queue target with rank={rank}")
    raise ValueError("Provide --semantic-key or --rank")


def diagnostic_packet(run_dir: Path, repo_root: Path, semantic_key: str, *, top: int = 20) -> Dict[str, Any]:
    paths = resolve_run_files(run_dir)
    index = load_or_build_index(run_dir, repo_root)
    node = index["compare_nodes"].get(semantic_key)
    if not node:
        raise KeyError(f"Semantic key not found: {semantic_key}")
    gems_json = load_json(paths["gems_json"])
    vendor_json = load_json(paths["vendor_json"])
    gems_map = build_kernel_map(gems_json, "gems")
    vendor_map = build_kernel_map(vendor_json, "vendor")
    gems_k = [gems_map[int(gid)] for gid in node.get("gems_kernel_ids", []) if int(gid) in gems_map]
    vendor_k = [vendor_map[int(gid)] for gid in node.get("vendor_kernel_ids", []) if int(gid) in vendor_map]
    gems_sum = summarize_kernels(gems_k, top=top)
    vendor_sum = summarize_kernels(vendor_k, top=top)
    packet = {
        "semantic_key": semantic_key,
        "display_path": node_display_path(semantic_key),
        "status": node.get("status", ""),
        "summary": {
            "gems_kernel_ns": node.get("gems_kernel_ns", 0),
            "vendor_kernel_ns": node.get("vendor_kernel_ns", 0),
            "delta_gems_minus_vendor_ns": node.get("delta_gems_minus_vendor_ns", 0),
            "ratio_gems_over_vendor": node.get("ratio_gems_over_vendor"),
            "gems_kernel_count": node.get("gems_kernel_count", 0),
            "vendor_kernel_count": node.get("vendor_kernel_count", 0),
        },
        "kernel_mix": {"gems": gems_sum["kernel_mix"], "vendor": vendor_sum["kernel_mix"]},
        "shape_diff": compare_shapes(gems_sum, vendor_sum),
        "cpu_stack_clusters": {"gems": gems_sum["stack_clusters"], "vendor": vendor_sum["stack_clusters"]},
        "source_locations": {"gems": gems_sum["source_locations"], "vendor": vendor_sum["source_locations"]},
        "kernel_ids": {"gems": node.get("gems_kernel_ids", []), "vendor": node.get("vendor_kernel_ids", [])},
    }
    return packet


def print_targets(index: Dict[str, Any], limit: int, sort: str) -> None:
    rows = list(index.get("queue", []))
    if sort == "ratio":
        rows.sort(key=lambda x: (-(x.get("ratio_gems_over_vendor") or 0), -int(x.get("delta_gems_minus_vendor_ns", 0))))
    elif sort == "gems":
        rows.sort(key=lambda x: (-int(x.get("gems_kernel_ns", 0)), x.get("semantic_key", "")))
    else:
        rows.sort(key=lambda x: (-int(x.get("delta_gems_minus_vendor_ns", 0)), x.get("semantic_key", "")))
    print(f"{'rank':>4}  {'delta':>12}  {'Gems':>12}  {'Vendor':>12}  {'G/V':>8}  semantic")
    for row in rows[:limit]:
        ratio = row.get("ratio_gems_over_vendor")
        r = "-" if ratio is None else f"{ratio:.3f}x"
        print(f"{int(row.get('rank', 0)):>4}  {ns_to_s(row.get('delta_gems_minus_vendor_ns',0)):>12}  "
              f"{ns_to_s(row.get('gems_kernel_ns',0)):>12}  {ns_to_s(row.get('vendor_kernel_ns',0)):>12}  "
              f"{r:>8}  {row.get('display_path') or row.get('semantic_key')}")


def print_packet_text(packet: Dict[str, Any], *, show_stacks: bool = True) -> None:
    s = packet["summary"]
    ratio = s.get("ratio_gems_over_vendor")
    print(f"# {packet['display_path']}")
    print(f"status: {packet.get('status')}")
    print(f"Gems:   {ns_to_s(s['gems_kernel_ns'])}  ({s['gems_kernel_count']} kernels)")
    print(f"Vendor: {ns_to_s(s['vendor_kernel_ns'])}  ({s['vendor_kernel_count']} kernels)")
    print(f"Delta:  {ns_to_s(s['delta_gems_minus_vendor_ns'])}  Gems/Vendor={('-' if ratio is None else f'{ratio:.3f}x')}")
    print("\n## Kernel mix")
    for side in ["gems", "vendor"]:
        print(f"\n### {side}")
        for k in packet["kernel_mix"][side][:10]:
            shapes = "; ".join(f"{x['shape']}×{x['count']}" for x in k.get("shape_examples", [])[:2])
            print(f"- {k['name']}: {ns_to_s(k['time_ns'])}, count={k['count']}, {k.get('time_pct',0)*100:.1f}%" + (f", shape={shapes}" if shapes else ""))
    print("\n## Shape diff")
    sd = packet.get("shape_diff", {})
    print(f"status: {sd.get('status')}")
    if sd.get("common"):
        print("common:")
        for x in sd["common"][:5]:
            print(f"- {x['shape']}  Gems×{x['gems_count']} Vendor×{x['vendor_count']}")
    if sd.get("gems_only"):
        print("gems-only:")
        for x in sd["gems_only"][:5]:
            print(f"- {x['shape']} ×{x['count']}")
    if sd.get("vendor_only"):
        print("vendor-only:")
        for x in sd["vendor_only"][:5]:
            print(f"- {x['shape']} ×{x['count']}")
    if show_stacks:
        print("\n## CPU stack clusters")
        for side in ["gems", "vendor"]:
            print(f"\n### {side}")
            for st in packet["cpu_stack_clusters"][side][:5]:
                print(f"- {ns_to_s(st['time_ns'])}, count={st['count']}: {st['signature']}")
    print("\n## Source locations")
    for side in ["gems", "vendor"]:
        print(f"\n### {side}")
        for loc in packet["source_locations"][side][:10]:
            print(f"- {loc['file']}:{loc['line']} {loc['func']}  count={loc['count']} time={ns_to_s(loc['time_ns'])}")


def print_kernel_mix(packet: Dict[str, Any]) -> None:
    for side in ["gems", "vendor"]:
        print(f"# {side}")
        for k in packet["kernel_mix"][side]:
            print(json.dumps(k, ensure_ascii=False))


def print_stacks(packet: Dict[str, Any], side: str, full: bool) -> None:
    sides = [side] if side in {"gems", "vendor"} else ["gems", "vendor"]
    for s in sides:
        print(f"# {s}")
        for i, st in enumerate(packet["cpu_stack_clusters"][s], 1):
            print(f"\n## stack {i}: {ns_to_s(st['time_ns'])}, count={st['count']}")
            if full:
                for fr in st.get("frames", []):
                    print(f"  {fr}")
            else:
                print(st.get("signature", ""))


def apply_path_map(path: str, maps: List[str]) -> str:
    for m in maps:
        if "=" not in m:
            continue
        src, dst = m.split("=", 1)
        if path.startswith(src):
            return dst + path[len(src):]
    return path


def print_source_context(packet: Dict[str, Any], side: str, source_root: Optional[Path], path_maps: List[str], context_lines: int) -> None:
    locs = packet["source_locations"].get(side, [])
    if not locs:
        print(f"No source locations for side={side}")
        return
    for loc in locs[:10]:
        raw = str(loc["file"])
        mapped = apply_path_map(raw, path_maps)
        p = Path(mapped)
        if source_root and not p.is_absolute():
            p = source_root / p
        print(f"\n# {raw}:{loc['line']} {loc['func']}  count={loc['count']} time={ns_to_s(loc['time_ns'])}")
        if not p.exists():
            print(f"[missing] {p}")
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as e:
            print(f"[error reading {p}] {e}")
            continue
        line = int(loc["line"])
        lo = max(1, line - context_lines)
        hi = min(len(lines), line + context_lines)
        for n in range(lo, hi + 1):
            mark = ">" if n == line else " "
            print(f"{mark}{n:5d}: {lines[n-1]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Query Ascend Gems/Vendor compare outputs for agent triage.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--run-dir", required=True, help="Pipeline output directory containing gems/, vendor/, compare files")
        p.add_argument("--repo-root", default=None, help="Package root. Default: parent of this script")

    p = sub.add_parser("build-index", help="Build agent_semantic_index.json and agent_triage_queue.jsonl")
    common(p)

    p = sub.add_parser("list-targets", help="List Gems-slower semantic optimization targets")
    common(p)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--sort", choices=["delta", "ratio", "gems"], default="delta")

    p = sub.add_parser("describe", help="Describe one semantic target")
    common(p)
    p.add_argument("--semantic-key", default=None)
    p.add_argument("--rank", type=int, default=None)
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--top", type=int, default=20)

    p = sub.add_parser("kernels", help="Show kernel mix for one semantic target")
    common(p)
    p.add_argument("--semantic-key", default=None)
    p.add_argument("--rank", type=int, default=None)

    p = sub.add_parser("stacks", help="Show CPU stack clusters for one semantic target")
    common(p)
    p.add_argument("--semantic-key", default=None)
    p.add_argument("--rank", type=int, default=None)
    p.add_argument("--side", choices=["gems", "vendor", "both"], default="both")
    p.add_argument("--full", action="store_true")

    p = sub.add_parser("source", help="Print local source context for one semantic target")
    common(p)
    p.add_argument("--semantic-key", default=None)
    p.add_argument("--rank", type=int, default=None)
    p.add_argument("--side", choices=["gems", "vendor"], required=True)
    p.add_argument("--source-root", default=None)
    p.add_argument("--path-map", action="append", default=[], help="Map profile prefix to local prefix, e.g. /vllm-workspace=/Users/me/vllm-workspace")
    p.add_argument("--context-lines", type=int, default=30)

    args = ap.parse_args()
    run_dir = Path(args.run_dir).resolve()
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]

    if args.cmd == "build-index":
        idx = build_index(run_dir, repo_root)
        paths = resolve_run_files(run_dir)
        print(f"[+] wrote {paths['index_json']}")
        print(f"[+] wrote {paths['queue_jsonl']}")
        print(f"[*] queue_count={idx['queue_count']} compare_node_count={idx['compare_node_count']}")
        return

    idx = load_or_build_index(run_dir, repo_root)
    if args.cmd == "list-targets":
        print_targets(idx, args.limit, args.sort)
        return

    key = resolve_semantic_key(idx, getattr(args, "semantic_key", None), getattr(args, "rank", None))
    packet = diagnostic_packet(run_dir, repo_root, key, top=getattr(args, "top", 20))
    if args.cmd == "describe":
        if args.format == "json":
            print(json.dumps(packet, ensure_ascii=False, indent=2))
        else:
            print_packet_text(packet)
    elif args.cmd == "kernels":
        print_kernel_mix(packet)
    elif args.cmd == "stacks":
        print_stacks(packet, args.side, args.full)
    elif args.cmd == "source":
        print_source_context(packet, args.side, Path(args.source_root).resolve() if args.source_root else None, args.path_map, args.context_lines)


if __name__ == "__main__":
    main()
