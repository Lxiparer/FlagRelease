#!/usr/bin/env python3
"""Generate Claude Code agent skill bundle from a probe+compare run.

Given a completed probe+compare `run_dir` (and the auto_profile config dir), this:

  1. builds the agent index (agent_triage_queue.jsonl + agent_semantic_index.json)
     via semantic_perf_query.build_index;
  2. produces agent_container_context.json from the auto_profile config
     (build_profile_context) so the agent knows which container to enter — unless an
     explicit --container-context-json is supplied;
  3. copies the repo skill agent/claude_skills/gpu-semantic-triage/ into the run dir so
     the bundle is self-contained and portable to Claude Code;
  4. writes an enriched CLAUDE_TRIAGE.md entry point that embeds the already-sorted
     Gems-slower targets, the run artifacts, and how to enter each container.

Usage:
    python agent/generate_skills.py \
      --run-dir ./output \
      --profile-dir ./auto_profile \
      --decode-idx 30 \
      [--container-context-json ./agent_container_context.json] \
      [--source-path-map /vllm-workspace=/Users/you/workspace]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SKILL_NAME = "gpu-semantic-triage"


def load_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_queue(run_dir: Path) -> List[Dict[str, Any]]:
    """Read agent_triage_queue.jsonl (from <run_dir>/agent/, legacy: <run_dir>/)."""
    qp = run_dir / "agent" / "agent_triage_queue.jsonl"
    if not qp.exists():
        qp = run_dir / "agent_triage_queue.jsonl"
    rows: List[Dict[str, Any]] = []
    if not qp.exists():
        return rows
    for line in qp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    rows.sort(key=lambda r: int(r.get("rank", 1 << 30)))
    return rows


def ns_to_ms(ns: Any) -> str:
    try:
        return f"{float(ns) / 1_000_000:.3f}"
    except (TypeError, ValueError):
        return "?"


def ratio_str(r: Any) -> str:
    try:
        return f"{float(r):.2f}x"
    except (TypeError, ValueError):
        return "-"


# ---------------------------------------------------------------------------
# Embedded fallbacks (used only if the repo skill dir is missing)
# ---------------------------------------------------------------------------

FALLBACK_SKILL_MD = f"""\
---
name: {SKILL_NAME}
description: Analyze Gems vs Vendor semantic compare runs on H100/Hygon; rank slower Gems nodes, inspect kernels, CPU stacks, shapes, and operator source context.
---

# GPU Semantic Triage (fallback copy)

The full skill lives in the repo at `agent/claude_skills/{SKILL_NAME}/SKILL.md`. This is
a minimal fallback. Read `CLAUDE_TRIAGE.md` first, then use the query scripts:

```bash
python agent/semantic_perf_query.py build-index  --run-dir <run_dir>
python agent/semantic_perf_query.py list-targets --run-dir <run_dir> --limit 20
python agent/semantic_perf_query.py describe      --run-dir <run_dir> --rank 1
python agent/semantic_perf_query.py kernels       --run-dir <run_dir> --rank 1
python agent/semantic_perf_query.py stacks        --run-dir <run_dir> --rank 1 --side gems
python agent/semantic_perf_query.py source        --run-dir <run_dir> --rank 1 --side gems --path-map ...
```

Compare only semantic subtree kernel execution time. Do not analyze from HTML. Prefer
absolute kernel-time delta as the priority metric.
"""

FALLBACK_OUTPUT_SCHEMA_MD = """\
# Agent Triage Output Schema (fallback)

agent_triage_queue.jsonl: one object per Gems-slower target, sorted by absolute
kernel-time delta. Fields: rank, semantic_key, display_path, gems_kernel_ns,
vendor_kernel_ns, delta_gems_minus_vendor_ns, ratio_gems_over_vendor,
gems_kernel_count, vendor_kernel_count, evidence_tags.

