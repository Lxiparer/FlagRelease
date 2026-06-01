#!/usr/bin/env python3
"""render_ascend_tree.py — render normalized Ascend decode tree JSON to HTML.

This file is intentionally standalone: probe_ascend_db.py writes structured
JSON, and this renderer turns that JSON into an interactive HTML view.  Other
analysis/rendering tools can consume the same JSON without depending on HTML.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional


def _format_ns(ns: Any) -> str:
    try:
        ns_i = int(ns)
    except Exception:
        return "?"
    if ns_i >= 1_000_000:
        return f"{ns_i / 1_000_000:.2f} ms"
    if ns_i >= 1_000:
        return f"{ns_i / 1_000:.1f} µs"
    return f"{ns_i} ns"


def _clean_shape_string(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s in {"", "N/A", "NULL", "None"}:
        return ""
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s.strip()


def _shape_tensors(shape_value: Any, *, max_items: int = 2) -> List[str]:
    """Parse CANN semicolon-separated tensor shape string into display tokens."""
    s = _clean_shape_string(shape_value)
    if not s:
        return []
    out: List[str] = []
    for part in s.split(";"):
        p = part.strip()
        if not p:
            continue
        # Scalar attrs sometimes appear as a single integer. Treat them as a
        # tensor-like value because this is a profiler display, not a type checker.
        out.append(f"[{p}]")
        if len(out) >= max_items:
            break
    total_nonempty = sum(1 for p in s.split(";") if p.strip())
    if total_nonempty > max_items:
        out.append(f"+{total_nonempty - max_items}")
    return out


def _kernel_shape_summary(k: Dict[str, Any]) -> str:
    shape = k.get("shape") or {}
    if shape.get("comm_size_bytes") is not None:
        return str(shape.get("summary", ""))
    name = str(k.get("name", ""))
    ins = _shape_tensors(shape.get("input_shapes"), max_items=2)
    outs = _shape_tensors(shape.get("output_shapes"), max_items=2)
    if not ins and not outs:
        return str(shape.get("summary", ""))
    if "Matmul" in name or "MatMul" in name:
        left = "×".join(ins) if ins else "?"
    else:
        left = "+".join(ins) if ins else "?"
    right = "+".join(outs) if outs else "?"
    return f"{left}→{right}"


def _kernel_shape_title(shape: Dict[str, Any]) -> str:
    if not shape:
        return ""
    lines: List[str] = []
    for label, key in [
        ("source", "source"),
        ("input_shapes", "input_shapes"),
        ("output_shapes", "output_shapes"),
        ("input_dtypes", "input_dtypes"),
        ("output_dtypes", "output_dtypes"),
        ("input_formats", "input_formats"),
        ("output_formats", "output_formats"),
        ("blockDim", "block_dim"),
        ("mixBlockDim", "mix_block_dim"),
        ("comm_size_bytes", "comm_size_bytes"),
        ("comm_data_type", "comm_data_type"),
        ("comm_bandwidth", "comm_bandwidth"),
        ("attr_info", "attr_info"),
    ]:
        if key in shape and shape.get(key) not in ("", None):
            lines.append(f"{label}: {shape.get(key)}")
    return "\n".join(lines)


def _strip_repeat_suffix(name: str, strip_re: re.Pattern[str]) -> str:
    """Normalize a rendered node label for repeat grouping."""
    return strip_re.sub("", str(name or ""))


def _direct_kernel_signature(node: Dict[str, Any]) -> str:
    """Signature of kernels directly attached to a node.

    This is intentionally based on kernel names/counts only, not duration/shape.
    Duration/shape can vary per layer without changing the semantic topology,
    but an extra direct kernel such as a layer-0-only RmsNorm must prevent the
    whole DecoderLayer run from being represented by layer 0.
    """
    counts: Dict[str, int] = {}
    for k in node.get("kernels", []) or []:
        name = str(k.get("name", "?"))
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return ""
    return ",".join(f"{k}:{v}" for k, v in sorted(counts.items()))


def _direct_child_layout_signature(node: Dict[str, Any], strip_re: re.Pattern[str]) -> str:
    """Immediate child layout used for conservative repeated folding.

    We deliberately do NOT recurse here.  A DecoderLayer should not be split
    just because its Attention implementation has slightly different internal
    helper/wrapper nodes; those children are folded independently.  But an
    extra direct child module/helper on only one occurrence must prevent that
    occurrence from representing the whole run.
    """
    parts: List[str] = []
    for c in node.get("children", []) or []:
        kind = "MODULE" if str(c.get("name", "")).startswith("nn.Module: ") else str(c.get("category", "node"))
        base = _strip_repeat_suffix(c.get("display_name", ""), strip_re)
        parts.append(f"{kind}:{base}")
    return ",".join(parts)


def _structure_signature(node: Dict[str, Any], strip_re: re.Pattern[str]) -> str:
    """Conservative HTML-only signature for repeated-node folding.

    Earlier versions folded module siblings solely by module base name.  That
    made MiniMaxM2DecoderLayer_0's special direct RmsNorm appear under
    `MiniMaxM2DecoderLayer ×62`, even though raw JSON correctly had RmsNorm
    only in layer 0.

    A repeated group is now formed only when the immediate visible topology is
    compatible:
      * same normalized display name;
      * same direct self-kernel multiset;
      * same direct child layout.

    Child internals are not part of the parent's signature; children are folded
    independently.  This keeps normal repeated DecoderLayer blocks readable but
    prevents a layer-0-only direct kernel/helper from being shown as if it
    existed in every layer.
    """
    base = _strip_repeat_suffix(node.get("display_name", ""), strip_re)
    kind = "MODULE" if str(node.get("name", "")).startswith("nn.Module: ") else str(node.get("category", "node"))
    self_sig = _direct_kernel_signature(node)
    child_layout = _direct_child_layout_signature(node, strip_re)
    return f"{kind}:{base}|self={self_sig}|children=({child_layout})"


def _representative_signature(node: Dict[str, Any], strip_re: re.Pattern[str]) -> str:
    """Fuller signature used only to choose a representative inside a fold.

    The grouping signature intentionally ignores child internals so that a
    repeated DecoderLayer block remains readable.  But once a run is folded, we
    should not blindly use the first occurrence as the display representative:
    layer 0 is often special.  This signature includes descendant direct-kernel
    and child-layout information, so we can choose the modal occurrence as the
    representative while still aggregating the whole run.
    """
    base = _strip_repeat_suffix(node.get("display_name", ""), strip_re)
    kind = "MODULE" if str(node.get("name", "")).startswith("nn.Module: ") else str(node.get("category", "node"))
    self_sig = _direct_kernel_signature(node)
    child_sigs = tuple(_representative_signature(c, strip_re) for c in node.get("children", []) or [])
    return f"{kind}:{base}|self={self_sig}|children=({','.join(child_sigs)})"


def _choose_representative_index(children: List[Dict[str, Any]], start: int, end: int,
                                 strip_re: re.Pattern[str]) -> int:
    """Choose the modal occurrence as display representative for a folded run.

    If layer 0 has a unique warmup/prelude operator but layers 1..N share the
    real steady-state structure, the old code picked layer 0 because it was the
    first child in the run.  This function picks the most frequent full
    representative signature; ties are resolved by the earliest occurrence of
    that modal signature.
    """
    counts: Dict[str, int] = {}
    first_idx: Dict[str, int] = {}
    for idx in range(start, end):
        sig = _representative_signature(children[idx], strip_re)
        counts[sig] = counts.get(sig, 0) + 1
        first_idx.setdefault(sig, idx)
    best_sig = max(counts.keys(), key=lambda sig: (counts[sig], -first_idx[sig]))
    return first_idx[best_sig]


def _add_merged_totals(rep: Dict[str, Any], other: Dict[str, Any]) -> None:
    rep["total_kernel_count"] = rep.get("total_kernel_count", 0) + other.get("total_kernel_count", 0)
    rep["total_gpu_ns"] = rep.get("total_gpu_ns", 0) + other.get("total_gpu_ns", 0)
    rep["self_kernel_count"] = rep.get("self_kernel_count", 0) + other.get("self_kernel_count", 0)
    rep["self_gpu_ns"] = rep.get("self_gpu_ns", 0) + other.get("self_gpu_ns", 0)
    rep_first = rep.get("first_ts")
    other_first = other.get("first_ts")
    if other_first is not None and (rep_first is None or other_first < rep_first):
        rep["first_ts"] = other_first
    rep_last = rep.get("last_ts")
    other_last = other.get("last_ts")
    if other_last is not None and (rep_last is None or other_last > rep_last):
        rep["last_ts"] = other_last


def _merge_semantic_shape(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not a:
        return b
    if not b:
        return a
    # Repeated layers normally have identical semantic shapes. Keep the readable
    # badge when they match; otherwise mark it as mixed rather than inventing one.
    if a.get("summary") == b.get("summary"):
        return a
    return {"summary": "mixed", "source": "repeat-merge"}


def _merge_repeated_nodes(node: Dict[str, Any], min_repeat: int, strip_re: re.Pattern[str]) -> None:
    for child in node.get("children", []):
        _merge_repeated_nodes(child, min_repeat, strip_re)
    children = node.get("children", [])
    if len(children) < min_repeat:
        return
    sigs = [_structure_signature(c, strip_re) for c in children]
    new_children: List[Dict[str, Any]] = []
    i = 0
    while i < len(children):
        sig = sigs[i]
        j = i + 1
        while j < len(children) and sigs[j] == sig:
            j += 1
        run_length = j - i
        if run_length >= min_repeat:
            rep_idx = _choose_representative_index(children, i, j, strip_re)
            rep = copy.deepcopy(children[rep_idx])
            rep["repeat_count"] = run_length
            rep["repeat_representative"] = children[rep_idx].get("display_name", children[rep_idx].get("name", ""))
            # Record how many distinct full structures were merged into this
            # visual group.  The group key may intentionally ignore descendant
            # details for readability; this tells the reader when the display
            # representative is modal rather than exact for every occurrence.
            variant_counts: Dict[str, int] = {}
            for k in range(i, j):
                vsig = _representative_signature(children[k], strip_re)
                variant_counts[vsig] = variant_counts.get(vsig, 0) + 1
            rep["repeat_variant_count"] = len(variant_counts)

            # Reset aggregate fields to zero, then add all occurrences including
            # the chosen representative exactly once.
            rep["total_kernel_count"] = 0
            rep["total_gpu_ns"] = 0
            rep["self_kernel_count"] = 0
            rep["self_gpu_ns"] = 0
            rep["first_ts"] = None
            rep["last_ts"] = None
            sem_shape = None
            for k in range(i, j):
                other = children[k]
                _add_merged_totals(rep, other)
                sem_shape = _merge_semantic_shape(sem_shape, other.get("semantic_shape"))
            if sem_shape:
                rep["semantic_shape"] = sem_shape
            base = strip_re.sub("", rep.get("display_name", ""))
            rep["display_name"] = f"{base} ×{run_length}"
            new_children.append(rep)
        else:
            new_children.extend(children[i:j])
        i = j
    node["children"] = new_children


def _convert_node_for_render(node: Dict[str, Any]) -> Dict[str, Any]:
    is_module = node.get("category") == "module"
    name = node.get("name", "")
    return {
        "name": f"nn.Module: {name}" if is_module else name,
        "display_name": name,
        "is_anchor": node.get("category") in ("phase", "root"),
        "anchor_reason": "",
        "self_kernel_count": node.get("self_kernel_count", 0),
        "self_gpu_ns": node.get("self_gpu_sum_ns", 0),
        "total_kernel_count": node.get("total_kernel_count", 0),
        "total_gpu_ns": node.get("total_gpu_sum_ns", 0),
        "semantic_shape": node.get("semantic_shape"),
        "first_ts": node.get("first_ts"),
        "last_ts": node.get("last_ts"),
        "children": [_convert_node_for_render(c) for c in node.get("children", [])],
        "kernels": node.get("kernels", []),
    }


def _node_classes(node: Dict[str, Any]) -> str:
    classes = ["name"]
    if str(node.get("name", "")).startswith("nn.Module: "):
        classes.append("module")
    elif node.get("is_anchor") and str(node.get("anchor_reason", "")).startswith("anchor_"):
        classes.append("anchor")
    if node.get("self_kernel_count", 0) > 0 and not node.get("children"):
        classes.append("kernel-leaf")
    return " ".join(classes)


def render_html(tree_json: Dict[str, Any], *, merge_repeated_min: int = 3,
                trailing_digit_strip_regex: str = r"_[0-9]+$",
                title: Optional[str] = None) -> str:
    root = _convert_node_for_render(tree_json["root"])
    _merge_repeated_nodes(root, merge_repeated_min, re.compile(trailing_digit_strip_regex))

    window = tree_json.get("window", {})
    stats = tree_json.get("stats", {})
    marker = tree_json.get("marker", {})
    total_gpu_ns = root.get("total_gpu_ns", 1) or 1
    title = title or f"Decode Tree — decode #{tree_json.get('decode_idx', '?')}"

    parts: List[str] = []
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'SF Mono','Fira Code','Consolas',monospace; font-size:13px; line-height:1.5; background:#1a1a2e; color:#e0e0e0; padding:20px; }}
.header {{ margin-bottom:20px; padding:12px 16px; background:#16213e; border-radius:8px; border:1px solid #0f3460; }}
.header h1 {{ font-size:16px; color:#e94560; margin-bottom:8px; }}
.header .meta {{ font-size:12px; color:#888; }} .header .meta span {{ margin-right:16px; }}
.search-box {{ margin-bottom:16px; }}
.search-box input {{ width:100%; max-width:680px; padding:6px 12px; background:#16213e; border:1px solid #0f3460; border-radius:4px; color:#e0e0e0; font-family:inherit; font-size:13px; }}
.search-box input:focus {{ outline:none; border-color:#e94560; }}
.tree {{ padding-left:0; }} .tree ul {{ list-style:none; padding-left:20px; }} .tree li {{ position:relative; }}
.tree li::before {{ content:''; position:absolute; left:-14px; top:0; height:100%; width:1px; background:#333; }}
.tree li:last-child::before {{ height:12px; }} .tree li::after {{ content:''; position:absolute; left:-14px; top:12px; width:10px; height:1px; background:#333; }}
.node {{ display:inline-flex; align-items:center; gap:6px; padding:2px 8px; margin:1px 0; border-radius:4px; cursor:pointer; transition:background .15s; }}
.node:hover {{ background:#16213e; }} .node.highlight {{ background:#3d1f00; border:1px solid #e94560; }} .node.focus-node {{ background:#263a10; border:1px solid #ffd54f; }}
.toggle {{ display:inline-block; width:14px; text-align:center; color:#666; font-size:11px; user-select:none; }} .toggle.has-children {{ color:#e94560; cursor:pointer; }}
.name {{ color:#e0e0e0; }} .name.module {{ color:#4fc3f7; }} .name.anchor {{ color:#ffd54f; }} .name.kernel-leaf {{ color:#81c784; }}
.badge {{ font-size:11px; padding:0 5px; border-radius:3px; color:#aaa; }}
.badge.kernels {{ background:#1b3a1b; color:#81c784; }} .badge.gpu-time {{ background:#1b2a3a; color:#4fc3f7; }} .badge.pct {{ background:#3a2a1b; color:#ffd54f; }} .badge.repeat {{ background:#3a1b3a; color:#ce93d8; font-weight:bold; }}
.badge.semantic-shape {{ background:#162b36; color:#80deea; border:1px solid #24536a; }}
.ordered-events {{ list-style:none; padding-left:20px; }}
.kernel-inline {{ display:inline-flex; align-items:center; gap:6px; padding:1px 8px; margin:1px 0; background:#0d1117; border-radius:4px; font-size:11px; white-space:nowrap; }}
.kernel-inline:hover {{ background:#101722; }}
.kernel-inline.highlight {{ background:#3d1f00; border:1px solid #e94560; }} .kernel-inline.focus-kernel {{ background:#473600; border:1px solid #ffd54f; box-shadow:0 0 0 1px rgba(255,213,79,.25) inset; }}
.kernel-inline .kname {{ color:#81c784; }} .kernel-inline .kdur {{ color:#777; margin-left:2px; }}
.kernel-inline .kshape {{ color:#ce93d8; margin-left:4px; background:#22152f; border:1px solid #3a1b3a; border-radius:3px; padding:0 4px; }}
.kernel-inline .kmeta {{ color:#6fa8dc; margin-left:2px; }} .kernel-inline .stream {{ color:#666; }}
.collapsed > ul {{ display:none; }}
</style>
</head>
<body>
<div class="header"><h1>{escape(title)}</h1><div class="meta">
<span>Window: {escape(str(window.get('duration_ms', '?')))} ms</span>
<span>Kernels: {escape(str(stats.get('gpu_kernel_count', '?')))}</span>
<span>Tree nodes: {escape(str(stats.get('final_tree_nodes', '?')))}</span>
<span>Marker: {escape(str(marker.get('marker_name', window.get('marker_name', '?'))))}</span>
</div></div>
<div class="search-box"><input type="text" id="search" placeholder="Search nodes / kernels / shapes..."></div>
<div class="tree" id="tree-root">
""")

    def render_kernel_item(k: Dict[str, Any]) -> None:
        """Render a direct kernel as a timeline-ordered tree item."""
        kname = escape(str(k.get("name", "?")))
        kdur = _format_ns(k.get("dur_ns", 0))
        shape = k.get("shape") or {}
        summary = _kernel_shape_summary(k)
        title = _kernel_shape_title(shape)
        is_comm = bool(k.get("is_communication"))
        original_dur = k.get("original_dur_ns", k.get("dur_ns", 0))
        if is_comm:
            extra = f"communication kernel; original duration: {_format_ns(original_dur)}; reason: {k.get('communication_reason','')}"
            title = (title + "\n" + extra) if title else extra
        search_blob = (str(k.get("name", "")) + " " + summary + (" communication" if is_comm else "")).lower()
        gpu_id = escape(str(k.get("gpu_id", "")))
        sem_path = escape(str(k.get("semantic_path", "")))
        parts.append(f'<li class="kernel-li"><div class="kernel-inline" data-name="{escape(search_blob)}" data-gpu-id="{gpu_id}" data-semantic-path="{sem_path}">')
        parts.append('<span class="toggle">·</span>')
        parts.append(f'<span class="kname">{kname}</span><span class="kdur">{kdur}</span>')
        if is_comm:
            parts.append(f'<span class="kmeta" title="{escape(str(title))}">comm raw={escape(_format_ns(original_dur))}</span>')
        if summary:
            parts.append(f'<span class="kshape" title="{escape(title)}">{escape(summary)}</span>')
        if shape.get("block_dim") not in (None, ""):
            parts.append(f'<span class="kmeta">bd={escape(str(shape.get("block_dim")))}</span>')
        stream = k.get("stream")
        if stream:
            parts.append(f'<span class="kmeta stream">{escape(str(stream))}</span>')
        parts.append('</div></li>')

    def _event_sort_key(item: Any) -> tuple:
        kind, obj = item
        if kind == "kernel":
            ts = obj.get("ts")
            tie = obj.get("gpu_id", 0)
        else:
            ts = obj.get("first_ts")
            tie = 0
        if ts is None:
            ts = 10**30
        return (int(ts), int(tie))

    def render_node(node: Dict[str, Any]) -> None:
        children = node.get("children", [])
        kernels = sorted(node.get("kernels", []), key=lambda x: (x.get("ts", 0), x.get("gpu_id", 0)))
        ordered_items = [("kernel", k) for k in kernels] + [("child", c) for c in children]
        ordered_items.sort(key=_event_sort_key)

        has_items = bool(ordered_items)
        total_k = node.get("total_kernel_count", 0)
        total_ns = node.get("total_gpu_ns", 0)
        pct = (total_ns / total_gpu_ns * 100) if total_gpu_ns > 0 else 0
        display = str(node.get("display_name", node.get("name", "?")))
        sem_shape = node.get("semantic_shape") or {}
        sem_summary = str(sem_shape.get("summary", ""))
        search_blob = (display + " " + sem_summary).lower()
        if kernels:
            search_blob += " " + " ".join((str(k.get("name", "")) + " " + _kernel_shape_summary(k)).lower() for k in kernels)

        parts.append("<li>")
        node_path = escape(str(node.get("path", "")))
        parts.append(f'<div class="node" data-name="{escape(search_blob)}" data-path="{node_path}">')
        parts.append(f'<span class="{"toggle has-children" if has_items else "toggle"}">{"▶" if has_items else "·"}</span>')
        parts.append(f'<span class="{_node_classes(node)}">{escape(display)}</span>')
        repeat_count = node.get("repeat_count", 1)
        if repeat_count > 1:
            parts.append(f'<span class="badge repeat">×{repeat_count}</span>')
            rep_name = node.get("repeat_representative")
            variant_count = int(node.get("repeat_variant_count", 1) or 1)
            if rep_name:
                title = escape(f"display representative: {rep_name}; merged variants: {variant_count}")
                parts.append(f'<span class="badge repeat" title="{title}">rep {escape(str(rep_name))}</span>')
            if variant_count > 1:
                parts.append(f'<span class="badge repeat" title="this folded group contains multiple internal variants; the visible subtree uses the modal representative">variants {variant_count}</span>')
        if sem_summary:
            title = escape(str(sem_shape.get("title", sem_summary)))
            parts.append(f'<span class="badge semantic-shape" title="{title}">shape {escape(sem_summary)}</span>')
        if total_k > 0:
            parts.append(f'<span class="badge kernels">{total_k} kernels</span>')
        if total_ns > 0:
            parts.append(f'<span class="badge gpu-time">{_format_ns(total_ns)}</span>')
        if pct >= 0.1:
            parts.append(f'<span class="badge pct">{pct:.1f}%</span>')
        parts.append("</div>")

        if ordered_items:
            parts.append('<ul class="ordered-events">')
            for kind, obj in ordered_items:
                if kind == "kernel":
                    render_kernel_item(obj)
                else:
                    render_node(obj)
            parts.append('</ul>')
        parts.append("</li>")

    parts.append("<ul>")
    render_node(root)
    parts.append("</ul>")
    parts.append(r'''
</div>
<script>
document.addEventListener('DOMContentLoaded', () => {
  const tree = document.getElementById('tree-root');
  tree.addEventListener('click', (e) => {
    const toggle = e.target.closest('.toggle.has-children');
    if (toggle) {
      const li = toggle.closest('li');
      li.classList.toggle('collapsed');
      toggle.textContent = li.classList.contains('collapsed') ? '▶' : '▼';
      return;
    }
  });
  tree.querySelectorAll('.toggle.has-children').forEach((t) => {
    const li = t.closest('li');
    const depth = li.closest('ul').closest('li') ? 1 : 0;
    if (depth < 2) t.textContent = '▼'; else li.classList.add('collapsed');
  });
  const searchInput = document.getElementById('search');
  function expandAncestors(el) {
    let li = el.closest('li');
    while (li) {
      li.classList.remove('collapsed');
      const toggle = li.querySelector(':scope > .node > .toggle');
      if (toggle && toggle.classList.contains('has-children')) toggle.textContent = '▼';
      li = li.parentElement?.closest('li');
    }
  }
  searchInput.addEventListener('input', () => {
    const q = searchInput.value.toLowerCase().trim();
    tree.querySelectorAll('.node, .kernel-inline').forEach(el => {
      el.classList.remove('highlight');
      const haystack = el.dataset.name || '';
      if (q && haystack.includes(q)) {
        el.classList.add('highlight');
        expandAncestors(el);
      }
    });
  });
  function applyFocus(msg) {
    const ids = new Set((msg && msg.kernelIds || []).map(String));
    tree.querySelectorAll('.focus-kernel,.focus-node').forEach(el => el.classList.remove('focus-kernel','focus-node'));
    let first = null;
    tree.querySelectorAll('.kernel-inline').forEach(el => {
      if (ids.has(String(el.dataset.gpuId || ''))) {
        el.classList.add('focus-kernel');
        if (!first) first = el;
        expandAncestors(el);
      }
    });
    if (!first && msg && msg.path) {
      const target = Array.from(tree.querySelectorAll('.node')).find(el => (el.dataset.path||'') === msg.path);
      if (target) { target.classList.add('focus-node'); first = target; expandAncestors(target); }
    }
    if (first) setTimeout(() => first.scrollIntoView({block:'center', inline:'nearest'}), 40);
  }
  window.addEventListener('message', (ev) => {
    const msg = ev.data || {};
    if (msg.type === 'focus-kernels') applyFocus(msg);
  });
  if (location.hash.startsWith('#focus=')) {
    try { applyFocus(JSON.parse(decodeURIComponent(location.hash.slice(7)))); } catch(e) {}
  }
});
</script>
</body>
</html>
''')
    return "".join(parts)


def write_html(path: str, tree_json: Dict[str, Any], *, merge_repeated_min: int = 3,
               trailing_digit_strip_regex: str = r"_[0-9]+$",
               title: Optional[str] = None) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    html = render_html(tree_json,
                       merge_repeated_min=merge_repeated_min,
                       trailing_digit_strip_regex=trailing_digit_strip_regex,
                       title=title)
    Path(path).write_text(html, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render normalized Ascend decode tree JSON as HTML.")
    parser.add_argument("json_file", help="Path to *.normalized_tree.json")
    parser.add_argument("--out", default=None, help="Output HTML path; default: same stem with .html")
    parser.add_argument("--merge-repeated-min", type=int, default=3)
    parser.add_argument("--trailing-digit-strip-regex", default=r"_[0-9]+$")
    args = parser.parse_args()

    with open(args.json_file, encoding="utf-8") as f:
        tree_json = json.load(f)
    out = args.out or str(Path(args.json_file).with_suffix(".html"))
    write_html(out, tree_json,
               merge_repeated_min=args.merge_repeated_min,
               trailing_digit_strip_regex=args.trailing_digit_strip_regex)
    print(f"[+] Written: {out}")


if __name__ == "__main__":
    main()
