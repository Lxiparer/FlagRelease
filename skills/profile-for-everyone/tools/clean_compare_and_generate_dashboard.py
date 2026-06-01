#!/usr/bin/env python3
"""clean_compare_and_generate_dashboard.py

Clean compare pipeline: uses fix_compare module-path matching logic
(no LLM dependency) and generates the full v19-style HTML dashboard.

Pages: Vendor audit, Gems audit, Compare tree, Drill, Hotspots.
(Unmatched page removed -- no LLM needed.)

Inputs: two *.cpu_stack.normalized_tree.json (probe output).

Example:
  python clean_compare_and_generate_dashboard.py \
    --gems   ./gems/final30_.cpu_stack.normalized_tree.json \
    --vendor ./vendor/final30_.cpu_stack.normalized_tree.json \
    --out-prefix ./compare/qwen3_30b_
"""
from __future__ import annotations

import argparse
import base64
import copy
import csv
import json
import math
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from html import escape as html_escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ===========================================================================
# fix_compare matching logic (module-path based, no LLM)
# ===========================================================================

PLUGIN_SUFFIXES = ["FL"]
ROOT_WRAPPERS = {"worker_busy_loop", "run_busy_loop", "root"}
PHASE_SKIP = {"execute_model"}
ALIGN_KFAM_MIN = 0.0


def load_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, obj: Any) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def is_module_node(n: Dict[str, Any]) -> bool:
    return n.get("category") == "module" or str(n.get("name", "")).startswith("nn.Module: ")


def canon_module(name: str, override: Dict[str, str]) -> Tuple[str, str]:
    x = name[len("nn.Module: "):] if name.startswith("nn.Module: ") else name
    x = re.sub(r"\s+×\d+$", "", x)
    m = re.match(r"(.+?)_(\d+)$", x)
    occ = ""
    if m:
        x, occ = m.group(1), m.group(2)
    for suf in PLUGIN_SUFFIXES:
        if x.endswith(suf) and len(x) > len(suf):
            x = x[:-len(suf)]
    x = override.get(x, x)
    return x, occ


def is_decode(n: str) -> bool:
    return bool(re.match(r"decode_token\[\d+\]", n))


def walk_kernels(node, path, out):
    cur = path + [(node.get("name", ""), node.get("category", ""))]
    for k in node.get("kernels", []) or []:
        out.append((k, cur))
    for c in node.get("children", []) or []:
        walk_kernels(c, cur, out)


def kernel_family(kname: str) -> str:
    s = re.sub(r"<.*?>", "", str(kname))
    s = re.sub(r"\d+", "", s).lower()
    for tok in ["rmsnorm", "layernorm", "rope", "rotary", "silu", "gemm", "matmul",
                "softmax", "topk", "moe", "attention", "reduce", "elementwise",
                "cast", "copy", "index", "embedding", "nccl", "rccl", "triton",
                "flash", "norm"]:
        if tok in s:
            return tok
    return s[:24] or "?"


def compact_kernel_name(name: str, max_len: int = 150) -> str:
    x = str(name or "?")
    x = x.replace("void ", "")
    x = re.sub(r" object at 0x[0-9a-fA-F]+", "", x)
    if len(x) > max_len:
        parts = x.split("::")
        if len(parts) > 2:
            tail = "::".join(parts[-2:])
            x = parts[0] + "::...::" + tail
        if len(x) > max_len:
            x = x[:max_len - 3] + "..."
    return x


def phase_label(path) -> str:
    for name, cat in path:
        if name in ROOT_WRAPPERS or is_decode(name) or name in PHASE_SKIP:
            continue
        return "phase:" + re.sub(r"_(\d+)$", "", name)
    return "phase:other"


def _should_include_source(name: str, node_in_tree: Any) -> bool:
    """Decide if a source node should be included in the path projection.

    We include source nodes that are semantically meaningful:
    - They have module children (they wrap modules)
    - They have ≥2 kernels directly (they do real work)
    We exclude thin wrappers like forward_oot, forward_hip, forward_cuda.
    """
    SKIP_SOURCES = {"forward_oot", "forward_hip", "forward_cuda", "forward_native",
                    "forward_impl", "forward"}
    base = re.sub(r"_\d+$", "", name)
    if base in SKIP_SOURCES:
        return False
    return True


def project_path(path, override, fold: bool, *, include_source: bool = False,
                  skip_types: Optional[set] = None) -> List[Tuple[str, str, str]]:
    """Returns list of (key, display_name, instance_id) tuples.

    When include_source=True, meaningful source nodes that appear AFTER the first
    module node are included with 'src:' prefix. Source nodes before any module
    are treated as phase/root wrappers and handled by phase_label.

    skip_types: set of canonical module type names to treat as transparent wrappers
    (skip them in the path, promoting their children up one level).
    """
    mods = []
    seen_module = False
    _skip = skip_types or set()
    for name, cat in path:
        if is_module_node({"name": name, "category": cat}):
            ctype, occ = canon_module(name, override)
            if ctype in _skip:
                continue
            mods.append(("mod", ctype, occ))
            seen_module = True
        elif include_source and cat == "source" and seen_module:
            if _should_include_source(name, None):
                clean = re.sub(r"_\d+$", "", name)
                mods.append(("src", clean, ""))
    if not mods:
        ph = phase_label(path)
        return [(ph, ph.split(":", 1)[1], "")]
    segs = []
    for kind, ctype, occ in mods:
        if kind == "src":
            segs.append((f"src:{ctype}", ctype, ""))
        elif fold:
            segs.append((f"mod:{ctype}", ctype, occ))
        else:
            tag = f"{ctype}_{occ}" if occ else ctype
            segs.append((f"mod:{tag}", f"{ctype}[{occ}]" if occ else ctype, occ))
    return segs


# Per-side aggregation tree
class MatchNode:
    __slots__ = ("key", "name", "children", "ns", "cnt", "kfam", "first_ts",
                 "kernel_names", "kernel_time_by_name", "kernel_ids", "instances")

    def __init__(self, key, name):
        self.key = key
        self.name = name
        self.children: OrderedDict = OrderedDict()
        self.ns = 0
        self.cnt = 0
        self.kfam: Counter = Counter()
        self.kernel_names: Counter = Counter()
        self.kernel_time_by_name: Counter = Counter()
        self.kernel_ids: List[Any] = []
        self.instances: set = set()
        self.first_ts = None

    def child(self, key, name):
        if key not in self.children:
            self.children[key] = MatchNode(key, name)
        return self.children[key]


def build_match_side(root, override, fold, *, include_source: bool = False,
                     skip_types: Optional[set] = None):
    recs = []
    walk_kernels(root, [], recs)
    T = MatchNode("root", "root")
    for kid, (k, path) in enumerate(recs):
        dur = int(k.get("dur_ns", 0) or 0)
        ts = k.get("ts")
        fam = kernel_family(k.get("name", ""))
        kname = compact_kernel_name(str(k.get("name", "")), 80)
        kernel_id = k.get("gpu_id")
        if kernel_id is None:
            kernel_id = kid
        cur = T
        cur.ns += dur
        cur.cnt += 1
        cur.kfam[fam] += dur
        cur.kernel_names[kname] += 1
        cur.kernel_time_by_name[kname] += dur
        cur.kernel_ids.append(kernel_id)
        for key, name, inst_id in project_path(path, override, fold, include_source=include_source,
                                               skip_types=skip_types):
            cur = cur.child(key, name)
            cur.ns += dur
            cur.cnt += 1
            cur.kfam[fam] += dur
            cur.kernel_names[kname] += 1
            cur.kernel_time_by_name[kname] += dur
            cur.kernel_ids.append(kernel_id)
            if inst_id:
                cur.instances.add(inst_id)
            if ts is not None:
                cur.first_ts = ts if cur.first_ts is None else min(cur.first_ts, int(ts))
    return T


def jacc_counter(a: Counter, b: Counter) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if (sa | sb) else 0.0


def _extract_base_type(mod_key: str) -> str:
    """Extract base module type from a key like 'mod:SomeType_3' -> 'SomeType'."""
    raw = mod_key.split("#")[0][4:]  # strip 'mod:' prefix
    m = re.match(r"(.+?)_(\d+)$", raw)
    return m.group(1) if m else raw


def derive_alignment(V: MatchNode, G: MatchNode) -> Dict[str, str]:
    add = {}
    def rec(vn, gn):
        vk = set(vn.children)
        gk = set(gn.children)
        v_only = [vn.children[k] for k in vn.children if k not in gk and k.startswith("mod:")]
        g_only = [gn.children[k] for k in gn.children if k not in vk and k.startswith("mod:")]
        v_only.sort(key=lambda x: (x.first_ts if x.first_ts is not None else 1 << 62))
        g_only.sort(key=lambda x: (x.first_ts if x.first_ts is not None else 1 << 62))
        for vc, gc in zip(v_only, g_only):
            if jacc_counter(vc.kfam, gc.kfam) >= ALIGN_KFAM_MIN:
                gt = _extract_base_type(gc.key)
                vt = _extract_base_type(vc.key)
                if gt != vt:
                    add[gt] = vt
        for k in vk & gk:
            rec(vn.children[k], gn.children[k])
    rec(V, G)
    return add


def detect_transparent_wrappers(V: MatchNode, G: MatchNode) -> set:
    """Find module types that are transparent wrappers on one side only.

    A transparent wrapper is detected when one side has a single unmatched module
    child whose own children overlap with the other side's multiple unmatched children.
    """
    skip: set = set()

    def rec(vn, gn):
        vk = set(vn.children)
        gk = set(gn.children)
        v_only_mods = [k for k in vn.children if k not in gk and k.startswith("mod:")]
        g_only_mods = [k for k in gn.children if k not in vk and k.startswith("mod:")]

        # Vendor has 1 wrapper, Gems has many direct children
        if len(v_only_mods) == 1 and len(g_only_mods) >= 2:
            wrapper = vn.children[v_only_mods[0]]
            w_types = {_extract_base_type(k) for k in wrapper.children if k.startswith("mod:")}
            o_types = {_extract_base_type(k) for k in g_only_mods}
            if w_types and o_types and len(w_types & o_types) / len(w_types | o_types) >= 0.3:
                skip.add(_extract_base_type(v_only_mods[0]))

        # Gems has 1 wrapper, Vendor has many direct children
        if len(g_only_mods) == 1 and len(v_only_mods) >= 2:
            wrapper = gn.children[g_only_mods[0]]
            w_types = {_extract_base_type(k) for k in wrapper.children if k.startswith("mod:")}
            o_types = {_extract_base_type(k) for k in v_only_mods}
            if w_types and o_types and len(w_types & o_types) / len(w_types | o_types) >= 0.3:
                skip.add(_extract_base_type(g_only_mods[0]))

        for k in vk & gk:
            rec(vn.children[k], gn.children[k])

    rec(V, G)
    return skip


def run_matching(vroot, groot, override, fold, iters=3, *, include_source: bool = False):
    ov = dict(override)
    skip_types: set = set()
    for _ in range(iters):
        V = build_match_side(vroot, ov, fold, include_source=include_source, skip_types=skip_types)
        G = build_match_side(groot, ov, fold, include_source=include_source, skip_types=skip_types)
        add = derive_alignment(V, G)
        new_skip = detect_transparent_wrappers(V, G)
        if not add and not (new_skip - skip_types):
            break
        ov.update(add)
        skip_types |= new_skip
    V = build_match_side(vroot, ov, fold, include_source=include_source, skip_types=skip_types)
    G = build_match_side(groot, ov, fold, include_source=include_source, skip_types=skip_types)
    if skip_types:
        print(f"[+] transparent wrappers skipped: {skip_types}")
    return V, G, ov