agent_semantic_index.json: all exact compare nodes + the queue.
describe --format json: summary, kernel_mix, shape_diff, cpu_stack_clusters,
source_locations, kernel_ids.
"""


def copy_repo_skill_into_run(program_root: Path, run_dir: Path) -> Path:
    """Copy agent/claude_skills/<SKILL_NAME>/ into run_dir/claude_skills/<SKILL_NAME>/.

    Falls back to writing minimal embedded docs if the repo skill dir is missing.
    Returns the run-dir-local skill directory.
    """
    repo_skill = program_root / "agent" / "claude_skills" / SKILL_NAME
    dst_skill = run_dir / "agent" / "claude_skills" / SKILL_NAME

    if repo_skill.is_dir():
        if dst_skill.exists():
            shutil.rmtree(dst_skill)
        shutil.copytree(repo_skill, dst_skill, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        print(f"[+] copied repo skill -> {dst_skill}")
        return dst_skill

    # Fallback: synthesize a minimal skill bundle.
    print(f"[!] repo skill dir not found at {repo_skill}; writing minimal fallback skill")
    write_text(dst_skill / "SKILL.md", FALLBACK_SKILL_MD)
    write_text(dst_skill / "references" / "OUTPUT_SCHEMA.md", FALLBACK_OUTPUT_SCHEMA_MD)
    scripts_dir = dst_skill / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for name in ("semantic_perf_query.py", "container_source_query.py", "profile_context.py"):
        src = program_root / "agent" / name
        if src.exists():
            shutil.copy2(src, scripts_dir / name)
    return dst_skill


def resolve_container_context(
    run_dir: Path,
    program_root: Path,
    explicit_ctx: Optional[Path],
    profile_dir: Optional[Path],
) -> Optional[Dict[str, Any]]:
    """Return container context, writing run_dir/agent_container_context.json.

    Priority: explicit json > build from profile_dir > already-present in run_dir > none.
    """
    dst = run_dir / "agent" / "agent_container_context.json"
    dst.parent.mkdir(parents=True, exist_ok=True)

    # 1. Explicit context file wins.
    if explicit_ctx and Path(explicit_ctx).exists():
        ctx = load_json(Path(explicit_ctx))
        shutil.copy2(Path(explicit_ctx), dst)
        print(f"[+] using provided container context -> {dst}")
        return ctx

    # 2. Build from the auto_profile config dir.
    if profile_dir is None:
        guess = program_root / "auto_profile"
        if guess.exists():
            profile_dir = guess
    if profile_dir and Path(profile_dir).exists():
        try:
            sys.path.insert(0, str((program_root / "agent").resolve()))
            import profile_context as pc  # type: ignore
            ctx = pc.build_profile_context(Path(profile_dir))
            pc.write_context(dst, ctx)
            print(f"[+] built container context from {profile_dir} -> {dst}")
            return ctx
        except Exception as exc:  # tolerant: context is helpful but not mandatory
            print(f"[!] could not build container context from {profile_dir}: {exc!r}")

    # 3. Already present in run dir.
    if dst.exists():
        print(f"[+] reusing existing container context at {dst}")
        return load_json(dst)

    print("[!] no container context available (agent will fall back to --path-map source access)")
    return None


def render_claude_triage_md(
    *,
    run_dir: Path,
    program_root: Path,
    decode_idx: int,
    queue: List[Dict[str, Any]],
    container_context: Optional[Dict[str, Any]],
    source_path_maps: List[str],
    local_skill_dir: Path,
    top_n: int = 15,
) -> str:
    spq = f"python {program_root}/agent/semantic_perf_query.py"
    csq = f"python {program_root}/agent/container_source_query.py"

    out: List[str] = []
    out.append("# CLAUDE_TRIAGE.md")
    out.append("")
    out.append("Auto-generated entry point for Claude Code agent triage "
               "(Gems vs Vendor, H100/Hygon).")
    out.append("")
    out.append(f"- run_dir: `{run_dir}`")
    out.append(f"- decode_idx: `{decode_idx}`")
    out.append(f"- repo_root: `{program_root}`")
    out.append("")
    out.append("## Goal")
    out.append("")
    out.append("For each high-priority semantic node below, find why **Gems** is slower "
               "than **Vendor**: follow the CPU call stack to the operator source, explain "
               "the slowdown (lowering / extra cast-copy / non-fused path / kernel "
               "selection / layout), and propose plus verify a fix. Use absolute "
               "kernel-time delta as the priority metric.")
    out.append("")

    # --- Sorted Gems-slower targets (embedded) ---
    out.append("## Top Gems-slower semantics (by absolute kernel-time delta)")
    out.append("")
    if queue:
        out.append("| rank | Δ ms (gems-vendor) | gems ms | vendor ms | G/V | semantic |")
        out.append("|-----:|-------------------:|--------:|----------:|----:|----------|")
        for r in queue[:top_n]:
            out.append(
                f"| {r.get('rank','?')} "
                f"| {ns_to_ms(r.get('delta_gems_minus_vendor_ns'))} "
                f"| {ns_to_ms(r.get('gems_kernel_ns'))} "
                f"| {ns_to_ms(r.get('vendor_kernel_ns'))} "
                f"| {ratio_str(r.get('ratio_gems_over_vendor'))} "
                f"| {r.get('display_path') or r.get('semantic_key','')} |"
            )
        out.append("")
        out.append(f"Full queue ({len(queue)} targets): `{run_dir}/agent/agent_triage_queue.jsonl`. "
                   "To act on a target, pass its `--rank` (or `--semantic-key`) to the "
                   "query script below.")
    else:
        out.append("_Queue not built yet._ Run:")
        out.append("")
        out.append("```bash")
        out.append(f"{spq} build-index --run-dir {run_dir}")
        out.append("```")
    out.append("")

    # --- Artifacts (absolute paths; layout: <run_dir>/{compare_dashboard.html, src/, agent/}) ---
    out.append("## Artifacts in this run")
    out.append("")
    out.append(f"- Dashboard (navigation only, do NOT analyze from HTML): `{run_dir}/compare_dashboard.html`")
    out.append(f"- Largest-gap semantics: `{run_dir}/agent/compare_hotspots.csv`, `compare_nodes.csv`, `compare_templates.csv`")
    out.append(f"- Per-side CPU-stack view: `{run_dir}/src/gems/final*.cpu_stack.html`, `{run_dir}/src/vendor/final*.cpu_stack.html`")
    out.append(f"- Per-side small trace slice (openable in Perfetto): `{run_dir}/src/gems/final*.decode_slice.json.gz`, `{run_dir}/src/vendor/final*.decode_slice.json.gz`")
    out.append(f"- Structured index: `{run_dir}/agent/agent_semantic_index.json`, `{run_dir}/agent/agent_triage_queue.jsonl`")
    out.append("")

    # --- Quick start ---
    out.append("## Recommended commands (structured queries, not HTML)")
    out.append("")
    out.append("```bash")
    out.append(f"{spq} list-targets --run-dir {run_dir} --sort delta --limit 30")
    out.append(f"{spq} describe     --run-dir {run_dir} --rank 1")
    out.append(f"{spq} kernels      --run-dir {run_dir} --rank 1")
    out.append(f"{spq} stacks       --run-dir {run_dir} --rank 1 --side gems")
    out.append(f"{spq} stacks       --run-dir {run_dir} --rank 1 --side vendor")
    path_map_hint = source_path_maps[0] if source_path_maps else "/vllm-workspace=/abs/local/workspace"
    out.append(f"{spq} source       --run-dir {run_dir} --rank 1 --side gems --path-map {path_map_hint} --context-lines 40")
    out.append("```")
    out.append("")

    # --- Container enter ---
    out.append("## Enter the right container for operator source (read-only)")
    out.append("")
    sides = (container_context or {}).get("sides") or {}
    if sides:
        out.append("Use the controlled tool (preferred over raw docker):")
        out.append("")
        out.append("```bash")
        out.append(f"{csq} show-context --run-dir {run_dir}")
        out.append(f"{csq} snapshot --run-dir {run_dir} --rank 1 --side gems")
        out.append(f"{csq} source   --run-dir {run_dir} --side gems --path <file_from_source_locations> --context-lines 80")
        out.append("```")
        out.append("")
        for side, info in sides.items():
            info = info or {}
            out.append(f"### {side}")
            out.append(f"- container: `{info.get('container', 'N/A')}`")
            out.append(f"- workdir: `{info.get('workdir', 'N/A')}`")
            if info.get("image"):
                out.append(f"- image: `{info.get('image')}`")
            if info.get("model_path"):
                out.append(f"- model_path (in container): `{info.get('model_path')}`")
            out.append(f"- manual fallback: `docker exec -it {info.get('container', '<container>')} bash`")
            out.append("")
        out.append("Rules: do NOT start both containers at once; do NOT start vLLM unless "
                   "the user asks to verify; prefer `snapshot`/`source` for read-only access; "
                   "`verify` requires explicit `--yes`.")
    else:
        out.append("_No `agent_container_context.json` available._ Re-run "
                   "`generate_skills.py` with `--profile-dir <auto_profile_dir>` to build it, "
                   "or analyze with local source via `--path-map`.")
    out.append("")

    # --- Source path maps ---
    if source_path_maps:
        out.append("## Source path maps")
        out.append("")
        for m in source_path_maps:
            out.append(f"- `{m}`")
        out.append("")

    # --- Skill ref ---
    out.append("## Skill")
    out.append("")
    out.append(f"Full workflow (bundled with this run): `{local_skill_dir}/SKILL.md`")
    out.append(f"Repo copy: `{program_root}/agent/claude_skills/{SKILL_NAME}/SKILL.md`")
    out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate agent skill bundle from a probe+compare run")
    ap.add_argument("--run-dir", required=True, help="probe+compare output directory (contains src/gems, src/vendor)")
    ap.add_argument("--profile-dir", default=None, help="auto_profile config dir; used to build agent_container_context.json")
    ap.add_argument("--container-context-json", default=None, help="explicit agent_container_context.json (overrides --profile-dir)")
    ap.add_argument("--decode-idx", type=int, default=100)
    ap.add_argument("--source-path-map", action="append", default=[])
    ap.add_argument("--repo-root", default=None, help="repo root holding clean_compare_and_generate_dashboard.py (default: this file's parent dir)")
    ap.add_argument("--skip-build-index", action="store_true")
    args = ap.parse_args()

    run_dir = Path(args.run_dir).resolve()
    program_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]

    if not run_dir.exists():
        sys.exit(f"[ERROR] run_dir does not exist: {run_dir}")

    # 1. Build the agent index (tolerant: a missing/failed build still yields a skill bundle).
    if not args.skip_build_index:
        print("[*] Building agent index...")
        try:
            sys.path.insert(0, str((program_root / "agent").resolve()))
            import semantic_perf_query as spq  # type: ignore
            spq.build_index(run_dir, program_root)
            print(f"[+] agent_triage_queue.jsonl + agent_semantic_index.json written to {run_dir}")
        except Exception as exc:
            print(f"[!] build-index failed ({exc!r}); CLAUDE_TRIAGE.md will omit the target table.")
            print(f"    Fix inputs, then run: python {program_root}/agent/semantic_perf_query.py build-index --run-dir {run_dir}")

    # 2. Container context (build from profile dir unless explicitly provided).
    profile_dir = Path(args.profile_dir) if args.profile_dir else None
    container_context = resolve_container_context(
        run_dir, program_root, Path(args.container_context_json) if args.container_context_json else None, profile_dir
    )

    # 3. Copy the repo skill bundle into the run dir.
    local_skill_dir = copy_repo_skill_into_run(program_root, run_dir)

    # 4. Enriched CLAUDE_TRIAGE.md entry point.
    queue = read_queue(run_dir)
    triage_md = render_claude_triage_md(
        run_dir=run_dir,
        program_root=program_root,
        decode_idx=args.decode_idx,
        queue=queue,
        container_context=container_context,
        source_path_maps=args.source_path_map,
        local_skill_dir=local_skill_dir,
    )
    triage_path = run_dir / "agent" / "CLAUDE_TRIAGE.md"
    write_text(triage_path, triage_md)
    print(f"[+] CLAUDE_TRIAGE.md written to {triage_path} ({len(queue)} targets embedded)")
    print(f"[*] Done. Hand {triage_path} to Claude Code as the task entry point.")


if __name__ == "__main__":
    main()
