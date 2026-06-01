#!/usr/bin/env python3
"""probe_trace.py — upgraded Hygon/H100 single-trace decode probe.

This is the phase-1 migration of the generic improvements learned from the
Ascend probe back to the older CUDA/HIP Perfetto-trace probe.

Input:
  PyTorch profiler trace readable by Perfetto, usually *.pt.trace.json.gz.

Output:
  <prefix>.normalized_tree.json
  <prefix>.cpu_stack.normalized_tree.json
  <prefix>.semantic_template.json
  <prefix>.kernel_timeline.csv
  <prefix>.semantic_tree.html
  <prefix>.cpu_stack.html
  <prefix>.boundary_debug.csv
  <prefix>.unaccounted_gpu_activity.json

The key platform-specific pieces are still Perfetto/CUDA-HIP:
  * GPU slices come from Perfetto slice categories such as kernel/gpu_memcpy.
  * GPU->CPU mapping uses Perfetto flow first, then correlation/external id.
  * Decode marker aliases come from platform.json.

The generic upgrades are:
  * full CPU stacks are interned and preserved;
  * every kernel carries cpu_stack_id, shape summary, correlation id, and CPU tail;
  * exact decode window can be resolved by semantic phase transition around
    marker boundaries rather than blindly trusting marker interval;
  * cpu_stack.normalized_tree.json is built from complete stack-derived context;
  * boundary_debug.csv and unaccounted_gpu_activity.json are emitted.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict, OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Reuse the proven v1 adapter/rule-engine helpers.  This script is intentionally
# an additive upgraded entrypoint; clear_probe.py is kept for compatibility.
import clear_probe as v1


# ---------------------------------------------------------------------------
# Small generic helpers
# ---------------------------------------------------------------------------


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def write_json(path: str, obj: Any, *, pretty: bool = False) -> None:
    ensure_parent(path)
    with open(path, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        else:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def safe_int(v: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if v is None:
            return default
        # pandas may return float-ish ints.
        if isinstance(v, float) and v != v:
            return default
        return int(v)
    except Exception:
        return default


def compact_source_name(name: str) -> str:
    return v1.compact_cpu_name(str(name or ""))


def is_interesting_source_frame(name: str) -> bool:
    low = str(name or "").lower()
    if name.startswith("nn.Module: "):
        return True
    # Keep key project/user source frames and operator wrappers; drop most
    # generic Python wrappers.  This is deliberately conservative.
    needles = [
        "/vllm", "vllm/", "vllm_", "flag_gems", "gems", "sampler", "sample",
        "model_runner", "executor", "worker", "logits", "apply_temperature",
        "_prepare_inputs", "_build_attention_metadata", "_model_forward",
        "true_divide", "torch/_ops", "torch/ops", "aten::", "hip", "cuda",
    ]
    return any(x in low for x in needles)


def stable_key(s: str) -> str:
    s = str(s or "").strip()
    s = re.sub(r"^nn\.Module:\s*", "", s)
    s = re.sub(r"[^A-Za-z0-9_./:-]+", "_", s)
    if len(s) > 120:
        s = s[:120]
    return s or "unknown"


# ---------------------------------------------------------------------------
# Perfetto args / shape extraction
# ---------------------------------------------------------------------------


def query_slice_args(tp: v1.TraceProcessor, slice_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    if not slice_ids:
        return {}
    out: Dict[int, Dict[str, Any]] = defaultdict(dict)
    B = 800
    for i in range(0, len(slice_ids), B):
        ids = ",".join(str(int(x)) for x in slice_ids[i:i+B])
        try:
            df = v1.query_df(tp, f"""
                SELECT s.id AS slice_id, a.key,
                       COALESCE(CAST(a.string_value AS STRING),
                                CAST(a.int_value AS STRING),
                                CAST(a.real_value AS STRING)) AS value
                FROM slice s
                JOIN args a ON s.arg_set_id = a.arg_set_id
                WHERE s.id IN ({ids})
            """)
        except Exception:
            continue
        for _, r in df.iterrows():
            sid = safe_int(r.get("slice_id"))
            if sid is None:
                continue
            out[sid][str(r.get("key"))] = r.get("value")
    return dict(out)


def get_first_arg(args: Dict[str, Any], candidates: Iterable[str]) -> Any:
    lower = {str(k).lower(): k for k in args}
    for c in candidates:
        if c in args and args[c] not in (None, "", "NULL", "None"):
            return args[c]
        cl = c.lower()
        if cl in lower and args[lower[cl]] not in (None, "", "NULL", "None"):
            return args[lower[cl]]
    return None


def extract_shape_from_args(args: Dict[str, Any], cpu_args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Best-effort shape extraction across PyTorch/CUDA/HIP trace variants.

    H100/Hygon traces are not as regular as Ascend DB task-info tables.  We
    preserve raw shape-ish args and synthesize a short summary when possible.
    """
    cpu_args = cpu_args or {}
    merged = dict(cpu_args)
    merged.update(args or {})

    shape_keys = [k for k in merged if any(x in str(k).lower() for x in [
        "shape", "dims", "dim", "dtype", "input", "output", "tensor", "size", "stride"
    ])]
    raw = {k: merged[k] for k in shape_keys[:32] if merged.get(k) not in (None, "", "NULL", "None")}

    input_shapes = get_first_arg(merged, [
        "Input Dims", "Input dims", "args.Input Dims", "input_shapes", "Input Shapes",
        "Input shape", "args.input_shapes", "inputs", "Input Tensor Dims"
    ])
    output_shapes = get_first_arg(merged, [
        "Output Dims", "Output dims", "output_shapes", "Output Shapes", "Output shape",
        "args.output_shapes", "outputs", "Output Tensor Dims"
    ])
    input_dtypes = get_first_arg(merged, ["Input type", "Input Dtype", "input_dtypes", "Input types"])
    output_dtypes = get_first_arg(merged, ["Output type", "Output Dtype", "output_dtypes", "Output types"])

    summary_parts: List[str] = []
    if input_shapes:
        summary_parts.append(str(input_shapes))
    if output_shapes:
        summary_parts.append("→" + str(output_shapes))
    if not summary_parts and raw:
        # Concise fallback: show first shape-ish key=value.
        for k, v in raw.items():
            if "shape" in str(k).lower() or "dims" in str(k).lower():
                summary_parts.append(f"{k}={v}")
                break
    summary = " ".join(summary_parts)

    shape = {
        "source": "perfetto_args" if raw else "none",
        "summary": summary,
        "input_shapes": "" if input_shapes is None else str(input_shapes),
        "output_shapes": "" if output_shapes is None else str(output_shapes),
        "input_dtypes": "" if input_dtypes is None else str(input_dtypes),
        "output_dtypes": "" if output_dtypes is None else str(output_dtypes),
        "raw": raw,
    }
    return shape


def extract_correlation(args: Dict[str, Any]) -> Optional[int]:
    for key in v1.PERFETTO_CORRELATION_KEYS:
        if key in args:
            return safe_int(args[key])
        # Some trace exports strip the args. prefix.
        k2 = key.split(".", 1)[-1]
        if k2 in args:
            return safe_int(args[k2])
    return None


# ---------------------------------------------------------------------------
# CPU stack preservation
# ---------------------------------------------------------------------------


def serialize_cpu_frame(s: v1.RawSlice) -> Dict[str, Any]:
    nm = str(s.name)
    loc = None
    m = re.search(r"(?P<file>[^\s]+\.(?:py|cc|cpp|c|cu|hip|hpp|h))\((?P<line>\d+)\):\s*(?P<func>.+)$", nm)
    if m:
        loc = {"file": m.group("file"), "line": int(m.group("line")), "func": m.group("func").strip()}
    return {
        "id": s.id,
        "parent_id": s.parent_id,
        "name": nm,
        "compact": compact_source_name(nm),
        "ts": s.ts,
        "dur_ns": s.dur_ns,
        "depth": s.depth,
        "is_module": v1.is_module_name(nm),
        "source_location": loc,
    }