# ===========================================================================
# Merge two MatchNode trees into a compare tree (dict-based for JSON/dashboard)
# ===========================================================================

def merge_trees(v: Optional[MatchNode], g: Optional[MatchNode], key="root", name="root") -> Dict[str, Any]:
    vc = {k: v.children[k] for k in v.children} if v else {}
    gc = {k: g.children[k] for k in g.children} if g else {}
    keys = list(dict.fromkeys(list(vc) + list(gc)))
    children = [merge_trees(vc.get(k), gc.get(k), k, (vc.get(k) or gc.get(k)).name) for k in keys]
    children.sort(key=lambda c: (c["first_ts"] if c["first_ts"] is not None else 1 << 62, c["name"]))
    vn = v.ns if v else 0
    gn = g.ns if g else 0
    v_cnt = v.cnt if v else 0
    g_cnt = g.cnt if g else 0
    fts = [x for x in [v.first_ts if v else None, g.first_ts if g else None] if x is not None]
    # Instance count for folded display (×N)
    v_inst = len(v.instances) if v and v.instances else 0
    g_inst = len(g.instances) if g and g.instances else 0
    max_inst = max(v_inst, g_inst)
    display_name = name  # no ×N here; folding adds it later
    # Node type tags
    is_module = key.startswith("mod:")
    is_source = key.startswith("src:")
    # Kernel stats
    v_kstats = []
    g_kstats = []
    if v:
        for kn, cnt in v.kernel_names.most_common(30):
            v_kstats.append({"name": kn, "count": cnt, "time_ms": v.kernel_time_by_name[kn] / 1e6})
    if g:
        for kn, cnt in g.kernel_names.most_common(30):
            g_kstats.append({"name": kn, "count": cnt, "time_ms": g.kernel_time_by_name[kn] / 1e6})

    return {
        "key": key,
        "name": display_name,
        "is_module": is_module,
        "is_source": is_source,
        "vendor_ns": vn,
        "gems_ns": gn,
        "vendor_ms": vn / 1e6,
        "gems_ms": gn / 1e6,
        "delta_ns": gn - vn,
        "delta_ms": (gn - vn) / 1e6,
        "ratio": (gn / vn) if vn > 0 and gn > 0 else None,
        "vendor_count": v_cnt,
        "gems_count": g_cnt,
        "vendor_instances": v_inst,
        "gems_instances": g_inst,
        "status": "both" if (vn and gn) else "vendor_only" if vn else "gems_only",
        "first_ts": min(fts) if fts else None,
        "children": children,
        "vendor_kernel_stats": v_kstats,
        "gems_kernel_stats": g_kstats,
        "vendor_kernel_ids": v.kernel_ids if v else [],
        "gems_kernel_ids": g.kernel_ids if g else [],
    }


# ===========================================================================
# Audit tree (single-side view for Vendor/Gems tabs)
# ===========================================================================

def build_audit_tree(node: MatchNode, path: str = "") -> Dict[str, Any]:
    p = (path + "/" + node.key) if path else node.key
    kstats = [{"name": kn, "count": cnt, "time_ms": node.kernel_time_by_name[kn] / 1e6}
              for kn, cnt in node.kernel_names.most_common(40)]
    children = [build_audit_tree(c, p) for c in node.children.values()]
    children.sort(key=lambda c: (c.get("first_ts") if c.get("first_ts") is not None else 1 << 62, c["name"]))
    return {
        "key": node.key,
        "name": node.name,
        "path": p,
        "is_module": node.key.startswith("mod:"),
        "is_source": node.key.startswith("src:"),
        "total_ms": node.ns / 1e6,
        "kernel_count": node.cnt,
        "first_ts": node.first_ts,
        "kernel_stats": kstats,
        "children": children,
    }


# ===========================================================================
# Hotspot extraction (from compare tree)
# ===========================================================================

BROAD_HOTSPOT_SKIP = {
    "root", "worker_busy_loop", "decode_token[*]", "execute_model", "model_forward",
    "hidden_select_and_logits", "input_and_attention_metadata",
}


def _ns(n: Dict[str, Any], key: str) -> float:
    return float(n.get(key, 0) or 0.0)


def _is_both_nonzero(n: Dict[str, Any]) -> bool:
    return n.get("status") == "both" and _ns(n, "gems_ns") > 0 and _ns(n, "vendor_ns") > 0


def _is_broad_node(n: Dict[str, Any]) -> bool:
    key = str(n.get("key", ""))
    name = str(n.get("name", ""))
    return key in BROAD_HOTSPOT_SKIP or name in BROAD_HOTSPOT_SKIP


def _same_time_envelope(parent: Dict[str, Any], child: Dict[str, Any], tol: float = 0.95) -> bool:
    pg, pv = _ns(parent, "gems_ns"), _ns(parent, "vendor_ns")
    cg, cv = _ns(child, "gems_ns"), _ns(child, "vendor_ns")
    if pg <= 0 or pv <= 0:
        return False
    return (cg / pg) >= tol and (cv / pv) >= tol


def _node_delta_gv(n: Dict[str, Any]) -> float:
    return _ns(n, "gems_ns") - _ns(n, "vendor_ns")


def _hotspot_display_path(key_path: List[str], display_path: List[str]) -> str:
    parts = [p for p in display_path if p and p != "root"]
    if not parts:
        return "root"
    while parts and parts[0] in {"execute_model", "model_forward"} and len(parts) > 2:
        parts = parts[1:]
    if len(parts) > 4:
        return "... / " + " / ".join(parts[-4:])
    return " / ".join(parts)


def _kernel_mix_diff(gems_stats: List[Dict[str, Any]], vendor_stats: List[Dict[str, Any]]) -> Dict[str, Any]:
    gm = {str(x.get("name", "")): x for x in (gems_stats or [])[:30]}
    vm = {str(x.get("name", "")): x for x in (vendor_stats or [])[:30]}
    common_keys = set(gm) & set(vm)
    common = sorted([
        {"name": k, "gems_ms": gm[k].get("time_ms", 0), "vendor_ms": vm[k].get("time_ms", 0),
         "delta_ms": float(gm[k].get("time_ms", 0) or 0) - float(vm[k].get("time_ms", 0) or 0)}
        for k in common_keys
    ], key=lambda x: -abs(x["delta_ms"]))[:10]
    gems_only = sorted([
        {"name": k, "gems_ms": v.get("time_ms", 0)} for k, v in gm.items() if k not in common_keys
    ], key=lambda x: -float(x.get("gems_ms", 0) or 0))[:10]
    vendor_only = sorted([
        {"name": k, "vendor_ms": v.get("time_ms", 0)} for k, v in vm.items() if k not in common_keys
    ], key=lambda x: -float(x.get("vendor_ms", 0) or 0))[:10]
    top_gems = gems_stats[0]["name"] if gems_stats else ""
    top_vendor = vendor_stats[0]["name"] if vendor_stats else ""
    return {"common": common, "gems_only": gems_only, "vendor_only": vendor_only,
            "top_gems": top_gems, "top_vendor": top_vendor}


