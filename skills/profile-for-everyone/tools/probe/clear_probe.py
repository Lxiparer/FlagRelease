#!/usr/bin/env python3
"""
clear_probe.py — Config-driven nograph decode probe.

Extracts a normalized semantic tree from a single decode-token window of a
PyTorch profiler trace (Perfetto-readable). All naming/structure decisions
are driven by two JSON configs:

  - schema.json   : model architecture + semantic skeleton + classify rules
  - platform.json : runtime/hardware naming (markers, wrappers, launch APIs)

Outputs:
  <prefix>.normalized_tree.json
  <prefix>.semantic_template.json
  <prefix>.kernel_timeline.csv
  <prefix>.semantic_tree.html

Usage:
  python clear_probe.py trace.pt.trace.json.gz \\
      --schema schema.json --platform platform.json \\
      --decode-idx 10 [--out-prefix myrun]

  python clear_probe.py trace.pt.trace.json.gz \\
      --schema schema.json --platform platform.json --list-markers
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from perfetto.trace_processor import TraceProcessor
try:
    from perfetto.trace_processor import TraceProcessorConfig  # type: ignore
except Exception:  # older perfetto Python packages may not expose it
    TraceProcessorConfig = None  # type: ignore


def open_trace_processor(trace: str, trace_processor_shell: Optional[str] = None) -> TraceProcessor:
    """Open a Perfetto trace, optionally using a user-specified trace_processor_shell.

    The perfetto Python API has changed slightly across versions.  Newer
    versions accept `TraceProcessorConfig(bin_path=...)`; some older wrappers
    expose direct kwargs instead.  Try the known forms in order and produce a
    clear error if none are supported.

    The shell path can also be provided through one of:
      TRACE_PROCESSOR_SHELL
      TRACE_PROCESSOR_SHELL_PATH
      PERFETTO_TRACE_PROCESSOR_SHELL
    """
    shell = (trace_processor_shell
             or os.environ.get("TRACE_PROCESSOR_SHELL")
             or os.environ.get("TRACE_PROCESSOR_SHELL_PATH")
             or os.environ.get("PERFETTO_TRACE_PROCESSOR_SHELL"))
    if not shell:
        return TraceProcessor(trace=trace)

    shell = os.path.abspath(os.path.expanduser(str(shell)))
    if not os.path.exists(shell):
        raise FileNotFoundError(f"trace_processor_shell not found: {shell}")

    attempts = []

    if TraceProcessorConfig is not None:
        for cfg_kw in ("bin_path", "shell_path", "trace_processor_shell"):
            try:
                cfg = TraceProcessorConfig(**{cfg_kw: shell})  # type: ignore[misc]
            except TypeError as e:
                attempts.append(f"TraceProcessorConfig({cfg_kw}=...): {e}")
                continue
            try:
                return TraceProcessor(trace=trace, config=cfg)
            except TypeError as e:
                attempts.append(f"TraceProcessor(trace, config=TraceProcessorConfig({cfg_kw}=...)): {e}")
            except Exception as e:
                attempts.append(f"TraceProcessor with config {cfg_kw}: {type(e).__name__}: {e}")

    for tp_kw in ("bin_path", "shell_path", "trace_processor_shell", "trace_processor_path"):
        try:
            return TraceProcessor(trace=trace, **{tp_kw: shell})
        except TypeError as e:
            attempts.append(f"TraceProcessor({tp_kw}=...): {e}")
        except Exception as e:
            attempts.append(f"TraceProcessor direct {tp_kw}: {type(e).__name__}: {e}")

    detail = "\n  ".join(attempts[-8:])
    raise RuntimeError(
        "Could not configure perfetto TraceProcessor with the requested "
        f"trace_processor_shell: {shell}\n  {detail}"
    )


# ---------------------------------------------------------------------------
# Perfetto trace-format implementation details (NOT user config).
# These are properties of how PyTorch/CUPTI/ROCm export traces to Perfetto.
# If a new trace format appears, write a new adapter — do not config-ify.
# ---------------------------------------------------------------------------

PERFETTO_CORRELATION_KEYS = ("args.correlation", "args.External id")
PERFETTO_CUDA_RUNTIME_CATEGORY = "cuda_runtime"
PERFETTO_LAUNCH_NAME_PATTERNS = ("%LaunchKernel%", "%Memcpy%", "%Memset%")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class TreeSkeleton:
    root_key: str
    root_name: str
    decode_key_template: str
    decode_name_template: str
    unmapped_bucket_key: str
    unmapped_bucket_name: str
    fallback_bucket_key: str
    fallback_bucket_name: str


@dataclass
class HtmlRenderConfig:
    merge_repeated_min: int
    trailing_digit_strip_regex: re.Pattern[str]


@dataclass
class OutputNamingConfig:
    output_dir: str
    trace_suffixes_to_strip: Tuple[str, ...]
    default_prefix_template: str
    tree_path_template: str
    template_path_template: str
    csv_path_template: str
    html_path_template: str


@dataclass
class ClassifyRule:
    """One classification rule, compiled from schema.classify_rules[i]."""
    id: str
    phase: str
    match: Dict[str, Any]
    emit_path: List[Dict[str, str]]
    expand_modules: bool
    module_filter: Optional[List[str]]
    unattributed_bucket: Optional[Dict[str, str]]
    fallback_buckets: List[Dict[str, Any]]


@dataclass
class SchemaConfig:
    tree: TreeSkeleton
    module_alias_rules: List[Tuple[re.Pattern[str], str]]
    always_index_canonical_types: frozenset
    classify_rules: List[ClassifyRule]
    html_render: HtmlRenderConfig
    output_naming: OutputNamingConfig


@dataclass
class MarkerAutodetect:
    name_like_any: Tuple[str, ...]
    name_bonus_keywords: Dict[str, int]


@dataclass
class PlatformConfig:
    decode_marker_aliases: Tuple[str, ...]
    marker_autodetect: MarkerAutodetect
    gpu_categories: Tuple[str, ...]
    cpu_wrapper_compact_names: frozenset
    cuda_launch_prefixes: Tuple[str, ...]


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_schema(path: str) -> SchemaConfig:
    cfg = _load_json(path)

    sk = cfg["tree_skeleton"]
    tree = TreeSkeleton(
        root_key=sk["root_key"],
        root_name=sk["root_name"],
        decode_key_template=sk["decode_key_template"],
        decode_name_template=sk["decode_name_template"],
        unmapped_bucket_key=sk["unmapped_bucket"]["key"],
        unmapped_bucket_name=sk["unmapped_bucket"]["name"],
        fallback_bucket_key=sk["fallback_bucket"]["key"],
        fallback_bucket_name=sk["fallback_bucket"]["name"],
    )

    module_alias_rules: List[Tuple[re.Pattern[str], str]] = []
    for item in cfg.get("module_aliases", []):
        module_alias_rules.append((re.compile(item["pattern"]), str(item["target"])))

    always_index = frozenset(cfg.get("always_index_canonical_types", []))

    classify_rules: List[ClassifyRule] = []
    for r in cfg["classify_rules"]:
        classify_rules.append(ClassifyRule(
            id=r["id"],
            phase=r["phase"],
            match=r["match"],
            emit_path=list(r["emit_path"]),
            expand_modules=bool(r.get("expand_modules", False)),
            module_filter=r.get("module_filter"),
            unattributed_bucket=r.get("unattributed_bucket"),
            fallback_buckets=list(r.get("fallback_buckets", [])),
        ))

    hr = cfg["html_render"]
    html_render = HtmlRenderConfig(
        merge_repeated_min=int(hr["merge_repeated_min"]),
        trailing_digit_strip_regex=re.compile(hr["trailing_digit_strip_regex"]),
    )

    on = cfg["output_naming"]
    output_naming = OutputNamingConfig(
        output_dir=on.get("output_dir", "output"),
        trace_suffixes_to_strip=tuple(on["trace_suffixes_to_strip"]),
        default_prefix_template=on["default_prefix_template"],
        tree_path_template=on["outputs"]["tree"],
        template_path_template=on["outputs"]["template"],
        csv_path_template=on["outputs"]["csv"],
        html_path_template=on["outputs"]["html"],
    )

    return SchemaConfig(
        tree=tree,
        module_alias_rules=module_alias_rules,
        always_index_canonical_types=always_index,
        classify_rules=classify_rules,
        html_render=html_render,
        output_naming=output_naming,
    )


def load_platform(path: str) -> PlatformConfig:
    cfg = _load_json(path)

    mad = cfg["marker_autodetect"]
    marker_autodetect = MarkerAutodetect(
        name_like_any=tuple(mad["name_like_any"]),
        name_bonus_keywords=dict(mad["name_bonus_keywords"]),
    )

    return PlatformConfig(
        decode_marker_aliases=tuple(cfg["decode_marker_aliases"]),
        marker_autodetect=marker_autodetect,
        gpu_categories=tuple(cfg["gpu_categories"]),
        cpu_wrapper_compact_names=frozenset(cfg["cpu_wrapper_compact_names"]),
        cuda_launch_prefixes=tuple(cfg["cuda_launch_prefixes"]),
    )


# ---------------------------------------------------------------------------
# Raw records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RawSlice:
    id: int
    parent_id: Optional[int]
    name: str
    ts: int
    dur_ns: int
    depth: int


@dataclass
class ModuleInfo:
    slice_id: int
    original_name: str
    module_name: str
    base_name: str
    suffix_index: Optional[int]
    canonical_type: str
    parent_module_id: Optional[int]
    sibling_ordinal: int = 0
    sibling_count: int = 1

    def canonical_segment(self, always_index_types: frozenset) -> str:
        if self.sibling_count > 1 or self.canonical_type in always_index_types:
            return f"{self.canonical_type}[{self.sibling_ordinal}]"
        return self.canonical_type


@dataclass
class KernelRecord:
    gpu_id: int
    name: str
    ts: int
    dur_ns: int
    category: str
    stream: str
    cpu_id: Optional[int] = None
    cpu_launch_name: str = ""
    semantic_path: str = ""
    semantic_name: str = ""
    phase: str = ""
    start_offset_ns: int = 0
    end_offset_ns: int = 0
    gap_to_next_ns: Optional[int] = None
    overlap_to_next_ns: int = 0
    cpu_chain_tail: str = ""
    relation: str = "nograph_observed"
    confidence: float = 1.0
    matched_rule_id: str = ""  # which classify rule fired (or "unmapped"/"fallback")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gpu_id": self.gpu_id,
            "name": self.name,
            "ts": self.ts,
            "dur_ns": self.dur_ns,
            "category": self.category,
            "stream": self.stream,
            "cpu_id": self.cpu_id,
            "cpu_launch_name": self.cpu_launch_name,
            "semantic_path": self.semantic_path,
            "semantic_name": self.semantic_name,
            "phase": self.phase,
            "start_offset_ns": self.start_offset_ns,
            "end_offset_ns": self.end_offset_ns,
            "gap_to_next_ns": self.gap_to_next_ns,
            "overlap_to_next_ns": self.overlap_to_next_ns,
            "cpu_chain_tail": self.cpu_chain_tail,
            "relation": self.relation,
            "confidence": self.confidence,
            "matched_rule_id": self.matched_rule_id,
        }


@dataclass
class SemanticNode:
    key: str
    name: str
    category: str
    path: str
    canonical_type: Optional[str] = None
    original_names: List[str] = field(default_factory=list)
    children: "OrderedDict[str, SemanticNode]" = field(default_factory=OrderedDict)
    kernels: List[KernelRecord] = field(default_factory=list)
    self_gpu_sum_ns: int = 0
    total_gpu_sum_ns: int = 0
    self_kernel_count: int = 0
    total_kernel_count: int = 0
    first_ts: Optional[int] = None
    last_ts: Optional[int] = None

    def add_child(self, key: str, name: str, category: str,
                  canonical_type: Optional[str] = None,
                  original_name: Optional[str] = None) -> "SemanticNode":
        if key not in self.children:
            child_path = key if not self.path else f"{self.path}/{key}"
            self.children[key] = SemanticNode(
                key=key, name=name, category=category, path=child_path,
                canonical_type=canonical_type,
            )
        child = self.children[key]
        if original_name and original_name not in child.original_names:
            child.original_names.append(original_name)
        if canonical_type and not child.canonical_type:
            child.canonical_type = canonical_type
        return child

    def to_dict(self, include_kernels: bool = True) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "path": self.path,
            "canonical_type": self.canonical_type,
            "original_names": self.original_names,
            "self_gpu_sum_ns": self.self_gpu_sum_ns,
            "total_gpu_sum_ns": self.total_gpu_sum_ns,
            "self_kernel_count": self.self_kernel_count,
            "total_kernel_count": self.total_kernel_count,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "children": [c.to_dict(include_kernels=include_kernels) for c in self.children.values()],
        }
        if include_kernels:
            out["kernels"] = [k.to_dict() for k in self.kernels]
        return out


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------

def sql_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def compact_cpu_name(name: str) -> str:
    if name.startswith("nn.Module: "):
        return name[len("nn.Module: "):]
    if ".py(" in name and "): " in name:
        return name.split("): ", 1)[1]
    return name


def is_module_name(name: str) -> bool:
    return name.startswith("nn.Module: ")


def canonicalize_module_type(base: str, alias_rules: List[Tuple[re.Pattern[str], str]]) -> str:
    for pat, repl in alias_rules:
        if pat.match(base):
            return repl
    return base


def parse_module_name(name: str,
                      alias_rules: List[Tuple[re.Pattern[str], str]]
                      ) -> Optional[Tuple[str, str, Optional[int], str]]:
    if not is_module_name(name):
        return None
    module_name = name[len("nn.Module: "):].strip()
    m = re.match(r"^(.*?)(?:_(\d+))?$", module_name)
    if not m:
        base = module_name
        suffix = None
    else:
        base = m.group(1)
        suffix = int(m.group(2)) if m.group(2) is not None else None
    canonical = canonicalize_module_type(base, alias_rules)
    return module_name, base, suffix, canonical


def is_cpu_wrapper(name: str, platform: PlatformConfig) -> bool:
    compact = compact_cpu_name(name).strip()
    if compact in platform.cpu_wrapper_compact_names:
        return True
    return any(compact.startswith(prefix) for prefix in platform.cuda_launch_prefixes)


def contains_any(names: Iterable[str], needles: Iterable[str]) -> bool:
    names_l = list(names)
    return any(any(n in x for x in names_l) for n in needles)


# ---------------------------------------------------------------------------
# Perfetto queries
# ---------------------------------------------------------------------------

def query_df(tp: TraceProcessor, sql: str):
    return tp.query(sql).as_pandas_dataframe()


def _build_marker_like_clause(platform: PlatformConfig) -> str:
    parts = [f"name LIKE {sql_quote(p)}" for p in platform.marker_autodetect.name_like_any]
    return "(" + " OR ".join(parts) + ")"


def list_marker_candidates(tp: TraceProcessor, platform: PlatformConfig, limit: int = 80) -> None:
    like = _build_marker_like_clause(platform)
    df = query_df(tp, f"""
        SELECT name, COUNT(*) AS n,
               MIN(dur) AS min_dur, CAST(AVG(dur) AS INT) AS avg_dur, MAX(dur) AS max_dur
        FROM slice
        WHERE {like}
        GROUP BY name
        ORDER BY n DESC, avg_dur DESC
        LIMIT {int(limit)}
    """)
    if len(df) == 0:
        print("[!] No marker-like slice names found.")
        return
    print(df.to_string(index=False))


def get_markers_by_name(tp: TraceProcessor, name: str):
    return query_df(tp, f"""
        SELECT id, name, ts, dur
        FROM slice
        WHERE name = {sql_quote(name)}
        ORDER BY ts
    """)


def count_gpu_in_window(tp: TraceProcessor, platform: PlatformConfig,
                        ts_start: int, ts_end: int) -> int:
    cats = ",".join(sql_quote(c) for c in platform.gpu_categories)
    df = query_df(tp, f"""
        SELECT COUNT(*) AS n
        FROM slice
        WHERE ts >= {int(ts_start)} AND ts < {int(ts_end)}
          AND category IN ({cats})
    """)
    return int(df.iloc[0]["n"])


def find_decode_window(tp: TraceProcessor, platform: PlatformConfig,
                       decode_idx: int,
                       decode_marker: Optional[str] = None) -> Dict[str, Any]:
    if decode_idx < 1:
        raise ValueError("decode_idx is 1-indexed and must be >= 1")

    tried: List[str] = []
    marker_names = [decode_marker] if decode_marker else list(platform.decode_marker_aliases)
    for name in marker_names:
        if not name:
            continue
        tried.append(name)
        markers = get_markers_by_name(tp, name)
        print(f"[+] marker candidate {name!r}: {len(markers)} slices")
        if len(markers) >= decode_idx + 1:
            cur = markers.iloc[decode_idx - 1]
            nxt = markers.iloc[decode_idx]
            return {
                "marker_name": name,
                "marker_id": int(cur["id"]),
                "ts_start": int(cur["ts"]),
                "ts_end": int(nxt["ts"]),
                "marker_duration_ns": int(cur["dur"]),
                "method": "exact_alias_or_user_marker",
                "tried": tried,
            }

    if decode_marker:
        raise RuntimeError(
            f"decode marker {decode_marker!r} has fewer than {decode_idx + 1} occurrences. "
            f"Use --list-markers to inspect candidates."
        )

    print("[!] default marker aliases not enough; trying coverage-based autodetect...")
    like = _build_marker_like_clause(platform)
    candidates = query_df(tp, f"""
        SELECT name, COUNT(*) AS n,
               MIN(dur) AS min_dur, CAST(AVG(dur) AS INT) AS avg_dur, MAX(dur) AS max_dur
        FROM slice
        WHERE {like}
        GROUP BY name
        HAVING n >= {decode_idx + 1}
        ORDER BY n DESC, avg_dur DESC
        LIMIT 60
    """)
    if len(candidates) == 0:
        raise RuntimeError("No repeated execute-like marker candidates found. "
                           "Use --list-markers or --decode-marker.")

    best: Optional[Dict[str, Any]] = None
    for _, row in candidates.iterrows():
        name = str(row["name"])
        markers = get_markers_by_name(tp, name)
        if len(markers) < decode_idx + 1:
            continue
        cur = markers.iloc[decode_idx - 1]
        nxt = markers.iloc[decode_idx]
        ts_start = int(cur["ts"])
        ts_end = int(nxt["ts"])
        if ts_end <= ts_start:
            continue
        gpu_n = count_gpu_in_window(tp, platform, ts_start, ts_end)
        low = name.lower()
        name_bonus = sum(bonus for kw, bonus in platform.marker_autodetect.name_bonus_keywords.items()
                         if kw in low)
        score = gpu_n * 10 + name_bonus
        cand = {
            "marker_name": name,
            "marker_id": int(cur["id"]),
            "ts_start": ts_start,
            "ts_end": ts_end,
            "marker_duration_ns": int(cur["dur"]),
            "method": "coverage_autodetect",
            "gpu_count_score_window": gpu_n,
            "score": score,
            "tried": tried,
        }
        if best is None or cand["score"] > best["score"]:
            best = cand

    if best is None:
        raise RuntimeError("Failed to autodetect a decode window. Use --decode-marker.")
    print(f"[+] autodetected marker {best['marker_name']!r}, score={best['score']}, "
          f"gpu_in_window={best.get('gpu_count_score_window')}")
    return best


def query_gpu_slices(tp: TraceProcessor, platform: PlatformConfig,
                     ts_start: int, ts_end: int):
    cats = ",".join(sql_quote(c) for c in platform.gpu_categories)
    return query_df(tp, f"""
        SELECT s.id, s.ts, s.dur, s.name, s.category,
               COALESCE(t.name, '?') AS stream_name
        FROM slice s
        LEFT JOIN thread_track tt ON s.track_id = tt.id
        LEFT JOIN thread t ON tt.utid = t.utid
        WHERE s.ts >= {int(ts_start)} AND s.ts < {int(ts_end)}
          AND s.category IN ({cats})
        ORDER BY s.ts, s.id
    """)


def query_flows(tp: TraceProcessor, gpu_ids: List[int]):
    if not gpu_ids:
        return None
    ids = ",".join(str(int(x)) for x in gpu_ids)
    return query_df(tp, f"""
        SELECT slice_in AS gpu_id, slice_out AS cpu_id, 'slice_in_gpu' AS direction
        FROM flow
        WHERE slice_in IN ({ids})
        UNION ALL
        SELECT slice_out AS gpu_id, slice_in AS cpu_id, 'slice_out_gpu' AS direction
        FROM flow
        WHERE slice_out IN ({ids})
    """)


def assign_orphans_by_correlation(tp: TraceProcessor,
                                  orphan_gpu_ids: List[int]) -> Dict[int, int]:
    """Two-step correlation-based GPU->CPU matching.

    Uses Perfetto-format-level constants (PERFETTO_CORRELATION_KEYS,
    PERFETTO_CUDA_RUNTIME_CATEGORY, PERFETTO_LAUNCH_NAME_PATTERNS) — these
    are properties of the trace exporter, not user config.
    """
    if not orphan_gpu_ids:
        return {}
    gpu_to_cpu: Dict[int, int] = {}
    BATCH = 500
    name_like = " OR ".join(f"s.name LIKE '{p}'" for p in PERFETTO_LAUNCH_NAME_PATTERNS)
    for corr_key in PERFETTO_CORRELATION_KEYS:
        remaining = [gid for gid in orphan_gpu_ids if gid not in gpu_to_cpu]
        if not remaining:
            break
        for i in range(0, len(remaining), BATCH):
            batch = remaining[i:i + BATCH]
            batch_ids = ",".join(str(int(x)) for x in batch)
            gpu_corr = query_df(tp, f"""
                SELECT s.id AS gpu_id, a.int_value AS corr_val
                FROM slice s
                JOIN args a ON s.arg_set_id = a.arg_set_id
                WHERE s.id IN ({batch_ids})
                  AND a.key = '{corr_key}'
            """)
            if gpu_corr is None or len(gpu_corr) == 0:
                continue
            vals = gpu_corr["corr_val"].dropna().unique().tolist()
            if not vals:
                continue
            vals_str = ",".join(str(int(v)) for v in vals)
            cpu_corr = query_df(tp, f"""
                SELECT s.id AS cpu_id, a.int_value AS corr_val
                FROM slice s
                JOIN args a ON s.arg_set_id = a.arg_set_id
                WHERE a.key = '{corr_key}'
                  AND a.int_value IN ({vals_str})
                  AND s.category = '{PERFETTO_CUDA_RUNTIME_CATEGORY}'
                  AND ({name_like})
            """)
            if cpu_corr is None or len(cpu_corr) == 0:
                continue
            cpu_map: Dict[int, int] = {}
            for _, r in cpu_corr.iterrows():
                cv = int(r["corr_val"])
                if cv not in cpu_map:
                    cpu_map[cv] = int(r["cpu_id"])
            for _, r in gpu_corr.iterrows():
                gid = int(r["gpu_id"])
                cv = int(r["corr_val"])
                if gid not in gpu_to_cpu and cv in cpu_map:
                    gpu_to_cpu[gid] = cpu_map[cv]
    return gpu_to_cpu


def query_ancestor_slices(tp: TraceProcessor, cpu_ids: List[int]) -> Dict[int, RawSlice]:
    if not cpu_ids:
        return {}
    ids = ",".join(str(int(x)) for x in sorted(set(cpu_ids)))
    anc = query_df(tp, f"""
        WITH RECURSIVE walk(id, parent_id, name, ts, dur, depth) AS (
            SELECT s.id, s.parent_id, s.name, s.ts, s.dur, s.depth
            FROM slice s WHERE s.id IN ({ids})
            UNION ALL
            SELECT s.id, s.parent_id, s.name, s.ts, s.dur, s.depth
            FROM slice s JOIN walk w ON s.id = w.parent_id
        )
        SELECT DISTINCT id, parent_id, name, ts, dur, depth
        FROM walk
    """)
    out: Dict[int, RawSlice] = {}
    for _, r in anc.iterrows():
        pid = r["parent_id"]
        try:
            parent_id: Optional[int] = int(pid)
        except (TypeError, ValueError):
            parent_id = None
        out[int(r["id"])] = RawSlice(
            id=int(r["id"]),
            parent_id=parent_id,
            name=str(r["name"]) if r["name"] is not None else "<unnamed>",
            ts=int(r["ts"]),
            dur_ns=int(r["dur"]) if r["dur"] is not None else 0,
            depth=int(r["depth"]) if r["depth"] is not None else 0,
        )
    return out


# ---------------------------------------------------------------------------
# Rule engine — data-driven replacement for the old classify_kernel()
# ---------------------------------------------------------------------------

@dataclass
class ClassificationResult:
    specs: List[Tuple[str, str, str, Optional[str], Optional[str]]]
    phase: str
    leaf_name: str
    matched_rule_id: str


class RuleEngine:
    """Interprets schema.classify_rules against a CPU chain + module info.

    Behaviour mirrors the original classify_kernel():
      - rules are evaluated in order; first match wins
      - on match, emit_path is materialized as path specs
      - if expand_modules is true, the chain's module list (optionally filtered
        by module_filter) is appended after emit_path
      - if expand_modules and no modules survive:
          * try fallback_buckets[] in order; first whose `when` matches wins
          * else use unattributed_bucket
      - if no rule matches at all, emit a single fallback bucket from the
        tree skeleton (e.g. other_gpu)
    """

    def __init__(self, schema: SchemaConfig) -> None:
        self.schema = schema

    # ---- condition evaluation ----

    def _eval_condition(self,
                        cond: Dict[str, Any],
                        compact_names: List[str],
                        mod_types: List[str]) -> bool:
        if "any_of" in cond:
            return any(self._eval_condition(c, compact_names, mod_types)
                       for c in cond["any_of"])
        if "all_of" in cond:
            return all(self._eval_condition(c, compact_names, mod_types)
                       for c in cond["all_of"])
        if "chain_contains_any" in cond:
            return contains_any(compact_names, cond["chain_contains_any"])
        if "module_types_contains_any" in cond:
            needles = set(cond["module_types_contains_any"])
            return any(t in needles for t in mod_types)
        # Unknown key: be strict — refuse to silently pass.
        raise ValueError(f"Unknown match condition keys: {list(cond.keys())}")

    # ---- spec emission ----

    def _emit_path_specs(self, rule: ClassifyRule
                         ) -> List[Tuple[str, str, str, Optional[str], Optional[str]]]:
        return [(p["key"], p["name"], p["category"], None, None)
                for p in rule.emit_path]

    def _filter_modules(self, mods: List[ModuleInfo],
                        module_filter: Optional[List[str]]) -> List[ModuleInfo]:
        if module_filter is None:
            return mods
        keep = set(module_filter)
        return [m for m in mods if m.canonical_type in keep]

    def _module_to_spec(self, m: ModuleInfo
                        ) -> Tuple[str, str, str, Optional[str], Optional[str]]:
        seg = m.canonical_segment(self.schema.always_index_canonical_types)
        return (seg, m.original_name, "module", m.canonical_type, m.original_name)

    # ---- main ----

    def classify(self,
                 chain: List[RawSlice],
                 mods: List[ModuleInfo]) -> ClassificationResult:
        compact_names = [compact_cpu_name(s.name) for s in chain]
        mod_types = [m.canonical_type for m in mods]

        for rule in self.schema.classify_rules:
            if not self._eval_condition(rule.match, compact_names, mod_types):
                continue

            specs = self._emit_path_specs(rule)

            if rule.expand_modules:
                filtered = self._filter_modules(mods, rule.module_filter)
                if filtered:
                    for m in filtered:
                        specs.append(self._module_to_spec(m))
                    leaf = filtered[-1].original_name
                else:
                    # Try fallback buckets first, then unattributed.
                    fb_used = False
                    for fb in rule.fallback_buckets:
                        when = fb.get("when", {})
                        if self._eval_condition(when, compact_names, mod_types):
                            specs.append((fb["key"], fb["name"], "bucket", None, None))
                            leaf = fb["name"]
                            fb_used = True
                            break
                    if not fb_used:
                        if rule.unattributed_bucket is not None:
                            ub = rule.unattributed_bucket
                            specs.append((ub["key"], ub["name"], "bucket", None, None))
                            leaf = ub["name"]
                        else:
                            leaf = specs[-1][1] if specs else rule.phase
            else:
                leaf = specs[-1][1] if specs else rule.phase

            return ClassificationResult(specs=specs, phase=rule.phase,
                                        leaf_name=leaf, matched_rule_id=rule.id)

        # No rule matched: fallback bucket from skeleton.
        fb_key = self.schema.tree.fallback_bucket_key
        fb_name = self.schema.tree.fallback_bucket_name
        return ClassificationResult(
            specs=[(fb_key, fb_name, "bucket", None, None)],
            phase=fb_name,
            leaf_name=fb_name,
            matched_rule_id="__fallback__",
        )


# ---------------------------------------------------------------------------
# Semantic normalization
# ---------------------------------------------------------------------------

def build_chain(cpu_id: int, slices: Dict[int, RawSlice]) -> List[RawSlice]:
    chain: List[RawSlice] = []
    cur: Optional[int] = cpu_id
    seen = set()
    while cur is not None and cur in slices and cur not in seen:
        seen.add(cur)
        s = slices[cur]
        chain.append(s)
        cur = s.parent_id
    chain.reverse()
    return chain


def build_module_infos(slices: Dict[int, RawSlice],
                       schema: SchemaConfig) -> Dict[int, ModuleInfo]:
    infos: Dict[int, ModuleInfo] = {}
    for sid, s in slices.items():
        parsed = parse_module_name(s.name, schema.module_alias_rules)
        if parsed is None:
            continue
        module_name, base, suffix, canonical = parsed
        infos[sid] = ModuleInfo(
            slice_id=sid,
            original_name=s.name,
            module_name=module_name,
            base_name=base,
            suffix_index=suffix,
            canonical_type=canonical,
            parent_module_id=None,
        )

    for sid, info in infos.items():
        cur = slices[sid].parent_id
        seen = set()
        parent_mod: Optional[int] = None
        while cur is not None and cur in slices and cur not in seen:
            seen.add(cur)
            if cur in infos:
                parent_mod = cur
                break
            cur = slices[cur].parent_id
        info.parent_module_id = parent_mod

    groups: Dict[Tuple[Optional[int], str], List[ModuleInfo]] = defaultdict(list)
    for info in infos.values():
        groups[(info.parent_module_id, info.canonical_type)].append(info)

    for _, group in groups.items():
        group.sort(key=lambda x: (slices[x.slice_id].ts, x.slice_id))
        count = len(group)
        for i, info in enumerate(group):
            info.sibling_ordinal = i
            info.sibling_count = count

    return infos


def module_infos_from_chain(chain: List[RawSlice],
                            module_infos: Dict[int, ModuleInfo]) -> List[ModuleInfo]:
    return [module_infos[s.id] for s in chain if s.id in module_infos]


def attach_kernel(root_decode: SemanticNode,
                  specs: List[Tuple[str, str, str, Optional[str], Optional[str]]],
                  kernel: KernelRecord) -> SemanticNode:
    cur = root_decode
    for key, display, category, canonical_type, original_name in specs:
        cur = cur.add_child(key, display, category, canonical_type, original_name)
    cur.kernels.append(kernel)
    kernel.semantic_path = cur.path
    kernel.semantic_name = cur.name
    return cur


def finalize_stats(node: SemanticNode) -> None:
    for c in node.children.values():
        finalize_stats(c)

    node.self_kernel_count = len(node.kernels)
    node.self_gpu_sum_ns = sum(k.dur_ns for k in node.kernels)

    total_count = node.self_kernel_count
    total_sum = node.self_gpu_sum_ns
    times: List[Tuple[int, int]] = []
    for k in node.kernels:
        times.append((k.ts, k.ts + k.dur_ns))
    for c in node.children.values():
        total_count += c.total_kernel_count
        total_sum += c.total_gpu_sum_ns
        if c.first_ts is not None and c.last_ts is not None:
            times.append((c.first_ts, c.last_ts))

    node.total_kernel_count = total_count
    node.total_gpu_sum_ns = total_sum
    if times:
        node.first_ts = min(t[0] for t in times)
        node.last_ts = max(t[1] for t in times)
    else:
        node.first_ts = None
        node.last_ts = None


def flatten_nodes(node: SemanticNode, out: List[Dict[str, Any]]) -> None:
    direct_names = Counter(k.name for k in node.kernels)
    out.append({
        "path": node.path,
        "name": node.name,
        "key": node.key,
        "category": node.category,
        "canonical_type": node.canonical_type,
        "original_names": node.original_names,
        "self_kernel_count": node.self_kernel_count,
        "total_kernel_count": node.total_kernel_count,
        "self_gpu_sum_ns": node.self_gpu_sum_ns,
        "total_gpu_sum_ns": node.total_gpu_sum_ns,
        "first_ts": node.first_ts,
        "last_ts": node.last_ts,
        "direct_kernel_name_counts": dict(direct_names),
        "direct_kernel_sequence": [
            {
                "name": k.name,
                "dur_ns": k.dur_ns,
                "category": k.category,
                "stream": k.stream,
                "gap_to_next_ns": k.gap_to_next_ns,
            }
            for k in sorted(node.kernels, key=lambda x: (x.ts, x.gpu_id))
        ],
        "children": [c.path for c in node.children.values()],
    })
    for c in node.children.values():
        flatten_nodes(c, out)


def compute_global_kernel_gaps(kernels: List[KernelRecord], ts_start: int) -> None:
    kernels.sort(key=lambda k: (k.ts, k.gpu_id))
    for i, k in enumerate(kernels):
        k.start_offset_ns = k.ts - ts_start
        k.end_offset_ns = k.start_offset_ns + k.dur_ns
        if i + 1 < len(kernels):
            nxt = kernels[i + 1]
            gap = nxt.ts - (k.ts + k.dur_ns)
            if gap >= 0:
                k.gap_to_next_ns = gap
                k.overlap_to_next_ns = 0
            else:
                k.gap_to_next_ns = 0
                k.overlap_to_next_ns = -gap
        else:
            k.gap_to_next_ns = None
            k.overlap_to_next_ns = 0


# ---------------------------------------------------------------------------
# LLM refinement hook (placeholder — wired but no-op)
# ---------------------------------------------------------------------------

@dataclass
class LlmRefinementContext:
    """What we'd send to an LLM to refine classification.

    NOTE: nothing in clear_probe.py constructs or consumes this yet.
    The fields below sketch the intended payload so a future LLM layer
    has a stable target. Keep it small and dedup-friendly.
    """
    schema_skeleton: Dict[str, Any]
    existing_rule_ids: List[str]
    unmapped_chain_signatures: List[Dict[str, Any]]      # dedup'd orphans
    low_confidence_classifications: List[Dict[str, Any]] # rule fired but unsure
    structural_anomalies: List[Dict[str, Any]]           # e.g. model_forward with 0 modules
    platform_hint: Dict[str, Any]


def llm_refine_hook(context: LlmRefinementContext) -> Optional[Dict[str, Any]]:
    """Placeholder. A future implementation will:
      1. serialize `context` into a prompt
      2. call an LLM with a constrained output schema
      3. return a config patch (new module aliases, new classify rules, etc.)
         that gets applied to schema/platform and triggers a re-classify pass.

    For now this is a no-op. Returning None means: don't refine, ship as-is.
    """
    return None


# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------

def probe(trace_path: str,
          schema: SchemaConfig,
          platform: PlatformConfig,
          decode_idx: int,
          decode_marker: Optional[str] = None,
          include_kernels_in_tree_json: bool = True,
          trace_processor_shell: Optional[str] = None
          ) -> Tuple[Dict[str, Any], Dict[str, Any], List[KernelRecord]]:
    print(f"[*] Loading trace: {trace_path}")
    tp = open_trace_processor(trace_path, trace_processor_shell=trace_processor_shell)
    try:
        win = find_decode_window(tp, platform, decode_idx=decode_idx,
                                 decode_marker=decode_marker)
        ts_start = int(win["ts_start"])
        ts_end = int(win["ts_end"])
        print(f"[+] decode #{decode_idx} window: [{ts_start}, {ts_end}) = "
              f"{(ts_end - ts_start) / 1e6:.3f} ms")
        print(f"[+] marker: {win['marker_name']!r} via {win['method']}")

        gpu_df = query_gpu_slices(tp, platform, ts_start, ts_end)
        print(f"[+] GPU kernels/memops in window: {len(gpu_df)}")
        if len(gpu_df) == 0:
            raise RuntimeError("No GPU kernel/memcpy/memset slices found in selected window.")

        gpu_by_id: Dict[int, KernelRecord] = {}
        for _, r in gpu_df.iterrows():
            kid = int(r["id"])
            gpu_by_id[kid] = KernelRecord(
                gpu_id=kid,
                name=str(r["name"]),
                ts=int(r["ts"]),
                dur_ns=int(r["dur"]) if r["dur"] is not None else 0,
                category=str(r["category"]),
                stream=str(r["stream_name"]),
            )

        flows = query_flows(tp, list(gpu_by_id.keys()))
        if flows is None or len(flows) == 0:
            print("[!] No CPU↔GPU flow edges. Expected for CUDA Graph traces, not NoGraph.")
            flows = []
        else:
            print(f"[+] CPU↔GPU flow edges: {len(flows)}")

        gpu_to_cpu: Dict[int, int] = {}
        if len(flows) > 0:
            for _, f in flows.iterrows():
                gid = int(f["gpu_id"])
                cid = int(f["cpu_id"])
                if gid in gpu_by_id and gid not in gpu_to_cpu:
                    gpu_to_cpu[gid] = cid
                    gpu_by_id[gid].cpu_id = cid

        orphan_gpu_ids = [gid for gid in gpu_by_id if gid not in gpu_to_cpu]
        if orphan_gpu_ids:
            print(f"[!] Orphan GPU slices without flow: {len(orphan_gpu_ids)}")
            corr_map = assign_orphans_by_correlation(tp, orphan_gpu_ids)
            if corr_map:
                gpu_to_cpu.update(corr_map)
                for gid, cid in corr_map.items():
                    gpu_by_id[gid].cpu_id = cid
                print(f"[+] Correlation-based mapping: {len(corr_map)} additional kernels")
                orphan_gpu_ids = [gid for gid in gpu_by_id if gid not in gpu_to_cpu]
                if orphan_gpu_ids:
                    print(f"[!] Remaining orphans after correlation: {len(orphan_gpu_ids)}")

        cpu_ids = sorted(set(gpu_to_cpu.values()))
        slices = query_ancestor_slices(tp, cpu_ids)
        print(f"[+] unique CPU ancestor slices: {len(slices)}")

        module_infos = build_module_infos(slices, schema)
        print(f"[+] nn.Module slices observed in GPU-bearing CPU ancestors: {len(module_infos)}")

        # Build normalized semantic tree using schema-defined skeleton.
        root = SemanticNode(
            key=schema.tree.root_key, name=schema.tree.root_name,
            category="root", path=schema.tree.root_key,
        )
        decode_key = schema.tree.decode_key_template.format(decode_idx=decode_idx)
        decode_name = schema.tree.decode_name_template.format(decode_idx=decode_idx)
        decode = root.add_child(decode_key, decode_name, "phase")

        kernels = list(gpu_by_id.values())
        compute_global_kernel_gaps(kernels, ts_start)

        engine = RuleEngine(schema)
        unmapped_key = schema.tree.unmapped_bucket_key
        unmapped_name = schema.tree.unmapped_bucket_name

        for k in kernels:
            if k.cpu_id is None or k.cpu_id not in slices:
                specs = [(unmapped_key, unmapped_name, "bucket", None, None)]
                k.phase = unmapped_name
                k.cpu_launch_name = "<no-flow>"
                k.cpu_chain_tail = "<no-flow>"
                k.matched_rule_id = "__unmapped__"
                attach_kernel(decode, specs, k)
                continue

            chain = build_chain(k.cpu_id, slices)
            cpu_slice = slices[k.cpu_id]
            k.cpu_launch_name = compact_cpu_name(cpu_slice.name)
            tail = [compact_cpu_name(s.name) for s in chain[-8:]]
            k.cpu_chain_tail = " > ".join(tail)
            mods = module_infos_from_chain(chain, module_infos)
            result = engine.classify(chain, mods)
            k.phase = result.phase
            k.matched_rule_id = result.matched_rule_id
            attach_kernel(decode, result.specs, k)

        # === LLM refinement hook (no-op for now) ===
        # When wired, this is where we'd:
        #   1. build LlmRefinementContext from kernels/slices/module_infos
        #   2. call llm_refine_hook(ctx) -> Optional[patch]
        #   3. apply patch to schema (new aliases / new rules) and re-classify
        # Left intentionally empty.
        _ = llm_refine_hook  # silence "unused" linters; real call site goes here later

        finalize_stats(root)

        tree_json = {
            "schema_version": 1,
            "kind": "nograph_normalized_semantic_tree",
            "trace_path": trace_path,
            "decode_idx": decode_idx,
            "marker": win,
            "window": {
                "ts_start": ts_start,
                "ts_end": ts_end,
                "duration_ns": ts_end - ts_start,
                "duration_ms": round((ts_end - ts_start) / 1e6, 6),
            },
            "stats": {
                "gpu_kernel_count": len(kernels),
                "flow_count": len(flows) if hasattr(flows, "__len__") else 0,
                "orphan_gpu_count": len(orphan_gpu_ids),
                "cpu_ancestor_slice_count": len(slices),
                "module_slice_count": len(module_infos),
            },
            "root": root.to_dict(include_kernels=include_kernels_in_tree_json),
        }

        nodes: List[Dict[str, Any]] = []
        flatten_nodes(root, nodes)
        template_json = {
            "schema_version": 1,
            "kind": "nograph_semantic_template",
            "trace_path": trace_path,
            "decode_idx": decode_idx,
            "marker": win,
            "window": tree_json["window"],
            "stats": tree_json["stats"],
            "nodes": nodes,
        }

        return tree_json, template_json, kernels
    finally:
        tp.close()


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def ensure_parent_dir(path: str) -> None:
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)


def default_prefix(trace_path: str, decode_idx: int, schema: SchemaConfig) -> str:
    base = Path(trace_path).name
    for ext in schema.output_naming.trace_suffixes_to_strip:
        if base.endswith(ext):
            base = base[:-len(ext)]
            break
    return schema.output_naming.default_prefix_template.format(
        base=base, decode_idx=decode_idx)


def write_json(path: str, obj: Dict[str, Any]) -> None:
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def write_kernel_csv(path: str, kernels: List[KernelRecord], decode_idx: int) -> None:
    ensure_parent_dir(path)
    fields = [
        "decode_idx", "global_kernel_idx", "gpu_id", "kernel_name", "category", "stream",
        "ts", "dur_ns", "start_offset_ns", "end_offset_ns", "gap_to_next_ns", "overlap_to_next_ns",
        "cpu_id", "cpu_launch_name", "phase", "semantic_path", "semantic_name",
        "matched_rule_id", "relation", "confidence", "cpu_chain_tail",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, k in enumerate(sorted(kernels, key=lambda x: (x.ts, x.gpu_id))):
            w.writerow({
                "decode_idx": decode_idx,
                "global_kernel_idx": i,
                "gpu_id": k.gpu_id,
                "kernel_name": k.name,
                "category": k.category,
                "stream": k.stream,
                "ts": k.ts,
                "dur_ns": k.dur_ns,
                "start_offset_ns": k.start_offset_ns,
                "end_offset_ns": k.end_offset_ns,
                "gap_to_next_ns": "" if k.gap_to_next_ns is None else k.gap_to_next_ns,
                "overlap_to_next_ns": k.overlap_to_next_ns,
                "cpu_id": "" if k.cpu_id is None else k.cpu_id,
                "cpu_launch_name": k.cpu_launch_name,
                "phase": k.phase,
                "semantic_path": k.semantic_path,
                "semantic_name": k.semantic_name,
                "matched_rule_id": k.matched_rule_id,
                "relation": k.relation,
                "confidence": k.confidence,
                "cpu_chain_tail": k.cpu_chain_tail,
            })


def _structure_signature(node: Dict[str, Any], strip_re: re.Pattern[str]) -> str:
    base = strip_re.sub('', node.get('display_name', ''))
    child_sigs = tuple(_structure_signature(c, strip_re) for c in node.get('children', []))
    return f"{base}({','.join(child_sigs)})"


def _merge_repeated_nodes(node: Dict[str, Any], min_repeat: int,
                          strip_re: re.Pattern[str]) -> None:
    for child in node.get('children', []):
        _merge_repeated_nodes(child, min_repeat, strip_re)
    children = node.get('children', [])
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
            rep = children[i]
            rep['repeat_count'] = run_length
            for k in range(i + 1, j):
                rep['total_kernel_count'] = rep.get('total_kernel_count', 0) + children[k].get('total_kernel_count', 0)
                rep['total_gpu_ns'] = rep.get('total_gpu_ns', 0) + children[k].get('total_gpu_ns', 0)
                rep['self_kernel_count'] = rep.get('self_kernel_count', 0) + children[k].get('self_kernel_count', 0)
                rep['self_gpu_ns'] = rep.get('self_gpu_ns', 0) + children[k].get('self_gpu_ns', 0)
            base = strip_re.sub('', rep['display_name'])
            rep['display_name'] = f"{base} ×{run_length}"
            new_children.append(rep)
        else:
            new_children.extend(children[i:j])
        i = j
    node['children'] = new_children


def _convert_node_for_render(node: Dict[str, Any]) -> Dict[str, Any]:
    is_module = node.get('category') == 'module'
    name = node.get('name', '')
    return {
        'name': f'nn.Module: {name}' if is_module else name,
        'display_name': name,
        'is_anchor': node.get('category') in ('phase', 'root'),
        'anchor_reason': '',
        'self_kernel_count': node.get('self_kernel_count', 0),
        'self_gpu_ns': node.get('self_gpu_sum_ns', 0),
        'total_kernel_count': node.get('total_kernel_count', 0),
        'total_gpu_ns': node.get('total_gpu_sum_ns', 0),
        'children': [_convert_node_for_render(c) for c in node.get('children', [])],
        'kernels': node.get('kernels', []),
    }


def write_html(path: str, tree_json: Dict[str, Any], schema: SchemaConfig,
               include_kernels: bool = False) -> None:
    ensure_parent_dir(path)
    root_converted = _convert_node_for_render(tree_json['root'])
    _merge_repeated_nodes(root_converted,
                          schema.html_render.merge_repeated_min,
                          schema.html_render.trailing_digit_strip_regex)
    render_json: Dict[str, Any] = {
        'schema_version': 1,
        'kind': 'nograph_semantic_tree',
        'decode_idx': tree_json.get('decode_idx'),
        'window': dict(tree_json.get('window', {})),
        'stats': dict(tree_json.get('stats', {})),
        'root': root_converted,
    }
    render_json['stats'].setdefault('final_tree_nodes', '?')
    marker_info = tree_json.get('marker') or {}
    render_json['window'].setdefault('marker_name', marker_info.get('marker_name', '?'))
    sys.path.insert(0, str(Path(__file__).parent))
    from render_steiner_tree import render_html  # external renderer, unchanged
    html = render_html(render_json)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("trace", nargs="?", help="pt.trace.json.gz path")
    p.add_argument("--schema", required=True,
                   help="path to schema.json (model architecture + semantic rules)")
    p.add_argument("--platform", required=True,
                   help="path to platform.json (runtime/hardware naming)")
    p.add_argument("--decode-idx", type=int, default=10,
                   help="which decode token (1-indexed). Default 10.")
    p.add_argument("--decode-marker",
                   help="explicit decode GPU range marker name")
    p.add_argument("--out-prefix",
                   help="output file prefix; default derived from trace name. "
                        "If given, used verbatim (no auto output_dir prefix).")
    p.add_argument("--out-dir",
                   help="directory for outputs when --out-prefix is not given. "
                        "Default: schema.output_naming.output_dir (typically 'output').")
    p.add_argument("--trace-processor-shell",
                   help="path to perfetto trace_processor_shell. Can also be set by TRACE_PROCESSOR_SHELL.")
    p.add_argument("--list-markers", action="store_true",
                   help="list candidate marker names and exit")
    p.add_argument("--html-include-kernels", action="store_true",
                   help="include GPU kernels as leaves in the HTML")
    p.add_argument("--no-kernels-in-tree-json", action="store_true",
                   help="don't embed kernels in normalized_tree.json")
    args = p.parse_args()

    if not args.trace:
        p.error("trace is required unless you only want help")

    schema = load_schema(args.schema)
    platform = load_platform(args.platform)

    tp = open_trace_processor(args.trace, trace_processor_shell=args.trace_processor_shell)
    try:
        if args.list_markers:
            list_marker_candidates(tp, platform)
            return
    finally:
        tp.close()

    if args.out_prefix:
        # User-specified: use verbatim, no auto dir prefix.
        prefix = args.out_prefix
    else:
        out_dir = args.out_dir if args.out_dir is not None else schema.output_naming.output_dir
        base_prefix = default_prefix(args.trace, args.decode_idx, schema)
        prefix = str(Path(out_dir) / base_prefix) if out_dir else base_prefix
    tree_json, template_json, kernels = probe(
        args.trace,
        schema=schema,
        platform=platform,
        decode_idx=args.decode_idx,
        decode_marker=args.decode_marker,
        include_kernels_in_tree_json=not args.no_kernels_in_tree_json,
        trace_processor_shell=args.trace_processor_shell,
    )

    on = schema.output_naming
    tree_path = on.tree_path_template.format(prefix=prefix)
    template_path = on.template_path_template.format(prefix=prefix)
    csv_path = on.csv_path_template.format(prefix=prefix)
    html_path = on.html_path_template.format(prefix=prefix)

    write_json(tree_path, tree_json)
    write_json(template_path, template_json)
    write_kernel_csv(csv_path, kernels, args.decode_idx)
    write_html(html_path, tree_json, schema,
               include_kernels=args.html_include_kernels)

    for path in (tree_path, template_path, csv_path, html_path):
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"[+] wrote {path} ({size_mb:.2f} MB)")

    print("[*] Next:")
    print(f"    open {html_path}")
    print(f"    inspect {csv_path}")


if __name__ == "__main__":
    main()