def intern_cpu_stacks(kernels: List[v1.KernelRecord], slices: Dict[int, v1.RawSlice]) -> Dict[str, List[Dict[str, Any]]]:
    sig_to_id: Dict[Tuple[int, ...], int] = {}
    stacks: Dict[str, List[Dict[str, Any]]] = OrderedDict()
    next_id = 0
    for k in kernels:
        if k.cpu_id is None or k.cpu_id not in slices:
            setattr(k, "cpu_stack_id", None)
            setattr(k, "cpu_stack_compact", [])
            continue
        chain = v1.build_chain(k.cpu_id, slices)
        sig = tuple(s.id for s in chain)
        if sig not in sig_to_id:
            sid = next_id
            next_id += 1
            sig_to_id[sig] = sid
            stacks[str(sid)] = [serialize_cpu_frame(s) for s in chain]
        else:
            sid = sig_to_id[sig]
        setattr(k, "cpu_stack_id", sid)
        setattr(k, "cpu_stack_compact", [compact_source_name(s.name) for s in chain])
    return stacks


# Monkey-patch KernelRecord.to_dict for this upgraded entrypoint.  The original
# file is intentionally kept unchanged for compatibility.
def upgraded_kernel_to_dict(self: v1.KernelRecord) -> Dict[str, Any]:
    base = {
        "gpu_id": self.gpu_id,
        "name": self.name,
        "ts": self.ts,
        "dur_ns": self.dur_ns,
        "original_dur_ns": getattr(self, "original_dur_ns", self.dur_ns),
        "is_communication": bool(getattr(self, "is_communication", False)),
        "communication_reason": getattr(self, "communication_reason", ""),
        "category": self.category,
        "stream": self.stream,
        "cpu_id": self.cpu_id,
        "cpu_launch_name": self.cpu_launch_name,
        "cpu_stack_id": getattr(self, "cpu_stack_id", None),
        "correlation_id": getattr(self, "correlation_id", None),
        "shape": getattr(self, "shape", {}),
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
    return base

v1.KernelRecord.to_dict = upgraded_kernel_to_dict  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Classification / CPU-stack tree specs
# ---------------------------------------------------------------------------


def classify_kernel(k: v1.KernelRecord, slices: Dict[int, v1.RawSlice], module_infos: Dict[int, v1.ModuleInfo],
                    engine: v1.RuleEngine, schema: v1.SchemaConfig) -> List[Tuple[str, str, str, Optional[str], Optional[str]]]:
    if k.cpu_id is None or k.cpu_id not in slices:
        k.phase = schema.tree.unmapped_bucket_name
        k.cpu_launch_name = "<no-flow>"
        k.cpu_chain_tail = "<no-flow>"
        k.matched_rule_id = "__unmapped__"
        return [(schema.tree.unmapped_bucket_key, schema.tree.unmapped_bucket_name, "bucket", None, None)]
    chain = v1.build_chain(k.cpu_id, slices)
    cpu_slice = slices[k.cpu_id]
    k.cpu_launch_name = compact_source_name(cpu_slice.name)
    tail = [compact_source_name(s.name) for s in chain[-10:]]
    k.cpu_chain_tail = " > ".join(tail)
    mods = v1.module_infos_from_chain(chain, module_infos)
    result = engine.classify(chain, mods)
    k.phase = result.phase
    k.matched_rule_id = result.matched_rule_id
    setattr(k, "semantic_specs", result.specs)
    return result.specs


def is_stack_noise_frame(name: str, compact: str, platform: v1.PlatformConfig) -> bool:
    """Frames that should remain in cpu_stacks metadata but not in the visible tree."""
    low = str(name or "").lower()
    c = str(compact or "")
    if v1.is_cpu_wrapper(name, platform):
        return True
    if c in {
        "forward", "__call__", "_call_impl", "_wrapped_call_impl", "inner", "wrapper",
        "_bootstrap", "_bootstrap_inner", "run", "spawn_main", "_main", "<module>",
        "<string>(1): <module>", "_disabled_torch_function_impl",
    }:
        return True
    if any(x in low for x in [
        "threading.py(", "multiprocessing/", "usage/usage_lib.py", "death_pipe_monitor",
        "_report_usage_worker", "_report_continuous_usage", "worker_main",
    ]):
        return True
    return False


def is_high_value_source_frame(name: str, compact: str) -> bool:
    """Source frames worth keeping as semantic nodes in concise mode.

    Full stacks are always preserved in metadata. This whitelist only controls
    the visible cpu_stack tree, so it should keep stable semantic operations and
    avoid generic implementation plumbing like ATen dispatch chains.

    Rotary/RoPE is intentionally handled narrowly.  Earlier versions used a
    broad `"rotary" in frame_name` rule, which made the visible tree expand as:

      RotaryEmbedding -> forward_cuda -> rotary_embedding -> <built-in method>
        -> _C::rotary_embedding -> kernel

    That information is useful in the full cpu_stack metadata, but too noisy for
    the concise HTML view.  Keep the Python-level wrapper and selected FlagGems
    source frames; hide pybind/C extension plumbing unless source/verbose mode
    is requested.
    """
    low = str(name or "").lower()
    c = str(compact or "")
    allow = {
        "apply_temperature", "sample", "random_sample", "forward_native",
        "_apply_top_k_top_p_ascendc", "_apply_top_k_top_p", "unified_attention_with_output",
        "copy_to_gpu", "_prepare_inputs", "_build_attention_metadata",
        "_get_block_table_and_slot_mapping", "update_cos_sin", "compute_logits",
        "get_logits", "hidden_states", "_get_masked_input_and_mask", "get_masked_input_and_mask",
        # Stable Python-level rotary/RoPE source frames.  These are useful to
        # distinguish vendor vs FlagGems paths without showing pybind/C plumbing.
        "forward_cuda", "forward_oot", "apply_rotary_pos_emb",
        "rotary_embedding_flaggems", "gems_rope_forward",
    }
    if c in allow:
        # forward_cuda/forward_oot are generic names, so only keep them when the
        # file/path clearly belongs to rotary/RoPE.  Other modules do not need a
        # visible "forward_*" node in concise mode.
        if c in {"forward_cuda", "forward_oot"}:
            return ("rotary" in low or "rope" in low)
        return True
    if any(x in low for x in [
        "sample/sampler.py", "logits_processor", "logits", "attention.py",
        "topk", "topp",
    ]):
        return True
    return False


def is_generic_operator_plumbing(name: str, compact: str) -> bool:
    low = str(name or "").lower()
    c = str(compact or "")
    if c.startswith("aten::"):
        return True
    if c.startswith("cuda") or c.startswith("cu") or c.startswith("hip"):
        return True
    if "<built-in function" in low or "<built-in method" in low:
        return True
    if c in {"apply", "default_unquantized_gemm", "__torch_function__", "_C::rotary_embedding"}:
        return True
    if any(x in low for x in [
        "torch/_ops", "torch/ops", "torch/nn/functional", "model_executor/parameter.py",
        "model_executor/layers/utils.py", "model_executor/layers/linear.py",
    ]):
        return True
    return False


def is_vllm_source_frame(name: str, compact: str) -> bool:
    """Real vLLM-family source frames used as *soft* semantic breadcrumbs.

    v16 fix: Hygon/Gems traces often use plugin paths such as `vllm_fl/...`
    rather than the upstream `vllm/...` path.  Earlier versions only matched
    `/vllm/` or a short compact allowlist, so real frames like
    `vllm_fl/worker/model_runner.py(...): _prepare_input_ids` were present in
    full cpu_stacks but absent from the visible module_vllm tree.

    These frames are still not schema-protected buckets: they participate in
    compare-aware joint pruning and can disappear when they are unary wrappers.
    nn.Module frames remain the hard anchors.
    """
    low = str(name or "").lower()
    c = str(compact or "")
    if is_generic_operator_plumbing(name, compact):
        return False
    # Match upstream vllm and plugin/vendor forks: vllm_fl, vllm_ascend,
    # vllm_plugin, vllm_vendor, etc.  We intentionally require a source-like
    # path or Python frame shape so random kernel names containing "vllm" are
    # not enough by themselves.
    if "vllm" in low and (".py" in low or "/vllm" in low or "vllm_" in low or "vllm-" in low):
        return True
    # Some stable operation names may arrive compacted without a path.  Keep
    # prepare/embed/copy skeletons soft-visible; joint prune decides whether
    # they survive in compare.
    return c in {
        "copy_to_gpu", "_prepare_inputs", "_prepare_input_ids",
        "_build_attention_metadata", "input_and_attention_metadata",
        "commit_block_table", "commit_slot_mapping",
        "embed_input_ids", "_embed_input_ids", "_embed_text_input_ids",
        "update_cos_sin", "compute_logits", "get_logits",
        "sample", "sample_tokens", "apply_temperature", "random_sample",
        "forward_native", "forward_cuda", "forward_oot",
        "get_masked_input_and_mask", "_get_masked_input_and_mask",
        "_compute_routing", "fused_topk", "vllm_topk_softmax", "topk_softmax",
    }


def trim_chain_to_decode_context(chain: List[v1.RawSlice]) -> List[v1.RawSlice]:
    """Drop process/thread prologue before the actual vLLM worker loop.

    In Perfetto traces, CPU ancestor chains often start at Python threading or
    death_pipe_monitor frames. If we append the whole chain after the semantic
    path, the visible tree becomes:

      Module -> death_pipe_monitor -> worker_busy_loop -> execute_model -> Module -> ...

    which duplicates the real semantic path. Keep only frames after the
    innermost busy loop anchor; the full original stack still remains in
    cpu_stacks metadata for agent/source analysis.
    """
    last_busy = None
    for i, s in enumerate(chain):
        c = compact_source_name(str(s.name))
        low = str(s.name).lower()
        if c in {"worker_busy_loop", "run_busy_loop"} or "worker_busy_loop" in low or "run_busy_loop" in low:
            last_busy = i
    if last_busy is not None:
        return chain[last_busy + 1:]
    return chain


def append_cpu_stack_specs(base_specs: List[Tuple[str, str, str, Optional[str], Optional[str]]],
                           chain: List[v1.RawSlice],
                           module_infos: Dict[int, v1.ModuleInfo],
                           schema: v1.SchemaConfig,
                           platform: v1.PlatformConfig,
                           *, detail: str = "module_vllm") -> List[Tuple[str, str, str, Optional[str], Optional[str]]]:
    """Build a robust stack-derived visible path.

    v3 appended almost every "interesting" frame from the full CPU ancestor
    chain. That preserved information, but made the HTML much deeper than the
    Ascend cpu_stack view because Perfetto stacks include process/thread
    prologue and long ATen/CUDA dispatch tails.

    This function keeps full stacks in metadata but makes the visible tree
    concise by default:
      * start from the schema/module semantic path;
      * trim process/thread prologue before worker_busy_loop;
      * skip frames already represented by the base semantic path;
      * skip generic Python/ATen/CUDA plumbing;
      * keep only stable semantic source frames in concise mode.

    Use --cpu-stack-tree-detail source/verbose if you need a deeper visible
    source tree. The complete original stack is always available through
    cpu_stack_id in the JSON.
    """
    # detail="module" and detail="module_vllm" deliberately do NOT start from
    # schema protected specs.  Schema/rule classification is still kept on each
    # kernel for boundary / phase decisions, but the visible tree is built from
    # real CPU stack frames.  In module_vllm mode, nn.Module nodes are hard
    # anchors while vLLM source frames are soft breadcrumbs that participate in
    # later pruning.
    out: List[Tuple[str, str, str, Optional[str], Optional[str]]] = [] if detail in {"module", "module_vllm"} else list(base_specs)
    seen_labels = [x[1] for x in out]
    seen_norm = {re.sub(r"^nn\.Module:\s*", "", str(x)).lower() for x in seen_labels}

    def add(key: str, name: str, cat: str, canonical: Optional[str] = None, orig: Optional[str] = None) -> None:
        norm = re.sub(r"^nn\.Module:\s*", "", str(name)).lower()
        if seen_labels and seen_labels[-1] == name:
            return
        if norm in seen_norm:
            return
        out.append((key, name, cat, canonical, orig))
        seen_labels.append(name)
        seen_norm.add(norm)

    trimmed = trim_chain_to_decode_context(chain)

    # In source/verbose modes append only frames after the deepest base label
    # already observed in the chain. This avoids duplicating execute_model,
    # model_forward, model modules, etc. after the schema path.
    #
    # IMPORTANT: detail="module"/"module_vllm" intentionally starts from an empty
    # visible path and must scan the *whole* CPU chain for nn.Module frames.  A
    # previous version still used base_specs to compute start_idx; for
    # model_forward kernels the deepest base label could be inside the module
    # stack, so all real modules were skipped and almost every kernel collapsed
    # into `other_gpu`.
    start_idx = 0
    if detail not in {"module", "module_vllm"} and base_specs:
        base_names = [str(x[1]) for x in base_specs]
        best = -1
        for i, s in enumerate(trimmed):
            compact = compact_source_name(str(s.name))
            labels = {compact, str(s.name)}
            if v1.is_module_name(str(s.name)) and s.id in module_infos:
                mi = module_infos[s.id]
                labels.add(mi.original_name)
                labels.add("nn.Module: " + mi.original_name)
            if any(b in labels for b in base_names):
                best = i
        if best >= 0:
            start_idx = best + 1

    for s in trimmed[start_idx:]:
        nm = str(s.name)
        compact = compact_source_name(nm)
        if is_stack_noise_frame(nm, compact, platform):
            continue

        if v1.is_module_name(nm):
            if s.id in module_infos:
                m = module_infos[s.id]
                seg = m.canonical_segment(schema.always_index_canonical_types)
                add(seg, m.original_name, "module", m.canonical_type, m.original_name)
                continue
            # Fallback for traces where the full cpu_stack contains an nn.Module
            # frame but that slice was not part of the module_infos index.  This
            # should be rare, but it is safer than dropping the module and sending
            # the kernel to `other_gpu`.  Use the explicit suffix as the segment
            # index when present so repeated layers do not collapse together.
            parsed = v1.parse_module_name(nm, schema.module_alias_rules)
            if parsed:
                module_name, _base, suffix, canonical = parsed
                if suffix is not None:
                    seg = f"{canonical}[{suffix}]"
                else:
                    seg = canonical
                add(seg, module_name, "module", canonical, module_name)
                continue

        if detail == "module":
            # Module-only visible tree: no schema protected phases/buckets and no
            # source implementation frames. Kernels without nn.Module context attach
            # directly under decode_token.
            continue

        if detail == "module_vllm":
            # Hybrid visible tree: keep real nn.Module frames as non-compressible
            # semantic anchors, and keep real vLLM source frames as *soft* anchors.
            # These source nodes are expected to be pruned by compare-aware joint
            # pruning when they form unary implementation chains.
            if is_vllm_source_frame(nm, compact):
                add(stable_key(compact), compact, "source", None, nm)
            continue

        if detail == "concise":
            # Avoid retaining pybind/ATen/CUDA plumbing just because it contains a
            # high-value token such as topk/rotary.
            if is_generic_operator_plumbing(nm, compact):
                continue
            if is_high_value_source_frame(nm, compact):
                add(stable_key(compact), compact, "source", None, nm)
            continue

        if detail == "source":
            if is_generic_operator_plumbing(nm, compact):
                continue
            if is_interesting_source_frame(nm) or is_high_value_source_frame(nm, compact):
                add(stable_key(compact), compact, "source", None, nm)
            continue

        # verbose: still skip pure noise/wrappers, but keep operator plumbing.
        if is_interesting_source_frame(nm) or is_high_value_source_frame(nm, compact) or compact.startswith("aten::"):
            add(stable_key(compact), compact, "source", None, nm)

    return out

def build_tree_from_kernels(kernels: List[v1.KernelRecord], schema: v1.SchemaConfig, decode_idx: int,
                            *, specs_attr: str) -> v1.SemanticNode:
    root = v1.SemanticNode(key=schema.tree.root_key, name=schema.tree.root_name,
                           category="root", path=schema.tree.root_key)
    decode_key = schema.tree.decode_key_template.format(decode_idx=decode_idx)
    decode_name = schema.tree.decode_name_template.format(decode_idx=decode_idx)
    decode = root.add_child(decode_key, decode_name, "phase")
    for k in sorted(kernels, key=lambda x: (x.ts, x.gpu_id)):
        specs = getattr(k, specs_attr, None)
        if not specs:
            specs = [(schema.tree.fallback_bucket_key, schema.tree.fallback_bucket_name, "bucket", None, None)]
        attach_kernel_ordered(decode, specs, k)
    v1.finalize_stats(root)
    return root



# ---------------------------------------------------------------------------
# Communication classification / accounting
# ---------------------------------------------------------------------------

COMM_KERNEL_RE = re.compile(
    r"(nccl|rccl|hccl|all[_ ]?gather|all[_ ]?reduce|reduce[_ ]?scatter|"
    r"broadcast|sendrecv|cross_device_reduce|vllm::all_reduce|vllm::all_gather)",
    re.IGNORECASE,
)

COMM_STACK_HINTS = (
    "distributed/communication_op.py",
    "distributed/parallel_state.py",
    "distributed/device_communicators",
    "tensor_model_parallel_all_reduce",
    "tensor_model_parallel_all_gather",
    "custom_all_reduce",
    "custom_all_gather",
    "_all_reduce_out_place",
    "_all_gather",
    "vllm::all_reduce",
    "vllm::all_gather",
    "_c_custom_ar::all_reduce",
    "_c_custom_ar::all_gather",
    "nccl",
    "rccl",
)


def is_communication_kernel(k: v1.KernelRecord, slices: Dict[int, v1.RawSlice]) -> Tuple[bool, str]:
    """Best-effort communication classification for CUDA/HIP Perfetto traces.

    Perfetto/PyTorch traces usually do not carry an Ascend-like dedicated
    COMMUNICATION_TASK_INFO table.  The most stable signal we have is therefore
    the CPU stack path (vLLM distributed communication frames).  Kernel-name
    matching is a conservative fallback and deliberately avoids plain 'reduce'
    so cublas splitKreduce kernels are not treated as communication.
    """
    name = str(k.name or "")
    if COMM_KERNEL_RE.search(name):
        return True, "kernel_name"
    sid = getattr(k, "cpu_stack_id", None)
    stack_frames = []
    if k.cpu_id is not None and k.cpu_id in slices:
        stack_frames = [str(s.name or "") for s in v1.build_chain(k.cpu_id, slices)]
    elif getattr(k, "cpu_stack_compact", None):
        stack_frames = [str(x) for x in getattr(k, "cpu_stack_compact")]
    hay = "\n".join(stack_frames).lower()
    for hint in COMM_STACK_HINTS:
        if hint.lower() in hay:
            return True, "cpu_stack"
    return False, ""


def apply_communication_accounting(kernels: List[v1.KernelRecord],
                                   slices: Dict[int, v1.RawSlice],
                                   *, include_comm_time: bool) -> Dict[str, Any]:
    """Mark communication kernels and optionally zero their accounted duration.

    We keep communication kernels visible by default but set dur_ns=0 for tree
    accounting.  The original raw GPU duration is preserved as original_dur_ns.
    Pass --include-comm-time to account their duration normally.
    """
    total_raw = 0
    count = 0
    by_reason: Counter[str] = Counter()
    examples: List[Dict[str, Any]] = []
    for k in kernels:
        raw = int(getattr(k, "original_dur_ns", k.dur_ns) or 0)
        setattr(k, "original_dur_ns", raw)
        is_comm, reason = is_communication_kernel(k, slices)
        setattr(k, "is_communication", bool(is_comm))
        setattr(k, "communication_reason", reason)
        if is_comm:
            count += 1
            total_raw += raw
            by_reason[reason or "unknown"] += 1
            if len(examples) < 20:
                examples.append({
                    "gpu_id": k.gpu_id,
                    "name": k.name,
                    "stream": k.stream,
                    "original_dur_ns": raw,
                    "reason": reason,
                    "cpu_chain_tail": k.cpu_chain_tail,
                })
            if not include_comm_time:
                k.dur_ns = 0
        else:
            k.dur_ns = raw
    return {
        "include_comm_time": include_comm_time,
        "communication_kernel_count": count,
        "communication_raw_dur_ns": total_raw,
        "communication_raw_dur_ms": round(total_raw / 1e6, 6),
        "by_reason": dict(by_reason),
        "examples": examples,
    }


# ---------------------------------------------------------------------------
# Order-preserving tree attachment
# ---------------------------------------------------------------------------


def _logical_node_key(key: str) -> str:
    return str(key).split("#seg", 1)[0]


def _add_ordered_child(parent: v1.SemanticNode,
                       logical_key: str,
                       name: str,
                       category: str,
                       canonical_type: Optional[str] = None,
                       original_name: Optional[str] = None) -> v1.SemanticNode:
    """Add/reuse a child only when it is contiguous in event order.

    Normal tree construction merges all children with the same key under a
    parent.  That breaks timeline order for patterns like:

      copy_to_gpu, direct_kernel, copy_to_gpu

    because the two copy_to_gpu groups become one child whose first_ts sorts
    before the direct kernel.  Here a repeated logical child after an intervening
    different event becomes copy_to_gpu#seg2 while retaining the display name.
    """
    last_key = getattr(parent, "_last_child_logical_key", None)
    last_child = getattr(parent, "_last_child_node", None)
    if last_key == logical_key and isinstance(last_child, v1.SemanticNode):
        child = last_child
    else:
        counts: Dict[str, int] = getattr(parent, "_logical_child_counts", {})
        n = counts.get(logical_key, 0) + 1
        counts[logical_key] = n
        setattr(parent, "_logical_child_counts", counts)
        physical_key = logical_key if n == 1 and logical_key not in parent.children else f"{logical_key}#seg{n}"
        while physical_key in parent.children:
            n += 1
            counts[logical_key] = n
            physical_key = f"{logical_key}#seg{n}"
        child = parent.add_child(physical_key, name, category, canonical_type, original_name)
        setattr(child, "logical_key", logical_key)
    setattr(parent, "_last_child_logical_key", logical_key)
    setattr(parent, "_last_child_node", child)
    if original_name and original_name not in child.original_names:
        child.original_names.append(original_name)
    if canonical_type and not child.canonical_type:
        child.canonical_type = canonical_type
    return child


def attach_kernel_ordered(root_decode: v1.SemanticNode,
                          specs: List[Tuple[str, str, str, Optional[str], Optional[str]]],
                          kernel: v1.KernelRecord) -> v1.SemanticNode:
    cur = root_decode
    for key, display, category, canonical_type, original_name in specs:
        cur = _add_ordered_child(cur, str(key), display, category, canonical_type, original_name)
    cur.kernels.append(kernel)
    # A direct kernel breaks child contiguity under this node.
    setattr(cur, "_last_child_logical_key", None)
    setattr(cur, "_last_child_node", None)
    kernel.semantic_path = cur.path
    kernel.semantic_name = cur.name
    return cur

# ---------------------------------------------------------------------------
# Decode boundary by semantic phase transition
# ---------------------------------------------------------------------------


def phase_label(k: v1.KernelRecord) -> str:
    rule = str(getattr(k, "matched_rule_id", "") or "")
    hay = " | ".join([
        str(k.name), str(k.phase), str(k.cpu_launch_name), str(k.cpu_chain_tail),
        " | ".join(getattr(k, "cpu_stack_compact", [])[-12:]),
    ]).lower()
    if rule in {"sampler", "hidden_select", "logits"}:
        return "PREV_TAIL"
    if any(x in hay for x in ["sample_tokens", "sampler", "topk", "topp", "hidden_select", "logits", "compute_logits", "apply_temperature"]):
        return "PREV_TAIL"
    if rule in {"prepare_inputs", "build_attention_metadata"}:
        return "CUR_START"
    if any(x in hay for x in ["_prepare_inputs", "prepare_inputs", "_build_attention_metadata", "build_attention_metadata", "copy_to_gpu", "input_and_attention_metadata"]):
        return "CUR_START"
    if rule == "model_forward" or "model_forward" in hay or "_model_forward" in hay:
        return "CUR_BODY"
    return "UNKNOWN"


def boundary_debug_rows(kernels: List[v1.KernelRecord], name: str, boundary_ts: int,
                        start_ns: int, end_ns: int, chosen: Optional[v1.KernelRecord]) -> List[Dict[str, Any]]:
    rows = [k for k in kernels if k.ts >= start_ns and k.ts < end_ns]
    rows.sort(key=lambda k: (abs(k.ts - boundary_ts), k.ts, k.gpu_id))
    rows = rows[:240]
    rows.sort(key=lambda k: (k.ts, k.gpu_id))
    chosen_id = chosen.gpu_id if chosen else None
    out = []
    for k in rows:
        out.append({
            "boundary": name,
            "boundary_ts": boundary_ts,
            "kernel_ts": k.ts,
            "distance_us": (k.ts - boundary_ts) / 1e3,
            "stream": k.stream,
            "label": phase_label(k),
            "chosen": int(chosen_id == k.gpu_id),
            "gpu_id": k.gpu_id,
            "kernel": k.name,
            "matched_rule_id": k.matched_rule_id,
            "cpu_launch_name": k.cpu_launch_name,
            "cpu_chain_tail": k.cpu_chain_tail,
        })
    return out


def resolve_boundary(kernels: List[v1.KernelRecord], boundary_name: str, boundary_ts: int,
                     lookaround_ns: int, *, after_ts: Optional[int] = None) -> Tuple[Optional[v1.KernelRecord], Dict[str, Any], List[Dict[str, Any]]]:
    start_ns = max(0, boundary_ts - lookaround_ns)
    end_ns = boundary_ts + lookaround_ns
    windowed = [k for k in kernels if k.ts >= start_ns and k.ts < end_ns and (after_ts is None or k.ts > after_ts)]
    by_stream: Dict[str, List[v1.KernelRecord]] = defaultdict(list)
    for k in windowed:
        by_stream[k.stream].append(k)
    for seq in by_stream.values():
        seq.sort(key=lambda k: (k.ts, k.gpu_id))

    candidates: List[Tuple[Tuple[Any, ...], v1.KernelRecord, v1.KernelRecord, str, bool, str]] = []
    for stream, seq in by_stream.items():
        labels = [phase_label(k) for k in seq]
        stream_time = sum(max(0, int(k.dur_ns)) for k in seq)
        for idx, lab in enumerate(labels):
            if lab not in {"CUR_START", "CUR_BODY"}:
                continue
            prev_found = False
            anchor_idx = idx
            j = idx - 1
            while j >= 0:
                if labels[j] == "PREV_TAIL":
                    prev_found = True
                    anchor_idx = j + 1
                    break
                if labels[j] in {"UNKNOWN", "CUR_START", "CUR_BODY"}:
                    anchor_idx = j
                    j -= 1
                    continue
                break
            while anchor_idx < len(seq) and after_ts is not None and seq[anchor_idx].ts <= after_ts:
                anchor_idx += 1
            if anchor_idx >= len(seq):
                continue
            anchor = seq[anchor_idx]
            if phase_label(anchor) == "PREV_TAIL":
                continue
            after_current = sum(1 for x in labels[idx:min(len(labels), idx+24)] if x in {"CUR_START", "CUR_BODY"})
            before_prev = sum(1 for x in labels[max(0, idx-24):idx] if x == "PREV_TAIL")
            decisive_is_prepare = lab == "CUR_START"
            quality = 0
            if not prev_found:
                quality += 4
            if not decisive_is_prepare:
                quality += 2
            sparse_penalty = 0 if len(seq) >= 4 else 1
            distance = abs(anchor.ts - boundary_ts)
            score = (quality, sparse_penalty, distance, -after_current, -before_prev, -stream_time, stream, anchor.ts, anchor.gpu_id)
            confidence = "strong_prev_to_prepare" if prev_found and decisive_is_prepare else ("prepare_near_boundary" if decisive_is_prepare else "weak_model_forward_transition")
            candidates.append((score, anchor, seq[idx], stream, prev_found, confidence))
    if not candidates:
        dbg = boundary_debug_rows(kernels, boundary_name, boundary_ts, start_ns, end_ns, None)
        return None, {"confidence": "not_found", "boundary_ts": boundary_ts}, dbg
    candidates.sort(key=lambda x: x[0])
    _, anchor, decisive, stream, prev_found, confidence = candidates[0]
    info = {
        "boundary": boundary_name,
        "boundary_ts": boundary_ts,
        "anchor_ts": anchor.ts,
        "anchor_kernel": anchor.name,
        "anchor_gpu_id": anchor.gpu_id,
        "stream": stream,
        "decisive_kernel": decisive.name,
        "decisive_label": phase_label(decisive),
        "prev_found": prev_found,
        "confidence": confidence,
    }
    dbg = boundary_debug_rows(kernels, boundary_name, boundary_ts, start_ns, end_ns, anchor)
    return anchor, info, dbg


def trim_by_phase_transition(kernels: List[v1.KernelRecord], marker_win: Dict[str, Any], lookaround_ms: float,
                             mode: str) -> Tuple[List[v1.KernelRecord], Dict[str, Any], List[Dict[str, Any]]]:
    raw_start = int(marker_win["ts_start"])
    raw_end = int(marker_win["ts_end"])
    if mode == "marker":
        return [k for k in kernels if raw_start <= k.ts < raw_end], {
            "method": "marker_interval", "ts_start": raw_start, "ts_end": raw_end,
            "current_start_anchor": None, "next_start_anchor": None,
        }, []
    look_ns = int(float(lookaround_ms) * 1e6)
    cur_anchor, cur_info, dbg1 = resolve_boundary(kernels, "current_start", raw_start, look_ns)
    nxt_anchor, nxt_info, dbg2 = resolve_boundary(kernels, "next_start", raw_end, look_ns, after_ts=(cur_anchor.ts if cur_anchor else None))
    if cur_anchor is None or nxt_anchor is None or nxt_anchor.ts <= cur_anchor.ts:
        # Safe fallback: original marker interval.  Boundary CSV still explains
        # why phase transition failed.
        return [k for k in kernels if raw_start <= k.ts < raw_end], {
            "method": "marker_interval_fallback",
            "ts_start": raw_start, "ts_end": raw_end,
            "current_start_anchor": cur_info, "next_start_anchor": nxt_info,
            "fallback_reason": "phase transition anchor not found or invalid",
        }, dbg1 + dbg2
    exact_start, exact_end = cur_anchor.ts, nxt_anchor.ts
    return [k for k in kernels if exact_start <= k.ts < exact_end], {
        "method": "phase_transition",
        "ts_start": exact_start, "ts_end": exact_end,
        "current_start_anchor": cur_info,
        "next_start_anchor": nxt_info,
        "raw_marker_ts_start": raw_start,
        "raw_marker_ts_end": raw_end,
    }, dbg1 + dbg2


# ---------------------------------------------------------------------------
# Trace slicing — extract decode-window GPU+CPU+flow into a small trace file
# ---------------------------------------------------------------------------

_LAUNCH_PREFIXES = (
    "cudaLaunchKernel", "cuLaunchKernel", "cuLaunchKernelEx",
    "hipLaunchKernel", "hipModuleLaunchKernel", "hipExtLaunchKernel",
    "hipExtModuleLaunchKernel",
    "cudaMemcpyAsync", "cudaMemsetAsync", "hipMemcpyAsync", "hipMemsetAsync",
)


def slice_via_open_tp(tp, output_path: str,
                      ts_start_ns: int, ts_end_ns: int,
                      pad_ms: float = 2.0,
                      max_cpu_tracks: int = 1,
                      max_gpu_tracks: int = 4,
                      verbose: bool = True) -> Dict[str, Any]:
    """Slice decode-window GPU kernels + CPU stacks + flows from an open tp."""
    import gzip as _gzip

    pad_ns = int(pad_ms * 1_000_000)
    lo = int(ts_start_ns) - pad_ns
    hi = int(ts_end_ns) + pad_ns

    def Q(sql):
        return tp.query(sql).as_pandas_dataframe()

    # 1. CPU launch track
    prefix_or = " OR ".join(f"s.name GLOB '{p}*'" for p in _LAUNCH_PREFIXES)
    df_cpu = Q(f"""
        SELECT t.utid AS utid, t.tid AS tid, p.pid AS pid,
               COALESCE(t.name,'?') AS tname, COALESCE(p.name,'?') AS pname, COUNT(*) AS cnt
        FROM slice s
        JOIN thread_track tt ON s.track_id = tt.id
        JOIN thread t        ON tt.utid    = t.utid
        JOIN process p       ON t.upid     = p.upid
        WHERE s.ts BETWEEN {lo} AND {hi} AND ({prefix_or})
        GROUP BY t.utid ORDER BY cnt DESC LIMIT {max_cpu_tracks}
    """)
    if df_cpu.empty:
        df_cpu = Q(f"""
            SELECT t.utid AS utid, t.tid AS tid, p.pid AS pid,
                   COALESCE(t.name,'?') AS tname, COALESCE(p.name,'?') AS pname, COUNT(*) AS cnt
            FROM slice s
            JOIN thread_track tt ON s.track_id = tt.id
            JOIN thread t        ON tt.utid    = t.utid
            JOIN process p       ON t.upid     = p.upid
            WHERE s.ts BETWEEN {lo} AND {hi}
            GROUP BY t.utid ORDER BY cnt DESC LIMIT {max_cpu_tracks}
        """)

    # 2. GPU tracks
    df_gpu = Q(f"""
        SELECT t.utid AS utid, t.tid AS tid, p.pid AS pid,
               COALESCE(t.name,'?') AS tname, COALESCE(p.name,'?') AS pname, COUNT(*) AS cnt
        FROM slice s
        JOIN thread_track tt ON s.track_id = tt.id
        JOIN thread t        ON tt.utid    = t.utid
        JOIN process p       ON t.upid     = p.upid
        WHERE s.ts BETWEEN {lo} AND {hi}
          AND (s.category LIKE '%kernel%' OR s.category LIKE '%gpu_%')
        GROUP BY t.utid ORDER BY cnt DESC LIMIT {max_gpu_tracks}
    """)

    # 3. Consolidate utids
    utids = list(df_cpu["utid"].astype(int)) + list(df_gpu["utid"].astype(int))
    cpu_utids = set(df_cpu["utid"].astype(int))
    utid_meta = {}
    for _, r in df_cpu.iterrows():
        utid_meta[int(r["utid"])] = (int(r["pid"]), int(r["tid"]), str(r["pname"]), str(r["tname"]))
    for _, r in df_gpu.iterrows():
        utid_meta[int(r["utid"])] = (int(r["pid"]), int(r["tid"]), str(r["pname"]), str(r["tname"]))

    if not utid_meta:
        if verbose:
            print("[slice] WARN: no tracks found in window")
        return {"events_out": 0, "slices_kept": 0, "flows_kept": 0}

    utids_csv = ",".join(str(u) for u in utids)

    # 4. All slices on chosen tracks
    df_s = Q(f"""
        SELECT s.id AS sid, s.ts AS ts, s.dur AS dur,
               COALESCE(s.name,'') AS name, COALESCE(s.category,'') AS cat,
               s.depth AS depth, t.utid AS utid
        FROM slice s
        JOIN thread_track tt ON s.track_id = tt.id
        JOIN thread t        ON tt.utid    = t.utid
        WHERE t.utid IN ({utids_csv}) AND s.ts BETWEEN {lo} AND {hi}
        ORDER BY s.ts, s.depth
    """)

    # 5. Flows
    df_f = Q(f"""
        WITH kept AS (
            SELECT s.id AS id, s.ts AS ts, t.utid AS utid
            FROM slice s
            JOIN thread_track tt ON s.track_id=tt.id
            JOIN thread t        ON tt.utid = t.utid
            WHERE t.utid IN ({utids_csv}) AND s.ts BETWEEN {lo} AND {hi}
        )
        SELECT ka.ts AS a_ts, ka.utid AS a_utid,
               kb.ts AS b_ts, kb.utid AS b_utid
        FROM flow f
        JOIN kept ka ON ka.id = f.slice_out
        JOIN kept kb ON kb.id = f.slice_in
    """)

    if verbose:
        print(f"[slice] {len(df_s)} slices, {len(df_f)} flows in decode window")

    # 6. Emit Chrome Trace Format
    NS_TO_US = 1000.0
    events = []

    pids = sorted({m[0] for m in utid_meta.values()})
    pid_pname = {pid: next((m[2] for m in utid_meta.values() if m[0] == pid), "") for pid in pids}
    for pid in pids:
        events.append({"ph": "M", "name": "process_name", "pid": pid, "tid": 0,
                       "args": {"name": pid_pname.get(pid, "")}})
    for u, (pid, tid, pn, tn) in utid_meta.items():
        events.append({"ph": "M", "name": "thread_name", "pid": pid, "tid": tid,
                       "args": {"name": tn}})

    for r in df_s.itertuples(index=False):
        pid, tid, _, _ = utid_meta[int(r.utid)]
        ts_ns = int(r.ts)
        dur_ns = int(r.dur) if (r.dur is not None and int(r.dur) >= 0) else 0
        events.append({"ph": "X", "pid": pid, "tid": tid,
                       "ts": ts_ns / NS_TO_US, "dur": dur_ns / NS_TO_US,
                       "name": str(r.name), "cat": str(r.cat)})

    for i, r in enumerate(df_f.itertuples(index=False)):
        a_ts, b_ts = int(r.a_ts), int(r.b_ts)
        a_utid, b_utid = int(r.a_utid), int(r.b_utid)
        if a_ts <= b_ts:
            src_ts, dst_ts, src_utid, dst_utid = a_ts, b_ts, a_utid, b_utid
        else:
            src_ts, dst_ts, src_utid, dst_utid = b_ts, a_ts, b_utid, a_utid
        s_pid, s_tid, _, _ = utid_meta[src_utid]
        d_pid, d_tid, _, _ = utid_meta[dst_utid]
        events.append({"ph": "s", "cat": "ac2g", "id": i, "name": "ac2g",
                       "pid": s_pid, "tid": s_tid, "ts": src_ts / NS_TO_US})
        events.append({"ph": "f", "cat": "ac2g", "id": i, "name": "ac2g",
                       "pid": d_pid, "tid": d_tid, "ts": dst_ts / NS_TO_US, "bp": "e"})

    out_data = {"displayTimeUnit": "ns", "traceEvents": events}
    p = str(output_path)
    opener = _gzip.open(p, "wt", encoding="utf-8") if p.endswith(".gz") else open(p, "w", encoding="utf-8")
    with opener as f:
        json.dump(out_data, f, separators=(",", ":"))

    out_size = Path(output_path).stat().st_size
    if verbose:
        print(f"[slice] wrote {output_path} ({out_size/1024:.1f} KiB, {len(events)} events)")
    return {"events_out": len(events), "slices_kept": int(len(df_s)), "flows_kept": int(len(df_f))}


# ---------------------------------------------------------------------------
# Main upgraded probe
# ---------------------------------------------------------------------------


def probe_trace(trace_path: str,
                schema: v1.SchemaConfig,
                platform: v1.PlatformConfig,
                decode_idx: int,
                decode_marker: Optional[str],
                include_kernels_in_tree_json: bool,
                boundary_mode: str,
                boundary_lookaround_ms: float,
                cpu_stack_tree_detail: str = "concise",
                include_comm_time: bool = False,
                trace_processor_shell: Optional[str] = None,
                slice_output_path: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], List[v1.KernelRecord], List[Dict[str, Any]], Dict[str, Any], Optional[Dict[str, Any]]]:
    print(f"[*] Loading trace: {trace_path}")
    tp = v1.open_trace_processor(trace_path, trace_processor_shell=trace_processor_shell)
    try:
        win = v1.find_decode_window(tp, platform, decode_idx=decode_idx, decode_marker=decode_marker)
        raw_start = int(win["ts_start"])
        raw_end = int(win["ts_end"])
        look_ns = int(float(boundary_lookaround_ms) * 1e6)
        query_start = raw_start if boundary_mode == "marker" else max(0, raw_start - look_ns)
        query_end = raw_end if boundary_mode == "marker" else raw_end + look_ns
        print(f"[+] marker window #{decode_idx}: [{raw_start}, {raw_end}) = {(raw_end - raw_start)/1e6:.3f} ms via {win['marker_name']!r}")
        if query_start != raw_start or query_end != raw_end:
            print(f"[+] expanded GPU search window: [{query_start}, {query_end}) = {(query_end-query_start)/1e6:.3f} ms")

        gpu_df = v1.query_gpu_slices(tp, platform, query_start, query_end)
        print(f"[+] GPU kernels/memops in search window: {len(gpu_df)}")
        if len(gpu_df) == 0:
            raise RuntimeError("No GPU kernel/memcpy/memset slices found in selected window.")

        gpu_by_id: Dict[int, v1.KernelRecord] = {}
        for _, r in gpu_df.iterrows():
            kid = int(r["id"])
            gpu_by_id[kid] = v1.KernelRecord(
                gpu_id=kid,
                name=str(r["name"]),
                ts=int(r["ts"]),
                dur_ns=int(r["dur"]) if r["dur"] is not None else 0,
                category=str(r["category"]),
                stream=str(r["stream_name"]),
            )

        # Args for correlation + best-effort shape.
        gpu_args = query_slice_args(tp, list(gpu_by_id.keys()))
        for gid, k in gpu_by_id.items():
            setattr(k, "correlation_id", extract_correlation(gpu_args.get(gid, {})))

        flows = v1.query_flows(tp, list(gpu_by_id.keys()))
        if flows is None or len(flows) == 0:
            print("[!] No CPU↔GPU flow edges; falling back to correlation/external id where possible.")
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
                    gpu_by_id[gid].relation = "perfetto_flow"

        orphan_gpu_ids = [gid for gid in gpu_by_id if gid not in gpu_to_cpu]
        if orphan_gpu_ids:
            print(f"[!] Orphan GPU slices without flow: {len(orphan_gpu_ids)}")
            corr_map = v1.assign_orphans_by_correlation(tp, orphan_gpu_ids)
            if corr_map:
                for gid, cid in corr_map.items():
                    if gid in gpu_by_id:
                        gpu_to_cpu[gid] = cid
                        gpu_by_id[gid].cpu_id = cid
                        gpu_by_id[gid].relation = "perfetto_correlation"
                print(f"[+] Correlation-based mapping: {len(corr_map)} additional kernels")
            orphan_gpu_ids = [gid for gid in gpu_by_id if gid not in gpu_to_cpu]
            if orphan_gpu_ids:
                print(f"[!] Remaining unmapped GPU slices: {len(orphan_gpu_ids)}")

        cpu_ids = sorted(set(gpu_to_cpu.values()))
        slices = v1.query_ancestor_slices(tp, cpu_ids)
        print(f"[+] unique CPU ancestor slices: {len(slices)}")
        module_infos = v1.build_module_infos(slices, schema)
        print(f"[+] nn.Module slices observed in GPU-bearing CPU ancestors: {len(module_infos)}")

        # The expensive work starts after the previous log line.  v7 queried
        # args for *all* ancestor slices (often 30k+) and built visible CPU-stack
        # paths for the whole lookaround window before trimming.  That made the
        # process look stuck at the nn.Module log.  Only CPU launch/anchor args
        # are needed for shape fallback, and visible cpu_stack paths are only
        # needed for exact-window kernels.
        t_stage = time.perf_counter()
        cpu_anchor_ids = sorted({cid for cid in gpu_to_cpu.values() if cid in slices})
        cpu_args = query_slice_args(tp, cpu_anchor_ids)
        print(f"[+] CPU anchor args loaded: {len(cpu_args)} launch slices (not all {len(slices)} ancestors) in {time.perf_counter()-t_stage:.2f}s")

        for gid, k in gpu_by_id.items():
            cargs = cpu_args.get(k.cpu_id, {}) if k.cpu_id is not None else {}
            setattr(k, "shape", extract_shape_from_args(gpu_args.get(gid, {}), cargs))

        kernels_all = list(gpu_by_id.values())
        v1.compute_global_kernel_gaps(kernels_all, query_start)

        t_stage = time.perf_counter()
        engine = v1.RuleEngine(schema)
        for k in kernels_all:
            # Needed for phase-transition boundary and semantic tree.  The
            # heavier visible cpu_stack_specs are deferred until after trimming.
            specs = classify_kernel(k, slices, module_infos, engine, schema)
            setattr(k, "semantic_specs", specs)
        print(f"[+] classified semantic paths for {len(kernels_all)} search-window kernels in {time.perf_counter()-t_stage:.2f}s")

        kernels, boundary_info, boundary_debug = trim_by_phase_transition(kernels_all, win, boundary_lookaround_ms, boundary_mode)
        ts_start = int(boundary_info["ts_start"])
        ts_end = int(boundary_info["ts_end"])
        comm_summary = apply_communication_accounting(kernels, slices, include_comm_time=include_comm_time)
        if comm_summary["communication_kernel_count"]:
            mode = "included" if include_comm_time else "shown with zero accounted duration"
            print(f"[*] communication kernels: {comm_summary['communication_kernel_count']} raw={comm_summary['communication_raw_dur_ms']:.3f} ms ({mode}; pass --include-comm-time to count them)")

        for k in kernels:
            k.start_offset_ns = k.ts - ts_start
            k.end_offset_ns = k.start_offset_ns + int(k.dur_ns or 0)
        v1.compute_global_kernel_gaps(kernels, ts_start)
        print(f"[+] exact decode window ({boundary_info['method']}): [{ts_start}, {ts_end}) = {(ts_end-ts_start)/1e6:.3f} ms")
        print(f"[+] kernels in exact window: {len(kernels)} (dropped {len(kernels_all)-len(kernels)} from search window)")

        t_stage = time.perf_counter()
        stacks = intern_cpu_stacks(kernels, slices)
        print(f"[+] interned CPU stacks: {len(stacks)} unique stacks for {sum(1 for k in kernels if getattr(k, 'cpu_stack_id', None) is not None)} exact-window anchored kernels in {time.perf_counter()-t_stage:.2f}s")

        t_stage = time.perf_counter()
        for k in kernels:
            specs = getattr(k, "semantic_specs", None) or [(schema.tree.fallback_bucket_key, schema.tree.fallback_bucket_name, "bucket", None, None)]
            if k.cpu_id is not None and k.cpu_id in slices:
                chain = v1.build_chain(k.cpu_id, slices)
                cpu_specs = append_cpu_stack_specs(specs, chain, module_infos, schema, platform, detail=cpu_stack_tree_detail)
            else:
                cpu_specs = specs
            setattr(k, "cpu_stack_specs", cpu_specs)
        print(f"[+] built visible cpu_stack paths for {len(kernels)} exact-window kernels in {time.perf_counter()-t_stage:.2f}s")

        # Now materialize semantic and CPU-stack trees from the exact-window kernels only.
        semantic_root = build_tree_from_kernels(kernels, schema, decode_idx, specs_attr="semantic_specs")
        cpu_root = build_tree_from_kernels(kernels, schema, decode_idx, specs_attr="cpu_stack_specs")

        common = {
            "schema_version": 2,
            "kind": "hygon_h100_trace_normalized_tree",
            "trace_path": trace_path,
            "decode_idx": decode_idx,
            "marker": win,
            "window": {
                "ts_start": ts_start,
                "ts_end": ts_end,
                "duration_ns": ts_end - ts_start,
                "duration_ms": round((ts_end - ts_start) / 1e6, 6),
                "raw_marker_ts_start": raw_start,
                "raw_marker_ts_end": raw_end,
                "boundary_method": boundary_info.get("method"),
                "boundary_info": boundary_info,
            },
            "stats": {
                "gpu_kernel_count": len(kernels),
                "gpu_kernel_search_count": len(kernels_all),
                "flow_count": len(flows) if hasattr(flows, "__len__") else 0,
                "orphan_gpu_count": len([gid for gid in gpu_by_id if gid not in gpu_to_cpu]),
                "cpu_ancestor_slice_count": len(slices),
                "module_slice_count": len(module_infos),
                "cpu_stack_count": len(stacks),
                "communication_kernel_count": comm_summary.get("communication_kernel_count", 0),
                "communication_raw_dur_ns": comm_summary.get("communication_raw_dur_ns", 0),
                "communication_time_accounted": bool(include_comm_time),
            },
            "communication": comm_summary,
            "cpu_stacks": stacks,
        }
        tree_json = dict(common)
        tree_json["root"] = semantic_root.to_dict(include_kernels=include_kernels_in_tree_json)
        cpu_tree_json = dict(common)
        cpu_tree_json["kind"] = "hygon_h100_trace_cpu_stack_tree"
        cpu_tree_json["root"] = cpu_root.to_dict(include_kernels=include_kernels_in_tree_json)

        nodes: List[Dict[str, Any]] = []
        v1.flatten_nodes(cpu_root, nodes)
        template_json = {
            "schema_version": 2,
            "kind": "hygon_h100_trace_semantic_template",
            "trace_path": trace_path,
            "decode_idx": decode_idx,
            "marker": win,
            "window": tree_json["window"],
            "stats": tree_json["stats"],
            "nodes": nodes,
        }

        unaccounted = summarize_unaccounted(kernels_all, kernels, gpu_to_cpu)

        # Slice decode window into a small trace file (reuses open tp)
        slice_info = None
        if slice_output_path:
            slice_info = slice_via_open_tp(tp, slice_output_path, raw_start, raw_end)

        return tree_json, cpu_tree_json, template_json, kernels, boundary_debug, unaccounted, slice_info
    finally:
        tp.close()


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def summarize_unaccounted(kernels_all: List[v1.KernelRecord], kernels: List[v1.KernelRecord], gpu_to_cpu: Dict[int, int]) -> Dict[str, Any]:
    included = {k.gpu_id for k in kernels}
    dropped = [k for k in kernels_all if k.gpu_id not in included]
    unmapped = [k for k in kernels_all if k.gpu_id not in gpu_to_cpu]
    def top(rows: List[v1.KernelRecord], limit: int = 20) -> List[Dict[str, Any]]:
        by: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for k in rows:
            key = (k.name, k.category, k.stream)
            x = by.setdefault(key, {"name": k.name, "category": k.category, "stream": k.stream, "count": 0, "sum_dur_ns": 0})
            x["count"] += 1
            x["sum_dur_ns"] += int(k.dur_ns)
        return sorted(by.values(), key=lambda x: (-x["sum_dur_ns"], -x["count"], x["name"]))[:limit]
    return {
        "dropped_by_window_trim": {"count": len(dropped), "top": top(dropped)},
        "unmapped_gpu_slices": {"count": len(unmapped), "top": top(unmapped)},
    }