def _children_contribution(n: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for c in n.get("children", []) or []:
        if c.get("status") != "both":
            continue
        g = _ns(c, "gems_ns")
        v = _ns(c, "vendor_ns")
        if g <= 0 and v <= 0:
            continue
        out.append({
            "name": c.get("name", ""),
            "gems_ms": g / 1e6,
            "vendor_ms": v / 1e6,
            "delta_ms": (g - v) / 1e6,
            "ratio_gems_over_vendor": g / v if v > 0 else None,
        })
    out.sort(key=lambda x: -abs(x["delta_ms"]))
    return out[:12]


def _evidence_tags(h: Dict[str, Any]) -> List[str]:
    tags = []
    km = h.get("kernel_mix", {})
    if km.get("gems_only") or km.get("vendor_only") or km.get("top_gems") != km.get("top_vendor"):
        tags.append("KERNEL_MIX_DIFF")
    if h.get("children_contribution"):
        tags.append("HAS_CHILD_BREAKDOWN")
    return tags


def _make_hotspot(n: Dict[str, Any], key_path: List[str], display_path: List[str]) -> Dict[str, Any]:
    g_ns = int(float(n.get("gems_ns", 0) or 0))
    v_ns = int(float(n.get("vendor_ns", 0) or 0))
    full_key_path = key_path + [str(n.get("key") or n.get("name") or "")]
    full_display_path = display_path + [str(n.get("name") or n.get("key") or "")]
    kernel_mix = _kernel_mix_diff(n.get("gems_kernel_stats", []), n.get("vendor_kernel_stats", []))
    h = {
        "path": "/".join(x for x in full_key_path[1:] if x),
        "display_path": _hotspot_display_path(full_key_path, full_display_path),
        "full_display_path": " / ".join(x for x in full_display_path[1:] if x),
        "name": n.get("name", ""),
        "gems_ns": g_ns,
        "vendor_ns": v_ns,
        "gems_ms": g_ns / 1e6,
        "vendor_ms": v_ns / 1e6,
        "ratio_gems_over_vendor": g_ns / v_ns if v_ns else None,
        "ratio_vendor_over_gems": v_ns / g_ns if g_ns else None,
        "delta_gems_minus_vendor_ns": g_ns - v_ns,
        "delta_gems_minus_vendor_ms": (g_ns - v_ns) / 1e6,
        "abs_delta_ms": abs(g_ns - v_ns) / 1e6,
        "gems_count": n.get("gems_count", 0),
        "vendor_count": n.get("vendor_count", 0),
        "gems_kernel_stats": n.get("gems_kernel_stats", []),
        "vendor_kernel_stats": n.get("vendor_kernel_stats", []),
        "kernel_mix": kernel_mix,
        "children_contribution": _children_contribution(n),
    }
    h["evidence_tags"] = _evidence_tags(h)
    if "KERNEL_MIX_DIFF" in h["evidence_tags"]:
        h["diagnosis"] = "Kernel mix differs between Gems and Vendor."
    else:
        h["diagnosis"] = "Both sides structurally comparable; difference is aggregate kernel time."
    return h


def _frontier_hotspots(n: Dict[str, Any], key_path: List[str], display_path: List[str], *, min_ns: int, min_delta_ns: int) -> List[Dict[str, Any]]:
    child_results: List[Dict[str, Any]] = []
    for c in n.get("children", []) or []:
        child_results.extend(_frontier_hotspots(
            c,
            key_path + [str(n.get("key") or n.get("name") or "")],
            display_path + [str(n.get("name") or n.get("key") or "")],
            min_ns=min_ns, min_delta_ns=min_delta_ns))

    if not _is_both_nonzero(n):
        return child_results
    if max(_ns(n, "gems_ns"), _ns(n, "vendor_ns")) < min_ns:
        return child_results
    if abs(_node_delta_gv(n)) < min_delta_ns:
        return child_results

    children_both = [c for c in n.get("children", []) or [] if _is_both_nonzero(c)]
    if len(children_both) == 1 and _same_time_envelope(n, children_both[0]):
        return [_make_hotspot(n, key_path, display_path)]
    if _is_broad_node(n) and child_results:
        return child_results
    parent_delta = abs(_node_delta_gv(n))
    child_delta = sum(abs(_node_delta_gv(c)) for c in children_both)
    if child_results and parent_delta > 0 and child_delta >= 0.65 * parent_delta and len(children_both) > 1:
        return child_results
    return [_make_hotspot(n, key_path, display_path)]


def build_hotspots(compare_tree: Dict[str, Any], *, limit: int = 500, min_ns: int = 1_000, min_delta_ns: int = 1) -> List[Dict[str, Any]]:
    items = _frontier_hotspots(compare_tree, [], [], min_ns=min_ns, min_delta_ns=min_delta_ns)
    dedup: OrderedDict = OrderedDict()
    for h in items:
        dedup.setdefault(h["path"], h)
    items = list(dedup.values())
    items.sort(key=lambda x: (-(x.get("ratio_gems_over_vendor") or 0.0), -x.get("abs_delta_ms", 0.0), x["path"]))
    return items[:limit]


# ===========================================================================
# Flatten / CSV helpers
# ===========================================================================

def flatten_compare_tree(node: Dict[str, Any], path: List[str], out: List[Dict[str, Any]]) -> None:
    p = path + [node["key"]]
    if node.get("key") != "root":
        out.append({
            "path": "/".join(p[1:]),
            "name": node["name"],
            "status": node["status"],
            "gems_kernel_ns": node["gems_ns"],
            "vendor_kernel_ns": node["vendor_ns"],
            "delta_ns": node["delta_ns"],
            "ratio_gems_over_vendor": "" if node.get("ratio") is None else f"{node['ratio']:.6f}",
            "gems_kernel_count": node["gems_count"],
            "vendor_kernel_count": node["vendor_count"],
        })
    for c in node.get("children", []) or []:
        flatten_compare_tree(c, p, out)


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        Path(path).write_text("", encoding="utf-8")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


# ===========================================================================
# Source node absorption: remove one-sided src: nodes, merge into parent
# ===========================================================================

def absorb_one_sided_sources(node: Dict[str, Any]) -> Dict[str, Any]:
    """Remove src: nodes that are one-sided (only exist on one platform).

    Their kernel time/ids are already counted in the parent via the tree
    aggregation, so we just need to remove them from children and redistribute
    their children upward.
    """
    children = node.get("children", []) or []
    if not children:
        return node

    new_children = []
    for c in children:
        c = absorb_one_sided_sources(c)
        key = str(c.get("key", ""))
        if key.startswith("src:") and c.get("status") != "both":
            # Absorb: promote this node's children to parent level
            for grandchild in c.get("children", []) or []:
                new_children.append(grandchild)
        else:
            new_children.append(c)

    new_children.sort(key=lambda c: (c["first_ts"] if c["first_ts"] is not None else 1 << 62, c["name"]))
    result = dict(node)
    result["children"] = new_children
    return result


# ===========================================================================
# Layer-only folding: fold only at transformer layer boundaries
# ===========================================================================

_MOD_KEY_RE = re.compile(r"^mod:(.+?)_(\d+)$")

_LAYER_FOLD_MIN_SIBLINGS = 3  # minimum same-type siblings to trigger folding


def _base_type_from_key(key: str) -> Optional[str]:
    m = _MOD_KEY_RE.match(key)
    return m.group(1) if m else None


def _child_signature(node: Dict[str, Any]) -> str:
    """Structural signature: sorted set of immediate child module base types."""
    child_types = []
    for c in node.get("children", []) or []:
        key = str(c.get("key", ""))
        m = _MOD_KEY_RE.match(key)
        if m:
            child_types.append(m.group(1))
        elif key.startswith("mod:"):
            child_types.append(key[4:])
    return "|".join(sorted(set(child_types)))


def _aggregate_children(all_nodes: List[Dict[str, Any]], rep_children: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate children across all nodes in a fold group.

    Uses the representative's child structure (names/keys) but sums up
    kernel_ids, time, and counts from all nodes' corresponding children.
    Matching is done by normalized key (base type for mod:, exact for src:).
    """
    if not rep_children:
        return []

    def _normalize_key(key: str) -> str:
        """Normalize key for matching: strip occurrence from mod: keys."""
        if key.startswith("mod:"):
            m = _MOD_KEY_RE.match(key)
            if m:
                return f"mod:{m.group(1)}"
        return key

    # Build lookup: normalized_key -> list of children from all nodes
    from collections import defaultdict
    all_children_by_nkey: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for node in all_nodes:
        node_children = node.get("children", []) or []
        for c in node_children:
            nk = _normalize_key(c.get("key", ""))
            all_children_by_nkey[nk].append(c)

    aggregated = []
    seen_nkeys = set()

    for rep_child in rep_children:
        rep_key = rep_child.get("key", "")
        nk = _normalize_key(rep_key)
        if nk in seen_nkeys:
            continue
        seen_nkeys.add(nk)

        corresponding = all_children_by_nkey.get(nk, [])
        if not corresponding:
            aggregated.append(rep_child)
            continue

        # Aggregate this child's data
        c_v_ns = sum(x.get("vendor_ns", 0) for x in corresponding)
        c_g_ns = sum(x.get("gems_ns", 0) for x in corresponding)
        c_v_cnt = sum(x.get("vendor_count", 0) for x in corresponding)
        c_g_cnt = sum(x.get("gems_count", 0) for x in corresponding)
        c_fts = [x.get("first_ts") for x in corresponding if x.get("first_ts") is not None]
        c_v_ids = [kid for x in corresponding for kid in x.get("vendor_kernel_ids", [])]
        c_g_ids = [kid for x in corresponding for kid in x.get("gems_kernel_ids", [])]

        # Aggregate kernel stats
        c_v_kstats: Dict[str, Dict[str, float]] = {}
        c_g_kstats: Dict[str, Dict[str, float]] = {}
        for x in corresponding:
            for ks in x.get("vendor_kernel_stats", []):
                nm = ks["name"]
                c_v_kstats.setdefault(nm, {"count": 0, "time_ms": 0})
                c_v_kstats[nm]["count"] += ks["count"]
                c_v_kstats[nm]["time_ms"] += ks["time_ms"]
            for ks in x.get("gems_kernel_stats", []):
                nm = ks["name"]
                c_g_kstats.setdefault(nm, {"count": 0, "time_ms": 0})
                c_g_kstats[nm]["count"] += ks["count"]
                c_g_kstats[nm]["time_ms"] += ks["time_ms"]
        c_v_kstats_list = sorted([{"name": k, "count": v["count"], "time_ms": v["time_ms"]}
                                   for k, v in c_v_kstats.items()], key=lambda x: -x["time_ms"])[:30]
        c_g_kstats_list = sorted([{"name": k, "count": v["count"], "time_ms": v["time_ms"]}
                                   for k, v in c_g_kstats.items()], key=lambda x: -x["time_ms"])[:30]

        # Recursively aggregate grandchildren
        rep_grandchildren = rep_child.get("children", []) or []
        merged_grandchildren = _aggregate_children(corresponding, rep_grandchildren)

        status = "both" if (c_v_ns and c_g_ns) else "vendor_only" if c_v_ns else "gems_only"
        agg_child = {
            "key": rep_child.get("key", ""),
            "name": rep_child.get("name", ""),
            "is_module": rep_child.get("is_module", False),
            "is_source": rep_child.get("is_source", False),
            "vendor_ns": c_v_ns,
            "gems_ns": c_g_ns,
            "vendor_ms": c_v_ns / 1e6,
            "gems_ms": c_g_ns / 1e6,
            "delta_ns": c_g_ns - c_v_ns,
            "delta_ms": (c_g_ns - c_v_ns) / 1e6,
            "ratio": (c_g_ns / c_v_ns) if c_v_ns > 0 and c_g_ns > 0 else None,
            "vendor_count": c_v_cnt,
            "gems_count": c_g_cnt,
            "vendor_instances": len(corresponding),
            "gems_instances": len(corresponding),
            "status": status,
            "first_ts": min(c_fts) if c_fts else None,
            "children": merged_grandchildren,
            "vendor_kernel_stats": c_v_kstats_list,
            "gems_kernel_stats": c_g_kstats_list,
            "vendor_kernel_ids": c_v_ids,
            "gems_kernel_ids": c_g_ids,
        }
        aggregated.append(agg_child)

    return aggregated


def _make_representative(nodes: List[Dict[str, Any]], base_type: str) -> Dict[str, Any]:
    """Create a folded ×N node from a group of structurally identical nodes.

    Uses the representative's child structure but aggregates kernel_ids/time
    across ALL nodes' children so drill-down works for every sub-node.
    """
    n = len(nodes)
    v_ns = sum(x.get("vendor_ns", 0) for x in nodes)
    g_ns = sum(x.get("gems_ns", 0) for x in nodes)
    v_cnt = sum(x.get("vendor_count", 0) for x in nodes)
    g_cnt = sum(x.get("gems_count", 0) for x in nodes)
    fts_vals = [x.get("first_ts") for x in nodes if x.get("first_ts") is not None]

    # Aggregate kernel stats at this level
    v_kstats: Dict[str, Dict[str, float]] = {}
    g_kstats: Dict[str, Dict[str, float]] = {}
    for node in nodes:
        for ks in node.get("vendor_kernel_stats", []):
            nm = ks["name"]
            v_kstats.setdefault(nm, {"count": 0, "time_ms": 0})
            v_kstats[nm]["count"] += ks["count"]
            v_kstats[nm]["time_ms"] += ks["time_ms"]
        for ks in node.get("gems_kernel_stats", []):
            nm = ks["name"]
            g_kstats.setdefault(nm, {"count": 0, "time_ms": 0})
            g_kstats[nm]["count"] += ks["count"]
            g_kstats[nm]["time_ms"] += ks["time_ms"]
    v_kstats_list = sorted([{"name": k, "count": v["count"], "time_ms": v["time_ms"]}
                            for k, v in v_kstats.items()], key=lambda x: -x["time_ms"])[:30]
    g_kstats_list = sorted([{"name": k, "count": v["count"], "time_ms": v["time_ms"]}
                            for k, v in g_kstats.items()], key=lambda x: -x["time_ms"])[:30]
    v_ids = [kid for node in nodes for kid in node.get("vendor_kernel_ids", [])]
    g_ids = [kid for node in nodes for kid in node.get("gems_kernel_ids", [])]

    # Aggregate children: use representative's structure but merge data from all nodes
    rep = max(nodes, key=lambda x: x.get("vendor_ns", 0) + x.get("gems_ns", 0))
    rep_children = rep.get("children", []) or []
    merged_children = _aggregate_children(nodes, rep_children)

    display_name = f"{base_type} ×{n}"
    status = "both" if (v_ns and g_ns) else "vendor_only" if v_ns else "gems_only"
    return {
        "key": f"mod:{base_type}",
        "name": display_name,
        "is_module": True,
        "is_source": False,
        "vendor_ns": v_ns,
        "gems_ns": g_ns,
        "vendor_ms": v_ns / 1e6,
        "gems_ms": g_ns / 1e6,
        "delta_ns": g_ns - v_ns,
        "delta_ms": (g_ns - v_ns) / 1e6,
        "ratio": (g_ns / v_ns) if v_ns > 0 and g_ns > 0 else None,
        "vendor_count": v_cnt,
        "gems_count": g_cnt,
        "vendor_instances": n,
        "gems_instances": n,
        "status": status,
        "first_ts": min(fts_vals) if fts_vals else None,
        "children": merged_children,  # aggregated children with all kernel_ids
        "vendor_kernel_stats": v_kstats_list,
        "gems_kernel_stats": g_kstats_list,
        "vendor_kernel_ids": v_ids,
        "gems_kernel_ids": g_ids,
    }


def fold_compare_tree(node: Dict[str, Any], depth: int = 0, max_fold_depth: int = 2) -> Dict[str, Any]:
    """Fold only at transformer layer boundaries (max 2 levels).

    A group of ≥3 same-base-type module siblings with the same child signature
    gets folded into a single ×N representative node. Inside the representative,
    no further folding occurs — the full structure is shown.
    """
    children = node.get("children", []) or []
    if not children:
        return node

    if depth >= max_fold_depth:
        # No more folding allowed — return node as-is
        return node

    # Group children by (base_type, child_signature) — only for mod: keys with occurrence
    groups: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for c in children:
        key = str(c.get("key", ""))
        base = _base_type_from_key(key)
        if base:
            sig = _child_signature(c)
            gkey = f"{base}@{sig}"
        else:
            gkey = key  # non-module or no occurrence — unique
        groups.setdefault(gkey, []).append(c)

    new_children = []
    for gkey, group in groups.items():
        if len(group) >= _LAYER_FOLD_MIN_SIBLINGS:
            # Foldable group — create representative
            base = gkey.split("@")[0] if "@" in gkey else gkey
            new_children.append(_make_representative(group, base))
        elif len(group) == 1:
            # Single node — recurse to check if IT has foldable children
            new_children.append(fold_compare_tree(group[0], depth + 1, max_fold_depth))
        else:
            # 2 siblings — not enough to fold, keep individual but recurse
            for c in group:
                new_children.append(fold_compare_tree(c, depth + 1, max_fold_depth))

    new_children.sort(key=lambda c: (c["first_ts"] if c["first_ts"] is not None else 1 << 62, c["name"]))
    result = dict(node)
    result["children"] = new_children
    return result


def sanitize_for_json(obj: Any) -> Any:
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    if isinstance(obj, tuple):
        return [sanitize_for_json(v) for v in obj]
    return obj


def fmt_ns(ns: int) -> str:
    ns = int(ns or 0)
    if abs(ns) >= 1_000_000:
        return f"{ns/1_000_000:.3f} ms"
    if abs(ns) >= 1_000:
        return f"{ns/1_000:.3f} us"
    return f"{ns} ns"


# ===========================================================================
# Build full payload for dashboard
# ===========================================================================


def _read_file_b64(path: str) -> str:
    """Read a file and return its contents as base64. Returns '' if path is empty or missing."""
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    return base64.b64encode(p.read_bytes()).decode("ascii")


def build_payload(vendor_json: Dict[str, Any], gems_json: Dict[str, Any], *,
                  override: Optional[Dict[str, str]] = None,
                  gems_html: str = "", vendor_html: str = "",
                  gems_trace_slice: str = "", vendor_trace_slice: str = "") -> Dict[str, Any]:
    """Build the complete dashboard payload using fix_compare matching logic."""
    import base64 as _b64
    ov = override or {}
    vroot = vendor_json["root"]
    groot = gems_json["root"]

    # Run matching (exact mode, with source nodes included)
    V_exact, G_exact, ov_exact = run_matching(vroot, groot, ov, fold=False, include_source=True)

    # Build exact compare tree
    compare_exact = merge_trees(V_exact, G_exact)

    # Absorb one-sided source nodes (two-pass: source consensus)
    compare_exact = absorb_one_sided_sources(compare_exact)

    # Build folded compare tree by layer-only structural grouping
    compare_folded = fold_compare_tree(compare_exact)

    # Build audit trees (single-side views)
    vendor_audit_exact = build_audit_tree(V_exact)
    gems_audit_exact = build_audit_tree(G_exact)
    # Folded audit: fold the exact audit trees
    vendor_audit_folded = fold_compare_tree(vendor_audit_exact)
    gems_audit_folded = fold_compare_tree(gems_audit_exact)

    # Build hotspots
    hotspots = {
        "exact": build_hotspots(compare_exact),
        "folded": build_hotspots(compare_folded),
    }

    # Flatten for unmatched list (kept for payload structure, but no LLM analysis)
    rows: List[Dict[str, Any]] = []
    flatten_compare_tree(compare_exact, [], rows)
    unmatched = [r for r in rows if r["status"] != "both" and
                 (int(r["gems_kernel_ns"]) or int(r["vendor_kernel_ns"]))]
    unmatched.sort(key=lambda r: (-max(int(r["gems_kernel_ns"] or 0), int(r["vendor_kernel_ns"] or 0)), r["path"]))

    # Vendor/Gems side metadata
    vendor_kernel_ms = V_exact.ns / 1e6
    gems_kernel_ms = G_exact.ns / 1e6
    vendor_kernel_count = V_exact.cnt
    gems_kernel_count = G_exact.cnt

    return {
        "vendor": {
            "label": "Vendor",
            "kernel_ms": vendor_kernel_ms,
            "kernel_count": vendor_kernel_count,
            "audit_folded": vendor_audit_folded,
            "audit_exact": vendor_audit_exact,
        },
        "gems": {
            "label": "Gems",
            "kernel_ms": gems_kernel_ms,
            "kernel_count": gems_kernel_count,
            "audit_folded": gems_audit_folded,
            "audit_exact": gems_audit_exact,
        },
        "compare": {
            "exact": compare_exact,
            "folded": compare_folded,
        },
        "hotspots": hotspots,
        "unmatched": unmatched,
        "unmatched_analysis": {"mode": "off", "exact": [], "folded": [], "stats": {}},
        "side_html": {"gems": gems_html, "vendor": vendor_html},
        "side_trace": {"gems": gems_trace_slice, "vendor": vendor_trace_slice},
        "side_trace_b64": {
            "gems": _read_file_b64(gems_trace_slice),
            "vendor": _read_file_b64(vendor_trace_slice),
        },
        "derived_aliases": {
            "folded": {k: v for k, v in ov_exact.items() if k not in (override or {})},
            "exact": {k: v for k, v in ov_exact.items() if k not in (override or {})},
        },
    }


# ===========================================================================
# HTML Dashboard Template (from v19, Unmatched page removed)
# ===========================================================================

HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Gems vs Vendor Semantic Dashboard</title>
<style>
:root{--vendor:#d94b4b;--gems:#239b56;--bg:#f6f7fb;--card:#fff;--text:#172033;--muted:#667085;--line:#d9dee7;--blue:#3451d1;--purple:#7c3aed;}
.ntag{display:inline-block;font-size:9px;font-weight:700;border-radius:3px;padding:1px 4px;margin-right:4px;vertical-align:middle;line-height:1.2}.ntag.mod{background:#e8f4fd;color:#1d4ed8;border:1px solid #bfdbfe}.ntag.src{background:#fef3c7;color:#92400e;border:1px solid #fde68a}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,"PingFang SC","Noto Sans CJK SC",sans-serif}
header{position:sticky;top:0;z-index:30;background:rgba(255,255,255,.96);backdrop-filter:blur(8px);border-bottom:1px solid #e7eaf0;padding:16px 22px} h1{font-size:18px;margin:0 0 8px}.sub{font-size:13px;color:var(--muted);line-height:1.5}.topbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:12px}.spacer{flex:1}
.tab{border:1px solid #cfd5df;background:white;border-radius:10px;padding:7px 12px;cursor:pointer;font-weight:750;color:#344054}.tab.active{color:white}.tab.vendor.active{background:var(--vendor);border-color:var(--vendor)}.tab.gems.active{background:var(--gems);border-color:var(--gems)}.tab.compare.active{background:var(--blue);border-color:var(--blue)}.tab.hotspots.active{background:#a15c00;border-color:#a15c00}.tab.unmatched.active{background:var(--purple);border-color:var(--purple)}.tab.drill.active{background:#0f766e;border-color:#0f766e}
.control{border:1px solid #cfd5df;background:white;color:#344054;border-radius:10px;padding:7px 10px;cursor:pointer;font:inherit;font-size:12px}.control:hover,.tab:hover{background:#f2f4f7}.tab.active:hover{filter:brightness(.98);color:white}.summary{font-size:12px;color:#475467;display:flex;gap:10px;flex-wrap:wrap}.summary b{color:#111827}
main.audit{display:grid;grid-template-columns:minmax(520px,1fr) 620px;gap:18px;padding:18px 22px 60px}.panel{background:white;border:1px solid #e4e7ec;border-radius:16px;box-shadow:0 2px 10px rgba(16,24,40,.04)}.treepanel{padding:14px}.detail{position:sticky;top:106px;align-self:start;max-height:calc(100vh - 126px);overflow:auto;padding:16px}.tree-head{font-size:12px;color:#667085;margin:0 0 10px 4px}
.node,.c-node{position:relative;margin:8px 0}.row,.c-row{display:flex;gap:8px;align-items:stretch}.children,.c-children{display:none;margin-left:31px;padding-left:18px;border-left:2px solid var(--line)}.node.expanded>.children,.c-node.expanded>.c-children{display:block}.caret,.c-caret{width:24px;height:24px;min-width:24px;margin-top:14px;border:1px solid #d0d5dd;border-radius:7px;background:white;color:#475467;cursor:pointer}.caret.empty,.c-caret.empty{visibility:hidden}.caret:before,.c-caret:before{content:"▸"}.node.expanded>.row .caret:before,.c-node.expanded>.c-row .c-caret:before{content:"▾"}.card,.pill{flex:1;min-width:0;border:1px solid #d8dde8;border-radius:14px;background:white;padding:10px 12px;cursor:pointer;box-shadow:0 1px 5px rgba(16,24,40,.035)}.card:hover,.pill:hover{box-shadow:0 6px 18px rgba(16,24,40,.08);border-color:#b8c0cc}.node.selected>.row .card{border-color:var(--blue);box-shadow:0 0 0 3px rgba(52,81,209,.12)}.c-node.selected>.c-row .pill{border-color:#0f766e;box-shadow:0 0 0 3px rgba(15,118,110,.16)}.title,.c-title{display:flex;justify-content:space-between;gap:10px;align-items:center}.name,.c-name{font-weight:760;font-size:13px}.muted{color:var(--muted);font-size:11px;white-space:nowrap}.path{font-size:11px;color:#7b8494;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.metrics,.bars{display:grid;grid-template-columns:58px 1fr 80px;gap:6px;align-items:center;font-size:11px;color:#475467;margin-top:8px}.track,.bar-track{height:8px;background:#eef1f6;border-radius:99px;overflow:hidden}.bar{height:100%;border-radius:99px;min-width:1px;background:#8ea0b8;display:block}.bar.vendor{background:var(--vendor)}.bar.gems{background:var(--gems)}.val,.value{text-align:right;font-variant-numeric:tabular-nums}.detail h2{font-size:18px;margin:0 0 4px}.small{font-size:12px;color:var(--muted);line-height:1.5;word-break:break-all}.metricgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:14px 0}.metric{background:#f8fafc;border:1px solid #e7eaf0;border-radius:13px;padding:10px}.metric .label{font-size:12px;color:var(--muted)}.metric .num{font-size:20px;font-weight:800;font-variant-numeric:tabular-nums;margin-top:3px}.tagrow{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.tag{font-size:11px;border:1px solid #d0d5dd;background:white;border-radius:999px;padding:3px 7px;color:#344054} table{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px} th,td{border-bottom:1px solid #eef1f6;padding:7px 6px;text-align:left;vertical-align:top} th{position:sticky;top:0;background:white;color:#475467;font-size:11px;text-transform:uppercase;letter-spacing:.02em} td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.kname{word-break:break-all;max-width:250px}.section{margin-top:18px}.section h3{margin:0 0 6px;font-size:14px}.emptydetail{color:var(--muted);padding:24px;text-align:center}
main.compare,main.unmatched{display:block;padding:22px 28px 60px} main.hotspots{display:grid;grid-template-columns:minmax(620px,1fr) 620px;gap:18px;padding:18px 22px 60px} main.drill{display:grid;grid-template-rows:auto 1fr;height:calc(100vh - 106px);padding:12px 14px 18px;gap:10px}.drill-head{background:white;border:1px solid #e4e7ec;border-radius:14px;padding:10px 12px;box-shadow:0 2px 10px rgba(16,24,40,.04)}.drill-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;min-height:0}.drill-pane{display:flex;flex-direction:column;min-height:0;background:white;border:1px solid #e4e7ec;border-radius:14px;overflow:hidden}.drill-pane h3{margin:0;padding:8px 10px;border-bottom:1px solid #e7eaf0;font-size:13px}.drill-pane iframe{border:0;width:100%;height:100%;min-height:0;background:#111827}.drill-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px}.drill-actions a{font-size:12px;color:#3451d1;text-decoration:none}.drill-actions a:hover{text-decoration:underline}.hot-card{background:white;border:1px solid #e4e7ec;border-radius:16px;padding:16px;box-shadow:0 2px 10px rgba(16,24,40,.04)}.hot-row{cursor:pointer}.hot-row:hover{background:#fff8e6}.hot-row.selected{background:#fff1cc}.stackbox{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;line-height:1.45;white-space:pre-wrap;background:#f8fafc;border:1px solid #e7eaf0;border-radius:10px;padding:8px;max-height:220px;overflow:auto}.shape-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.side-title{font-weight:800;margin:10px 0 4px}.sample-table td{font-size:11px}.compare-tree{max-width:1180px;margin:0 auto}.pill{min-width:330px;max-width:880px}.delta{font-size:12px;color:#475467;white-space:nowrap}.delta.pos{color:var(--gems);font-weight:760}.delta.neg{color:var(--vendor);font-weight:760}.depth-0>.c-row .pill{border-width:2px}.depth-0>.c-row .c-name{font-size:16px}.footer-note{max-width:1180px;margin:18px auto 0;color:#667085;font-size:12px;line-height:1.6}.unmatched-card{max-width:1180px;margin:0 auto;background:white;border:1px solid #e4e7ec;border-radius:16px;padding:18px}.status-vendor_only{background:rgba(217,75,75,.08)}.status-gems_only{background:rgba(35,155,86,.08)} .unmatched-actions{display:inline-flex;gap:8px;margin-left:8px}.unmatched-link{color:#7c3aed;text-decoration:underline;cursor:pointer}.unmatched-grid{max-width:1280px;margin:0 auto;display:grid;grid-template-columns:minmax(520px,1fr) 560px;gap:16px}.unmatched-list,.unmatched-detail{background:white;border:1px solid #e4e7ec;border-radius:16px;padding:16px;box-shadow:0 2px 10px rgba(16,24,40,.04)}.unmatched-item{border:1px solid #e7eaf0;border-radius:12px;padding:10px;margin:8px 0;cursor:pointer;background:#fff}.unmatched-item:hover{background:#faf7ff;border-color:#c4b5fd}.unmatched-item.selected{background:#f5f3ff;border-color:#7c3aed;box-shadow:0 0 0 3px rgba(124,58,237,.12)}.analysis-category{font-size:11px;font-weight:800;color:#6d28d9;background:#f5f3ff;border:1px solid #ddd6fe;border-radius:999px;padding:2px 7px}.pink-note{color:#be185d;font-weight:750}
@media(max-width:1050px){main.audit{grid-template-columns:1fr}.detail{position:relative;top:auto;max-height:none}}
main.perfetto-view{display:block;padding:0;height:calc(100vh - 106px);position:relative}.perfetto-container{position:absolute;top:0;bottom:0;left:0;right:0;overflow:hidden}.perfetto-frame{width:100%;height:100%;border:none}#perfFrameWrap{position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;overflow:hidden}
.tag{display:inline-block;border:1px solid #d0d5dd;background:#f8fafc;border-radius:999px;padding:2px 7px;margin:2px 4px 2px 0;font-size:11px;color:#344054}.tag.good{border-color:#9fd7b3;background:#eefaf2;color:#14532d}.tag.warn{border-color:#f4c477;background:#fff8e6;color:#7a4b00}.tag.bad{border-color:#f2a7a7;background:#fff1f1;color:#991b1b}.hot-toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0 12px}.hot-sort{border:1px solid #cfd5df;background:white;border-radius:9px;padding:5px 9px;cursor:pointer;font-size:12px}.hot-sort.active{background:#a15c00;color:white;border-color:#a15c00}.issue-title{font-weight:800}.issue-sub{font-size:11px;color:#667085;max-width:520px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.summary-box{background:#f8fafc;border:1px solid #e7eaf0;border-radius:13px;padding:10px;font-size:13px;line-height:1.55}.mix-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.mini-list{font-size:12px}.mini-list .rowline{display:flex;justify-content:space-between;gap:8px;border-bottom:1px solid #eef1f6;padding:5px 0}.mini-list .rowline span:first-child{word-break:break-all}.diff-section{border:1px solid #e7eaf0;border-radius:12px;padding:10px;margin-top:10px;background:white}.diff-section summary{cursor:pointer;font-weight:800;font-size:13px}.prefix{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#f8fafc;border:1px solid #e7eaf0;border-radius:10px;padding:8px;font-size:11px;line-height:1.45;white-space:pre-wrap;max-height:180px;overflow:auto}.evidence-cell{max-width:240px}.hot-row .path{max-width:560px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.delta-pos{color:#239b56;font-weight:800}.delta-neg{color:#d94b4b;font-weight:800}
</style>
</head>
<body>
<header>
  <h1>Gems vs Vendor Semantic Dashboard</h1>
  <div class="sub" id="subtitle">按 cpu-stack 语义树比较 subtree kernel execution time；不统计 gap / host time。</div>
  <div class="topbar">
    <button id="tabVendor" class="tab vendor active">Vendor</button>
    <button id="tabGems" class="tab gems">Gems</button>
    <button id="tabCompare" class="tab compare">Gems vs Vendor</button>
    <button id="tabDrill" class="tab drill">双栏定位</button>
    <button id="tabHotspots" class="tab hotspots">最大差距</button>
    <button id="tabUnmatched" class="tab unmatched">Unmatched</button>
    <button id="toggleFold" class="control">当前：模板平均</button>
    <button id="expandDepth" class="control">展开到 depth2</button>
    <button id="expandAll" class="control">展开全部</button>
    <button id="collapseAll" class="control">折叠全部</button>
    <span class="spacer"></span>
    <span id="summary" class="summary"></span>
  </div>
</header>
<main id="main" class="audit"><section class="panel treepanel"><div class="tree-head" id="treeHead"></div><div id="tree"></div></section><aside class="panel detail" id="detail"><div class="emptydetail">点击左侧语义节点查看 kernel 时间统计。</div></aside></main>
<script id="payload" type="text/plain">__DATA_B64__</script>
<script>
const payload=JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(document.getElementById('payload').textContent.trim()),c=>c.charCodeAt(0))));
let view='vendor'; let mode='folded'; let selectedId=null; let selectedComparePath=null; let selectedUnmatchedPath=null; let hotspotSort='ratio'; const openState={}; let compareScrollTop=0;
function fmt(ms){if(!isFinite(ms))return '∞'; if(Math.abs(ms)>=100)return ms.toFixed(1)+' ms'; if(Math.abs(ms)>=10)return ms.toFixed(2)+' ms'; if(Math.abs(ms)>=1)return ms.toFixed(3)+' ms'; return (ms*1000).toFixed(1)+' µs'}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function nodeTag(n){if(n.is_module)return '<span class="ntag mod">M</span>';if(n.is_source)return '<span class="ntag src">src</span>';return ''}
function flat(n,out=[]){out.push(n);(n.children||[]).forEach(c=>flat(c,out));return out}
function pct(v,max){return max?Math.max(1,Math.min(100,100*v/max)):0}
function auditRoot(){return payload[view][mode==='folded'?'audit_folded':'audit_exact']}
function setOpen(id,val){openState[view+'|'+mode+'|'+id]=val} function getOpen(id){return !!openState[view+'|'+mode+'|'+id]}
function renderAuditNode(n,depth=0,max=1){let has=(n.children||[]).length>0; let open=getOpen(n.path)||(depth<max&&has); let mx=Math.max(n.total_ms||0,...(n.children||[]).map(c=>c.total_ms||0)); let ks=(n.kernel_stats||[]).slice(0,3).map(x=>`${esc(x.name)}×${x.count}`).join(' · '); return `<div class="node ${open?'expanded':''} ${selectedId===n.path?'selected':''}" data-id="${esc(n.path)}"><div class="row"><button class="caret ${has?'':'empty'}"></button><div class="card"><div class="title"><span class="name">${nodeTag(n)}${esc(n.name)}</span><span class="muted">${n.kernel_count||0} kernels</span></div><div class="path">${esc(n.path||'')}</div><div class="metrics"><span>GPU</span><span class="track"><span class="bar" style="width:${pct(n.total_ms||0,mx)}%"></span></span><span class="val">${fmt(n.total_ms||0)}</span></div>${ks?`<div class="small">${ks}</div>`:''}</div></div>${has?`<div class="children">${n.children.map(c=>renderAuditNode(c,depth+1,max)).join('')}</div>`:''}</div>`}
function renderAudit(){parkPerfettoFrames();document.getElementById('main').className='audit';document.getElementById('main').innerHTML='<section class="panel treepanel"><div class="tree-head" id="treeHead"></div><div id="tree"></div></section><aside class="panel detail" id="detail"><div class="emptydetail">点击左侧语义节点查看 kernel 时间统计。</div></aside>'; let r=auditRoot(); document.getElementById('treeHead').textContent=`${payload[view].label} · ${mode==='folded'?'Folded':'Exact'} · kernel time only`; document.getElementById('tree').innerHTML=renderAuditNode(r,0,1); document.getElementById('summary').innerHTML=`<span><b>${payload[view].kernel_count}</b> kernels</span><span><b>${fmt(payload[view].kernel_ms)}</b> kernel time</span>`; bindAudit(); updateTabs();}
function bindAudit(){document.querySelectorAll('.node>.row .caret').forEach(b=>b.onclick=e=>{let n=e.target.closest('.node');setOpen(n.dataset.id,!n.classList.contains('expanded'));renderAudit();function renderUnmatched(){}});document.querySelectorAll('.node>.row .card').forEach(c=>c.onclick=e=>{let id=e.target.closest('.node').dataset.id;selectedId=id;let n=flat(auditRoot()).find(x=>x.path===id);renderAuditDetail(n);document.querySelectorAll('.node').forEach(x=>x.classList.toggle('selected',x.dataset.id===id));});}
function renderAuditDetail(n){if(!n)return;let rows=(n.kernel_stats||[]).map(k=>`<tr><td class="kname">${esc(k.name)}</td><td class="num">${k.count}</td></tr>`).join('');document.getElementById('detail').innerHTML=`<h2>${esc(n.name)}</h2><div class="small">${esc(n.path)}</div><div class="metricgrid"><div class="metric"><div class="label">Kernel time</div><div class="num">${fmt(n.total_ms||0)}</div></div><div class="metric"><div class="label">Kernel count</div><div class="num">${n.kernel_count||0}</div></div><div class="metric"><div class="label">Children</div><div class="num">${(n.children||[]).length}</div></div></div><div class="section"><h3>Kernel stats</h3><table><thead><tr><th>Name</th><th>Count</th></tr></thead><tbody>${rows}</tbody></table></div>`}
const traceCache={};const perfLoaded={vendor:false,gems:false};
function decodeTraceBuffer(side){if(traceCache[side])return traceCache[side];let b64=payload.side_trace_b64&&payload.side_trace_b64[side];if(!b64)return null;let bin=atob(b64);let buf=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)buf[i]=bin.charCodeAt(i);traceCache[side]=buf.buffer;return buf.buffer;}
function initPerfettoFor(iframeId,traceObj){let iframe=document.getElementById(iframeId);let origin='https://ui.perfetto.dev';let target=iframe.contentWindow;let timer=setInterval(()=>{target.postMessage('PING',origin);},50);function onMsg(evt){if(evt.source!==iframe.contentWindow)return;if(evt.origin!==origin)return;if(evt.data==='PONG'){clearInterval(timer);window.removeEventListener('message',onMsg);target.postMessage(traceObj,origin);}}window.addEventListener('message',onMsg);}
function renderSideView(){let side=view;let hasTrace=!!(payload.side_trace_b64&&payload.side_trace_b64[side]);if(!hasTrace){showMain();renderAudit();return;}hideMain();let label=side==='vendor'?'Vendor':'Gems';let containerId=side+'PerfettoContainer';let container=document.getElementById(containerId);if(!container){container=document.createElement('div');container.id=containerId;container.style.cssText='position:absolute;top:60px;bottom:0;left:0;right:0;overflow:hidden;display:none;';container.innerHTML='<iframe id="'+side+'-perfetto" style="width:100%;height:100%;border:none;" src="https://ui.perfetto.dev"></iframe>';document.body.appendChild(container);}document.getElementById('vendorPerfettoContainer')&&(document.getElementById('vendorPerfettoContainer').style.display='none');document.getElementById('gemsPerfettoContainer')&&(document.getElementById('gemsPerfettoContainer').style.display='none');container.style.display='';if(!perfLoaded[side]){let buf=decodeTraceBuffer(side);if(buf){let traceObj={perfetto:{buffer:buf,title:label+' Decode Window',fileName:side+'_decode_slice.json.gz'}};initPerfettoFor(side+'-perfetto',traceObj);perfLoaded[side]=true;}}document.getElementById('summary').innerHTML='<span>'+label+' decode window timeline</span>';updateTabs();}
function showMain(){document.getElementById('main').style.display='';document.getElementById('vendorPerfettoContainer')&&(document.getElementById('vendorPerfettoContainer').style.display='none');document.getElementById('gemsPerfettoContainer')&&(document.getElementById('gemsPerfettoContainer').style.display='none');}
function hideMain(){document.getElementById('main').style.display='none';}
function parkPerfettoFrames(){showMain();}
function compareRoot(){return payload.compare[mode==='folded'?'folded':'exact']}
function compareFlat(n=compareRoot(), path='root', out=[]){let p=path==='root'?n.key:(path+'/'+n.key); out.push([p,n]); (n.children||[]).forEach(c=>compareFlat(c,p,out)); return out;}
function findCompareNode(path){let item=compareFlat().find(([p,n])=>p===path);return item?item[1]:null;}
function renderCompareNode(n,depth=0,path='root'){let p=path==='root'?n.key:(path+'/'+n.key);let has=(n.children||[]).length>0; let delta=n.delta_ms||0; let cls=delta>0?'neg':delta<0?'pos':''; let max=Math.max(n.vendor_ms||0,n.gems_ms||0,0.0001); let one=(n.status==='gems_only'||n.status==='vendor_only'); let actions=one?`<span class="unmatched-actions"><span class="unmatched-link" data-action="drill">双栏定位</span></span>`:'点击打开双栏定位'; return `<div class="c-node depth-${depth} expanded ${selectedComparePath===p?'selected':''}" data-depth="${depth}" data-path="${esc(p)}"><div class="c-row"><button class="c-caret ${has?'':'empty'}"></button><div class="pill"><div class="c-title"><span class="c-name">${nodeTag(n)}${esc(n.name)}</span><span class="delta ${cls}">Δ ${fmt(delta)}</span></div><div class="bars"><span>Vendor</span><span class="bar-track"><span class="bar vendor" style="width:${pct(n.vendor_ms||0,max)}%"></span></span><span class="value">${fmt(n.vendor_ms||0)}</span><span>Gems</span><span class="bar-track"><span class="bar gems" style="width:${pct(n.gems_ms||0,max)}%"></span></span><span class="value">${fmt(n.gems_ms||0)}</span></div><div class="small">${n.vendor_count||0} / ${n.gems_count||0} kernels · ${esc(n.status||'')} · ${actions}</div></div></div>${has?`<div class="c-children">${n.children.map(c=>renderCompareNode(c,depth+1,p)).join('')}</div>`:''}</div>`}
function renderCompare(){parkPerfettoFrames();document.getElementById('main').className='compare';document.getElementById('main').innerHTML='<div id="compareTree" class="compare-tree"></div><div class="footer-note">说明：点击任意语义节点会打开 Gems / Vendor 双栏定位页；两边使用各自 cpu_stack exact HTML，并标黄该语义下的 kernel。</div>';document.getElementById('compareTree').innerHTML=renderCompareNode(compareRoot(),0);document.getElementById('summary').innerHTML=`<span>Vendor <b>${fmt(payload.vendor.kernel_ms)}</b></span><span>Gems <b>${fmt(payload.gems.kernel_ms)}</b></span><span>Δ <b>${fmt(payload.vendor.kernel_ms-payload.gems.kernel_ms)}</b></span>`;bindCompare();updateTabs();if(compareScrollTop>0){setTimeout(()=>{document.getElementById('main').scrollTop=compareScrollTop;window.scrollTo(0,compareScrollTop);},0);}}
function bindCompare(){document.querySelectorAll('.c-caret').forEach(b=>b.onclick=e=>{e.stopPropagation();let n=e.target.closest('.c-node');n.classList.toggle('expanded')});document.querySelectorAll('.unmatched-link').forEach(a=>a.onclick=e=>{e.stopPropagation();let path=e.target.closest('.c-node').dataset.path;selectedComparePath=path;renderDrill(findCompareNode(path));});document.querySelectorAll('.c-node>.c-row .pill').forEach(pill=>pill.onclick=e=>{let path=e.target.closest('.c-node').dataset.path;selectedComparePath=path;renderDrill(findCompareNode(path));});}
function hotspotRows(){return payload.hotspots[mode==='folded'?'folded':'exact']||[]}
function tagClass(t){if(t.includes('SAME'))return 'good'; if(t.includes('DIFF'))return t.includes('CPU')?'bad':'warn'; if(t.includes('AVG'))return 'good'; return ''}
function tagsHtml(tags){return (tags||[]).map(t=>`<span class="tag ${tagClass(t)}">${esc(t)}</span>`).join('') || '<span class="tag">NO_TAG</span>'}
function kernelMixList(items, side){if(!items||!items.length)return '<div class="small">无独有 kernel</div>';return '<div class="mini-list">'+items.slice(0,10).map(x=>`<div class="rowline"><span>${esc(x.name)}</span><b>${fmt(x[side==='Gems'?'gems_ms':'vendor_ms']||0)}</b></div>`).join('')+'</div>'}
function commonKernelMix(items){if(!items||!items.length)return '<div class="small">无共同 kernel family</div>';return '<div class="mini-list">'+items.slice(0,10).map(x=>`<div class="rowline"><span>${esc(x.name)}</span><span>G ${fmt(x.gems_ms||0)} · V ${fmt(x.vendor_ms||0)} · Δ ${fmt(x.delta_ms||0)}</span></div>`).join('')+'</div>'}
function shapeDiffHtml(d){if(!d)return '<div class="small">无 shape 信息</div>';let common=(d.common||[]).slice(0,8).map(x=>`<tr><td class="kname">${esc(x.shape)}</td><td class="num">${x.gems_count}</td><td class="num">${x.vendor_count}</td></tr>`).join('');let go=(d.gems_only||[]).slice(0,8).map(x=>`<tr><td class="kname">${esc(x.shape)}</td><td class="num">${x.count}</td></tr>`).join('');let vo=(d.vendor_only||[]).slice(0,8).map(x=>`<tr><td class="kname">${esc(x.shape)}</td><td class="num">${x.count}</td></tr>`).join('');return `<div class="small">Status: <b>${esc(d.status||'unknown')}</b></div><h4>Common</h4><table><thead><tr><th>Shape transition</th><th>Gems</th><th>Vendor</th></tr></thead><tbody>${common||'<tr><td colspan="3" class="small">无共同 shape</td></tr>'}</tbody></table><div class="mix-grid"><div><h4>Gems-only</h4><table><tbody>${go||'<tr><td class="small">无</td></tr>'}</tbody></table></div><div><h4>Vendor-only</h4><table><tbody>${vo||'<tr><td class="small">无</td></tr>'}</tbody></table></div></div>`}
function stackDiffHtml(d){if(!d)return '<div class="small">无 stack 信息</div>';let c=(d.common||[]).join('\n');let gs=(d.gems_suffix||[]).join('\n');let vs=(d.vendor_suffix||[]).join('\n');return `<div class="small">Status: <b>${esc(d.status||'unknown')}</b></div><h4>Common prefix</h4><div class="prefix">${esc(c||'(empty)')}</div><div class="mix-grid"><div><h4>Gems unique suffix</h4><div class="prefix">${esc(gs||'(empty)')}</div></div><div><h4>Vendor unique suffix</h4><div class="prefix">${esc(vs||'(empty)')}</div></div></div>`}
function childContributionHtml(items){if(!items||!items.length)return '<div class="small">该 hotspot 没有可比较子项，或差异主要在当前节点。</div>';return '<table><thead><tr><th>Child</th><th>G/V</th><th>Gems</th><th>Vendor</th><th>Δ</th></tr></thead><tbody>'+items.slice(0,12).map(x=>`<tr><td class="kname">${esc(x.name)}</td><td class="num">${x.ratio_gems_over_vendor?Number(x.ratio_gems_over_vendor).toFixed(3)+'×':'-'}</td><td class="num">${fmt(x.gems_ms||0)}</td><td class="num">${fmt(x.vendor_ms||0)}</td><td class="num">${fmt(x.delta_ms||0)}</td></tr>`).join('')+'</tbody></table>'}
function sampleRows(items, side){return (items||[]).slice(0,12).map(x=>`<tr><td>${side}</td><td class="kname">${esc(x.name)}</td><td class="num">${fmt(x.dur_ms||0)}</td><td class="kname">${esc(x.shape||'')}</td></tr>`).join('')}
function sideHtml(side){return (payload.side_html&&payload.side_html[side])||''}
function postDrillFocus(){let n=selectedComparePath?findCompareNode(selectedComparePath):null;if(!n)return;let gemsIds=n.gems_kernel_ids||[];let vendorIds=n.vendor_kernel_ids||[];let ctx=n.unmatched_context||{};if((!gemsIds.length)&&ctx.gems_context_kernel_ids)gemsIds=ctx.gems_context_kernel_ids;if((!vendorIds.length)&&ctx.vendor_context_kernel_ids)vendorIds=ctx.vendor_context_kernel_ids;let msgG={type:'focus-kernels',kernelIds:gemsIds,path:'',highlight:'unmatched-context'};let msgV={type:'focus-kernels',kernelIds:vendorIds,path:'',highlight:'unmatched-context'};let gf=document.getElementById('gemsFrame');let vf=document.getElementById('vendorFrame');try{if(gf&&gf.contentWindow)gf.contentWindow.postMessage(msgG,'*');}catch(e){}try{if(vf&&vf.contentWindow)vf.contentWindow.postMessage(msgV,'*');}catch(e){}}
function renderDrill(n){parkPerfettoFrames();compareScrollTop=document.getElementById('main').scrollTop||window.scrollY||0;view='drill';if(n&&!selectedComparePath){selectedComparePath=n.key||'root';}let title=n?`${esc(n.name)} · ${esc(n.status||'')}`:'未选择语义节点';let gv=n&&n.vendor_ms?((n.gems_ms||0)/(n.vendor_ms||1)).toFixed(3)+'×':'-';let gemsUrl=sideHtml('gems');let vendorUrl=sideHtml('vendor');let gemsIds=n?(n.gems_kernel_ids||[]):[];let vendorIds=n?(n.vendor_kernel_ids||[]):[];document.getElementById('main').className='drill';document.getElementById('main').innerHTML=`<section class="drill-head"><b>双栏定位：</b>${title}<div class="small">Gems ${fmt(n?n.gems_ms:0)} (${gemsIds.length} kernels) · Vendor ${fmt(n?n.vendor_ms:0)} (${vendorIds.length} kernels) · G/V ${gv}</div>${(n&&n.unmatched_context)?`<div class="small pink-note">Unmatched context: ${esc(n.unmatched_context.explanation||'missing side focuses nearby matched siblings')} Prev=${esc(n.unmatched_context.prev_matched_name||'-')} Next=${esc(n.unmatched_context.next_matched_name||'-')}</div>`:''}<div class="drill-actions"><button class="control" id="backCompare">返回比较树</button><button class="control" id="refocusFrames">重新定位</button><a href="${esc(gemsUrl)}" target="_blank">打开 Gems cpu_stack</a><a href="${esc(vendorUrl)}" target="_blank">打开 Vendor cpu_stack</a></div></section><section class="drill-grid"><div class="drill-pane"><h3>Gems</h3><iframe id="gemsFrame" src="${esc(gemsUrl)}"></iframe></div><div class="drill-pane"><h3>Vendor</h3><iframe id="vendorFrame" src="${esc(vendorUrl)}"></iframe></div></section>`;document.getElementById('backCompare').onclick=()=>{view='compare';renderCompare();};document.getElementById('refocusFrames').onclick=postDrillFocus;document.getElementById('gemsFrame').onload=()=>setTimeout(postDrillFocus,120);document.getElementById('vendorFrame').onload=()=>setTimeout(postDrillFocus,120);setTimeout(postDrillFocus,350);document.getElementById('summary').innerHTML=`<span>selected <b>${esc(n?n.name:'')}</b></span><span>G/V <b>${gv}</b></span>`;updateTabs();}
function renderHotspotDetail(h){if(!h){document.getElementById('hotDetail').innerHTML='<div class="emptydetail">点击左侧条目查看诊断摘要、kernel mix、shape diff 和 CPU anchor 分叉点。</div>';return;}let km=h.kernel_mix||{};let samples=sampleRows(h.gems_kernel_samples,'Gems')+sampleRows(h.vendor_kernel_samples,'Vendor');let deltaCls=(h.delta_gems_minus_vendor_ms||0)>=0?'delta-pos':'delta-neg';document.getElementById('hotDetail').innerHTML=`<h2>${esc(h.display_path||h.name)}</h2><div class="small">${esc(h.full_display_path||h.path)}</div><div class="tagrow">${tagsHtml(h.evidence_tags)}</div><div class="metricgrid"><div class="metric"><div class="label">Gems / Vendor</div><div class="num">${h.ratio_gems_over_vendor?Number(h.ratio_gems_over_vendor).toFixed(3):'-'}×</div></div><div class="metric"><div class="label">Gems time</div><div class="num">${fmt(h.gems_ms)}</div></div><div class="metric"><div class="label">Vendor time</div><div class="num">${fmt(h.vendor_ms)}</div></div></div><div class="summary-box"><b>诊断摘要</b><br>${esc(h.diagnosis||'')}<br><span class="small">Δ(Gems-Vendor) = <b class="${deltaCls}">${fmt(h.delta_gems_minus_vendor_ms)}</b> · kernels ${h.gems_count} / ${h.vendor_count}</span></div><div class="section"><h3>Kernel mix</h3><div class="mix-grid"><div><div class="side-title">Gems-only / dominant</div>${kernelMixList(km.gems_only,'Gems')}</div><div><div class="side-title">Vendor-only / dominant</div>${kernelMixList(km.vendor_only,'Vendor')}</div></div><details class="diff-section" open><summary>Common kernel families</summary>${commonKernelMix(km.common)}</details></div><details class="diff-section" ${((h.evidence_tags||[]).includes('SHAPE_DIFF'))?'open':''}><summary>Shape diff digest</summary>${shapeDiffHtml(h.shape_diff)}</details><details class="diff-section" ${((h.evidence_tags||[]).includes('CPU_ANCHOR_DIFF'))?'open':''}><summary>CPU anchor common prefix / divergence</summary>${stackDiffHtml(h.stack_diff)}</details><details class="diff-section" open><summary>Children contribution</summary>${childContributionHtml(h.children_contribution)}</details><details class="diff-section"><summary>Top kernel samples</summary><table class="sample-table"><thead><tr><th>Side</th><th>Kernel</th><th>Dur</th><th>Shape</th></tr></thead><tbody>${samples}</tbody></table></details>`}
function sortedHotspotRows(){let rows=[...(payload.hotspots[mode==='folded'?'folded':'exact']||[])];if(hotspotSort==='delta')rows.sort((a,b)=>(b.abs_delta_ms||0)-(a.abs_delta_ms||0));else if(hotspotSort==='gems')rows.sort((a,b)=>(b.gems_ms||0)-(a.gems_ms||0));else rows.sort((a,b)=>(b.ratio_gems_over_vendor||0)-(a.ratio_gems_over_vendor||0)||(b.abs_delta_ms||0)-(a.abs_delta_ms||0));return rows}
function renderHotspots(){parkPerfettoFrames();document.getElementById('main').className='hotspots';let rows=sortedHotspotRows();let totalDelta=rows.reduce((a,h)=>a+(h.delta_gems_minus_vendor_ms||0),0);let table=rows.slice(0,300).map((h,i)=>`<tr class="hot-row" data-i="${i}"><td class="num">${i+1}</td><td><div class="issue-title">${esc(h.display_path||h.path)}</div><div class="issue-sub">${esc(h.full_display_path||h.path)}</div></td><td class="num"><b>${h.ratio_gems_over_vendor?Number(h.ratio_gems_over_vendor).toFixed(3):'-'}×</b></td><td class="num">${fmt(h.gems_ms)}</td><td class="num">${fmt(h.vendor_ms)}</td><td class="num">${fmt(h.delta_gems_minus_vendor_ms)}</td><td class="evidence-cell">${tagsHtml(h.evidence_tags)}</td></tr>`).join('');document.getElementById('main').innerHTML=`<section class="hot-card"><h2>Gems / Vendor 最大差距</h2><div class="small">非重叠 hotspot frontier：祖先/子节点重复问题会合并成一个语义问题。默认按 timeGems / timeVendor 排序；指标只使用 subtree kernel duration sum。</div><div class="hot-toolbar"><button class="hot-sort ${hotspotSort==='ratio'?'active':''}" data-sort="ratio">按 G/V ratio</button><button class="hot-sort ${hotspotSort==='delta'?'active':''}" data-sort="delta">按绝对 Δ</button><button class="hot-sort ${hotspotSort==='gems'?'active':''}" data-sort="gems">按 Gems time</button></div><table><thead><tr><th>#</th><th>Semantic issue</th><th>G/V</th><th>Gems</th><th>Vendor</th><th>Δ</th><th>Evidence</th></tr></thead><tbody>${table}</tbody></table></section><aside class="hot-card detail" id="hotDetail"><div class="emptydetail">点击左侧条目查看诊断摘要、kernel mix、shape diff 和 CPU anchor 分叉点。</div></aside>`;document.querySelectorAll('.hot-sort').forEach(b=>b.onclick=()=>{hotspotSort=b.dataset.sort;renderHotspots();});document.querySelectorAll('.hot-row').forEach(r=>r.onclick=e=>{document.querySelectorAll('.hot-row').forEach(x=>x.classList.remove('selected'));r.classList.add('selected');renderHotspotDetail(rows[Number(r.dataset.i)]);});if(rows.length){document.querySelector('.hot-row').classList.add('selected');renderHotspotDetail(rows[0]);}document.getElementById('summary').innerHTML=`<span><b>${rows.length}</b> non-overlap hotspots</span><span>sort <b>${hotspotSort}</b></span>`;updateTabs();}
function normPathForMatch(x){return String(x||'').replace(/#seg\d+/g,'').replace(/@[^/]+/g,'').replace(/\[[0-9]+\]/g,'[*]');}
function pathTailTokens(x){return normPathForMatch(x).split('/').filter(Boolean).slice(-4).join('/');}
function analysisMatchesPath(a,path){if(!a||!path)return false;let cand=[a.path,a.representative_path].concat(a.duplicate_paths||[]).filter(Boolean);if(cand.includes(path))return true;let np=normPathForMatch(path);for(let c of cand){let nc=normPathForMatch(c);if(nc===np)return true;if(pathTailTokens(nc) && pathTailTokens(nc)===pathTailTokens(np))return true;}return false;}
function unmatchedAnalysisRows(){let ua=payload.unmatched_analysis||{};let analyses=(ua[mode==='folded'?'folded':'exact']||[]);if(!analyses.length&&Array.isArray(ua.contexts)){analyses=ua.contexts.filter(a=>a.mode===(mode==='folded'?'folded':'exact')).map(a=>Object.assign({category:'candidate_context',summary:'candidates-only: this context was prepared for unmatched LLM diagnosis but no API call was made.',evidence:[]},a));}let rows=analyses.map((a,i)=>{let ms=Number(a.node_time_ms||((a.node||{}).time_ms)||0);let status=a.status||'';return {path:a.path||a.representative_path||('analysis_'+i),name:a.node_name||((a.node||{}).name)||a.path||('analysis_'+i),status:status,gems_kernel_ns:status==='gems_only'?ms*1e6:0,vendor_kernel_ns:status==='vendor_only'?ms*1e6:0,gems_kernel_count:'',vendor_kernel_count:'',analysis:a};});rows.sort((a,b)=>{let ac=(b.analysis&&b.analysis.duplicate_count)||1;let bc=(a.analysis&&a.analysis.duplicate_count)||1;if(ac!==bc)return ac-bc;return Math.max(b.gems_kernel_ns||0,b.vendor_kernel_ns||0)-Math.max(a.gems_kernel_ns||0,a.vendor_kernel_ns||0);});return rows}
function unmatchedSummaryText(a){if(!a)return '未开启 --llm-unmatched-mode，尚无 LLM 诊断。';let s=a.summary||'';if(!s&&a.category==='error')s='LLM unmatched request failed.';return s||'无诊断摘要。'}
function renderUnmatchedDetail(r){let box=document.getElementById('unmatchedDetail');if(!r){box.innerHTML='<div class="emptydetail">当前没有 LLM unmatched 分析。请使用 --llm-unmatched-mode suggest/apply 重新生成。</div>';return;}let a=r.analysis||{};let ev=(a.evidence||[]).map(x=>`<li>${esc(x)}</li>`).join('');let cps=(a.possible_counterparts||[]).map(c=>`<tr><td>${esc(c.candidate_id)}</td><td>${esc(c.status)}</td><td>${esc(c.name)}</td><td class="num">${esc(c.sibling_distance)}</td></tr>`).join('');let als=(a.alias_candidates||[]).map(c=>`<tr><td>${esc(c.candidate_id)}</td><td class="num">${esc(c.confidence)}</td><td>${esc(c.canonical_name||'')}</td><td>${esc(c.reason||'')}</td></tr>`).join('');let dup=a.duplicate_count||1;box.innerHTML=`<h2>${esc(a.node_name||r.name||r.path)}</h2><div class="small path">${esc(a.representative_path||a.path||r.path)}</div><div class="tagrow"><span class="tag ${r.status==='gems_only'?'good':'bad'}">${esc(r.status)}</span><span class="analysis-category">${esc(a.category||'not_analyzed')}</span><span class="tag">×${esc(dup)} structural occurrence(s)</span></div><div class="metricgrid"><div class="metric"><div class="label">Gems</div><div class="num">${fmt((r.gems_kernel_ns||0)/1e6)}</div></div><div class="metric"><div class="label">Vendor</div><div class="num">${fmt((r.vendor_kernel_ns||0)/1e6)}</div></div><div class="metric"><div class="label">Stage</div><div class="num">${esc((payload.unmatched_analysis||{}).stage||'-')}</div></div></div><div class="summary-box"><b>可能原因</b><br>${esc(unmatchedSummaryText(a))}<br><span class="small">risk: ${esc(a.risk||'')}<br>representative: ${esc(a.representative_mode||a.mode||'')}/${esc(a.representative_path||a.path||'')}</span></div><div class="section"><h3>上下文</h3><div class="small">Prev matched: ${esc((a.prev_matched||{}).name||'-')}<br>Next matched: ${esc((a.next_matched||{}).name||'-')}</div></div><details class="diff-section" open><summary>Evidence</summary><ul>${ev||'<li>无</li>'}</ul></details><details class="diff-section" open><summary>Possible counterparts considered by LLM</summary><table><thead><tr><th>ID</th><th>Status</th><th>Name</th><th>Dist</th></tr></thead><tbody>${cps||'<tr><td colspan="4" class="small">无候选</td></tr>'}</tbody></table></details><details class="diff-section" ${a.should_alias_review?'open':''}><summary>Secondary alias-review requested by unmatched analysis</summary><div class="small">should_alias_review=${!!a.should_alias_review}</div><table><thead><tr><th>ID</th><th>Conf</th><th>Canonical</th><th>Reason</th></tr></thead><tbody>${als||'<tr><td colspan="4" class="small">无</td></tr>'}</tbody></table></details><div class="section"><button class="control" id="unmatchedDrillBtn">打开双栏定位</button></div>`;let b=document.getElementById('unmatchedDrillBtn');if(b)b.onclick=()=>{selectedComparePath=a.path||r.path;renderDrill(findCompareNode(selectedComparePath));}}
function renderUnmatched(preselectPath=null){parkPerfettoFrames();view='unmatched';if(preselectPath)selectedUnmatchedPath=preselectPath;let rows=unmatchedAnalysisRows();let selectedIdx=Math.max(0, rows.findIndex(r=>analysisMatchesPath(r.analysis,selectedUnmatchedPath)));let selected=rows[selectedIdx];let list=rows.map((r,i)=>{let a=r.analysis||{};let selectedCls=i===selectedIdx?'selected':'';return `<div class="unmatched-item ${selectedCls}" data-i="${i}"><div><b>${esc(a.node_name||r.name)}</b> <span class="analysis-category">${esc(a.category||'not_analyzed')}</span></div><div class="small">${esc(r.status)} · ${esc(a.representative_mode||a.mode||'')} · ×${esc(a.duplicate_count||1)} · ${fmt(Number(a.node_time_ms||0))}</div><div class="small">${esc(unmatchedSummaryText(a)).slice(0,240)}</div></div>`}).join('');document.getElementById('main').className='unmatched';document.getElementById('main').innerHTML=`<div class="unmatched-grid"><section class="unmatched-list"><h2>Unmatched 语义诊断</h2><div class="small">这里只列 LLM 去重后的 unmatched 语义分析，不再平铺所有 one-sided 算子。点击 Gems vs Vendor 里的 Unmatched解释 会定位到对应分析。</div>${list||'<div class="emptydetail">无 LLM unmatched 分析。请使用 --llm-unmatched-mode suggest/apply。</div>'}</section><aside class="unmatched-detail" id="unmatchedDetail"></aside></div>`;document.querySelectorAll('.unmatched-item').forEach(el=>el.onclick=()=>{document.querySelectorAll('.unmatched-item').forEach(x=>x.classList.remove('selected'));el.classList.add('selected');let r=rows[Number(el.dataset.i)];selectedUnmatchedPath=(r.analysis||{}).path||r.path;renderUnmatchedDetail(r);});renderUnmatchedDetail(selected);setTimeout(()=>{let el=document.querySelector('.unmatched-item.selected');if(el)el.scrollIntoView({block:'center'});},0);document.getElementById('summary').innerHTML=`<span><b>${rows.length}</b> analyzed unmatched groups</span><span>raw one-sided <b>${payload.unmatched.length}</b></span><span>stage <b>${esc((payload.unmatched_analysis||{}).stage||'off')}</b></span>`;updateTabs();}
function updateTabs(){for(const [id,v] of [['tabVendor','vendor'],['tabGems','gems'],['tabCompare','compare'],['tabDrill','drill'],['tabHotspots','hotspots'],['tabUnmatched','unmatched']]){let el=document.getElementById(id); if(el)el.classList.toggle('active',view===v);}document.getElementById('toggleFold').textContent=mode==='folded'?'当前：模板平均':'当前：Exact Layers'}
function setDepth(d){if(view==='compare'){document.querySelectorAll('.c-node').forEach(x=>x.classList.toggle('expanded',Number(x.dataset.depth||0)<d));}else if(view==='hotspots'||view==='unmatched'||view==='drill'){}else{flat(auditRoot()).forEach(n=>setOpen(n.path,(n.path.split('/').length-1)<d));renderAudit();}}
document.getElementById('tabVendor').onclick=()=>{view='vendor';renderSideView();};document.getElementById('tabGems').onclick=()=>{view='gems';renderSideView();};document.getElementById('tabCompare').onclick=()=>{view='compare';renderCompare();};document.getElementById('tabDrill').onclick=()=>{if(selectedComparePath)renderDrill(findCompareNode(selectedComparePath));else{view='compare';renderCompare();}};document.getElementById('tabHotspots').onclick=()=>{view='hotspots';renderHotspots();};document.getElementById('toggleFold').onclick=()=>{mode=mode==='folded'?'exact':'folded'; if(view==='compare')renderCompare(); else if(view==='hotspots')renderHotspots();  else if(view==='drill')renderDrill(findCompareNode(selectedComparePath)); else renderSideView();};document.getElementById('expandDepth').onclick=()=>setDepth(2);document.getElementById('expandAll').onclick=()=>{if(view==='compare')document.querySelectorAll('.c-node').forEach(x=>x.classList.add('expanded')); else if(view==='hotspots'||view==='unmatched'||view==='drill'){} else {flat(auditRoot()).forEach(n=>setOpen(n.path,true));renderAudit();}};document.getElementById('collapseAll').onclick=()=>{if(view==='compare')document.querySelectorAll('.c-node').forEach(x=>x.classList.remove('expanded')); else if(view==='hotspots'||view==='unmatched'||view==='drill'){} else {flat(auditRoot()).forEach(n=>setOpen(n.path,false));renderAudit();}};
renderSideView();
</script></body></html>'''



# ===========================================================================
# Main entry point
# ===========================================================================

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Clean compare pipeline: module-path matching + v19 dashboard (no LLM)."
    )
    ap.add_argument("--gems", required=True, help="Gems final*.cpu_stack.normalized_tree.json")
    ap.add_argument("--vendor", required=True, help="Vendor final*.cpu_stack.normalized_tree.json")
    ap.add_argument("--out-prefix", required=True, help="Output prefix, e.g. ./compare/qwen3_30b_")
    ap.add_argument("--gems-html", default="", help="Path to Gems cpu_stack HTML for drill page")
    ap.add_argument("--vendor-html", default="", help="Path to Vendor cpu_stack HTML for drill page")
    ap.add_argument("--override", default="", help="Optional manual override JSON (module name mapping)")
    ap.add_argument("--max-depth", type=int, default=6, help="Max depth for console tree print")
    args = ap.parse_args()

    gems_json = load_json(args.gems)
    vendor_json = load_json(args.vendor)
    override = load_json(args.override) if args.override else {}
    prefix = args.out_prefix
    Path(prefix).parent.mkdir(parents=True, exist_ok=True)

    # Build payload
    payload = build_payload(
        vendor_json, gems_json,
        override=override,
        gems_html=args.gems_html,
        vendor_html=args.vendor_html,
    )

    # Print derived aliases
    for mode in ["folded", "exact"]:
        derived = payload["derived_aliases"].get(mode, {})
        if derived:
            print(f"[+] derived aliases ({mode}): {derived}")

    # Print compare summary
    rows: List[Dict[str, Any]] = []
    flatten_compare_tree(payload["compare"]["exact"], [], rows)
    rows.sort(key=lambda r: (r["status"] != "both", -abs(int(r["delta_ns"])), r["path"]))
    matched = sum(1 for r in rows if r["status"] == "both")
    one_sided = len(rows) - matched

    folded_rows: List[Dict[str, Any]] = []
    flatten_compare_tree(payload["compare"]["folded"], [], folded_rows)
    folded_matched = sum(1 for r in folded_rows if r["status"] == "both")
    folded_one_sided = len(folded_rows) - folded_matched

    # Write CSV outputs
    write_csv(prefix + "compare_nodes.csv", rows)
    write_csv(prefix + "compare_templates.csv", folded_rows)

    # Write hotspots CSV
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
    write_csv(prefix + "compare_hotspots.csv", hotspot_rows)

    # Write unmatched JSON
    write_json(prefix + "compare_unmatched.json", {"unmatched": payload["unmatched"]})

    # Generate HTML dashboard
    safe_payload = sanitize_for_json(payload)
    b64 = base64.b64encode(
        json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).decode("ascii")
    html = HTML.replace("__DATA_B64__", b64)
    out_html = prefix + "semantic_dashboard.html"
    Path(out_html).write_text(html, encoding="utf-8")

    # Print summary
    print(f"[+] wrote {prefix}compare_nodes.csv")
    print(f"[+] wrote {prefix}compare_templates.csv")
    print(f"[+] wrote {prefix}compare_hotspots.csv")
    print(f"[+] wrote {prefix}compare_unmatched.json")
    print(f"[+] wrote {out_html}")
    print(f"[*] exact:  projected_nodes={len(rows)} matched={matched} one_sided={one_sided}")
    print(f"[*] folded: projected_nodes={len(folded_rows)} matched={folded_matched} one_sided={folded_one_sided}")
    print(f"[*] vendor kernel time: {payload['vendor']['kernel_ms']:.3f} ms ({payload['vendor']['kernel_count']} kernels)")
    print(f"[*] gems kernel time:   {payload['gems']['kernel_ms']:.3f} ms ({payload['gems']['kernel_count']} kernels)")

    # Print top hotspots
    hotspots = payload["hotspots"]["folded"][:10]
    if hotspots:
        print(f"\n[*] Top hotspots (folded, by G/V ratio):")
        print(f"    {'Semantic path':<50} {'G/V':>8} {'Gems ms':>10} {'Vendor ms':>10} {'Delta ms':>10}")
        for h in hotspots:
            ratio = h.get("ratio_gems_over_vendor")
            ratio_s = f"{ratio:.3f}x" if ratio else "-"
            print(f"    {h['display_path']:<50} {ratio_s:>8} {h['gems_ms']:>10.3f} {h['vendor_ms']:>10.3f} {h['delta_gems_minus_vendor_ms']:>10.3f}")


if __name__ == "__main__":
    main()