def write_kernel_csv(path: str, kernels: List[v1.KernelRecord], decode_idx: int) -> None:
    ensure_parent(path)
    fields = [
        "decode_idx", "global_kernel_idx", "gpu_id", "kernel_name", "category", "stream",
        "ts", "dur_ns", "original_dur_ns", "is_communication", "communication_reason",
        "start_offset_ns", "end_offset_ns", "gap_to_next_ns", "overlap_to_next_ns",
        "cpu_id", "cpu_launch_name", "cpu_stack_id", "correlation_id", "phase", "semantic_path", "semantic_name",
        "matched_rule_id", "relation", "confidence", "shape_summary", "cpu_chain_tail",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, k in enumerate(sorted(kernels, key=lambda x: (x.ts, x.gpu_id))):
            shape = getattr(k, "shape", {}) or {}
            w.writerow({
                "decode_idx": decode_idx,
                "global_kernel_idx": i,
                "gpu_id": k.gpu_id,
                "kernel_name": k.name,
                "category": k.category,
                "stream": k.stream,
                "ts": k.ts,
                "dur_ns": k.dur_ns,
                "original_dur_ns": getattr(k, "original_dur_ns", k.dur_ns),
                "is_communication": bool(getattr(k, "is_communication", False)),
                "communication_reason": getattr(k, "communication_reason", ""),
                "start_offset_ns": k.start_offset_ns,
                "end_offset_ns": k.end_offset_ns,
                "gap_to_next_ns": "" if k.gap_to_next_ns is None else k.gap_to_next_ns,
                "overlap_to_next_ns": k.overlap_to_next_ns,
                "cpu_id": "" if k.cpu_id is None else k.cpu_id,
                "cpu_launch_name": k.cpu_launch_name,
                "cpu_stack_id": "" if getattr(k, "cpu_stack_id", None) is None else getattr(k, "cpu_stack_id"),
                "correlation_id": "" if getattr(k, "correlation_id", None) is None else getattr(k, "correlation_id"),
                "phase": k.phase,
                "semantic_path": k.semantic_path,
                "semantic_name": k.semantic_name,
                "matched_rule_id": k.matched_rule_id,
                "relation": k.relation,
                "confidence": k.confidence,
                "shape_summary": shape.get("summary", ""),
                "cpu_chain_tail": k.cpu_chain_tail,
            })


def write_boundary_debug_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    ensure_parent(path)
    fields = ["boundary", "boundary_ts", "kernel_ts", "distance_us", "stream", "label", "chosen", "gpu_id", "kernel", "matched_rule_id", "cpu_launch_name", "cpu_chain_tail"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def default_prefix(trace_path: str, decode_idx: int, schema: v1.SchemaConfig) -> str:
    return v1.default_prefix(trace_path, decode_idx, schema)


def render_html(json_path: str, html_path: str, schema: v1.SchemaConfig, title: Optional[str] = None) -> None:
    sys.path.insert(0, str(Path(__file__).parent))
    import render_trace_tree
    render_trace_tree.write_html(
        html_path,
        json.load(open(json_path, encoding="utf-8")),
        merge_repeated_min=schema.html_render.merge_repeated_min,
        trailing_digit_strip_regex=schema.html_render.trailing_digit_strip_regex.pattern,
        title=title,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("trace", nargs="?", help="pt.trace.json.gz / trace.json.gz path")
    p.add_argument("--schema", required=True, help="path to schema.json")
    p.add_argument("--platform", required=True, help="path to platform.json")
    p.add_argument("--decode-idx", type=int, default=10, help="which decode token (1-indexed)")
    p.add_argument("--decode-marker", help="explicit decode marker name")
    p.add_argument("--decode-boundary-mode", choices=["phase", "marker"], default="phase", help="phase transition boundary or raw marker interval")
    p.add_argument("--decode-anchor-lookaround-ms", type=float, default=50.0, help="lookaround around marker ts for phase boundary")
    p.add_argument("--cpu-stack-tree-detail", choices=["module_vllm", "module", "concise", "source", "verbose"], default="module_vllm",
                   help="Visible cpu_stack tree detail. module_vllm keeps nn.Module nodes plus real vLLM source breadcrumbs; those breadcrumbs participate in compare/joint pruning. module keeps only nn.Module nodes. Schema phases are still used for boundary. Full stacks are always preserved in JSON.")
    p.add_argument("--out-prefix", help="output file prefix; used verbatim")
    p.add_argument("--out-dir", help="output dir when --out-prefix is not given")
    p.add_argument("--trace-processor-shell",
                   help="path to perfetto trace_processor_shell. Can also be set by TRACE_PROCESSOR_SHELL.")
    p.add_argument("--include-comm-time", action="store_true",
                   help="count communication kernels in node GPU time. Default: show communication kernels, but charge 0 accounted time and keep original_dur_ns.")
    p.add_argument("--list-markers", action="store_true")
    p.add_argument("--no-kernels-in-tree-json", action="store_true")
    args = p.parse_args()

    if not args.trace:
        p.error("trace is required unless using --help")
    schema = v1.load_schema(args.schema)
    platform = v1.load_platform(args.platform)

    tp = v1.open_trace_processor(args.trace, trace_processor_shell=args.trace_processor_shell)
    try:
        if args.list_markers:
            v1.list_marker_candidates(tp, platform)
            return
    finally:
        tp.close()

    if args.out_prefix:
        prefix = args.out_prefix
    else:
        out_dir = args.out_dir if args.out_dir is not None else schema.output_naming.output_dir
        base = default_prefix(args.trace, args.decode_idx, schema)
        prefix = str(Path(out_dir) / base) if out_dir else base

    tree_json, cpu_tree_json, template_json, kernels, boundary_debug, unaccounted = probe_trace(
        args.trace,
        schema=schema,
        platform=platform,
        decode_idx=args.decode_idx,
        decode_marker=args.decode_marker,
        include_kernels_in_tree_json=not args.no_kernels_in_tree_json,
        boundary_mode=args.decode_boundary_mode,
        boundary_lookaround_ms=args.decode_anchor_lookaround_ms,
        cpu_stack_tree_detail=args.cpu_stack_tree_detail,
        include_comm_time=args.include_comm_time,
        trace_processor_shell=args.trace_processor_shell,
    )

    tree_path = f"{prefix}.normalized_tree.json"
    cpu_tree_path = f"{prefix}.cpu_stack.normalized_tree.json"
    template_path = f"{prefix}.semantic_template.json"
    csv_path = f"{prefix}.kernel_timeline.csv"
    html_path = f"{prefix}.semantic_tree.html"
    cpu_html_path = f"{prefix}.cpu_stack.html"
    boundary_path = f"{prefix}.boundary_debug.csv"
    unaccounted_path = f"{prefix}.unaccounted_gpu_activity.json"

    write_json(tree_path, tree_json)
    write_json(cpu_tree_path, cpu_tree_json)
    write_json(template_path, template_json)
    write_kernel_csv(csv_path, kernels, args.decode_idx)
    write_boundary_debug_csv(boundary_path, boundary_debug)
    write_json(unaccounted_path, unaccounted, pretty=True)
    render_html(tree_path, html_path, schema, title=f"Semantic decode tree — decode #{args.decode_idx}")
    render_html(cpu_tree_path, cpu_html_path, schema, title=f"CPU-stack decode tree — decode #{args.decode_idx}")

    for path in [tree_path, cpu_tree_path, template_path, csv_path, html_path, cpu_html_path, boundary_path, unaccounted_path]:
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"[+] wrote {path} ({size_mb:.2f} MB)")
    print("[*] Next:")
    print(f"    open    {cpu_html_path}")
    print(f"    raw     {html_path}")
    print(f"    inspect {csv_path}")
    print(f"    debug   {boundary_path}")


if __name__ == "__main__":
    main()
