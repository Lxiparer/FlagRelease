#!/usr/bin/env python3
"""Top-level pipeline orchestrator: profile → probe → compare → generate agent skills.

Usage:
    python run_full_pipeline.py \
      --task-name Hygon_Qwen3.6ProfileAnalyse \
      --task-dir /tmp/Hygon_Qwen3.6ProfileAnalyse \
      --decode-idx 30

    python run_full_pipeline.py --skip-profile \
      --task-dir /tmp/Hygon_Qwen3.6ProfileAnalyse \
      --decode-idx 30

    python run_full_pipeline.py --profile-only \
      --task-name Hygon_Qwen3.6ProfileAnalyse \
      --task-dir /tmp/Hygon_Qwen3.6ProfileAnalyse
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List


PROGRAM_ROOT = Path(__file__).resolve().parent


def run_step(cmd: List[str], *, label: str, check: bool = True) -> subprocess.CompletedProcess:
    print(f"\n{'='*60}", flush=True)
    print(f"[PIPELINE] {label}", flush=True)
    print(f"  cmd: {' '.join(cmd)}", flush=True)
    print(f"{'='*60}\n", flush=True)
    t0 = time.time()
    # Force the child process to be unbuffered so its stdout/stderr stream into
    # our (possibly redirected) log line-by-line instead of all at once at exit.
    child_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    cp = subprocess.run(cmd, text=True, env=child_env)
    elapsed = time.time() - t0
    status = "OK" if cp.returncode == 0 else f"FAILED (rc={cp.returncode})"
    print(f"\n[PIPELINE] {label}: {status} ({elapsed:.1f}s)", flush=True)
    if check and cp.returncode != 0:
        raise SystemExit(f"[FATAL] Step failed: {label}")
    return cp


def step_profile(task_dir: Path, config_dir: Path, target: str, dry_run: bool = False) -> None:
    # v27-faithful CLI: --upstream / --config / --override / --target.
    upstream = config_dir / "upstream.json"
    local_required = config_dir / "local_required.json"
    override = config_dir / "override.json"
    cmd = [
        sys.executable,
        str(PROGRAM_ROOT / "auto_profile" / "profile_runner.py"),
        "run",
        "--upstream", str(upstream),
        "--config", str(local_required),
        "--target", target,
        # Keep all run artifacts under --task-dir.
        "--host-output-dir", str(task_dir / "traceSource_both"),
        "--host-log-dir", str(task_dir / "profile_logs"),
    ]
    if override.exists():
        cmd += ["--override", str(override)]
    if dry_run:
        cmd.append("--dry-run")
    run_step(cmd, label=f"Profile ({target})")


def step_probe_and_compare(task_dir: Path, decode_idx: int, output_dir: Path) -> None:
    trace_dir = task_dir / "traceSource_both"
    gems_trace = trace_dir / "gems.pt.trace.json.gz"
    vendor_trace = trace_dir / "vendor.pt.trace.json.gz"

    if not gems_trace.exists():
        raise FileNotFoundError(f"Gems trace not found: {gems_trace}")
    if not vendor_trace.exists():
        raise FileNotFoundError(f"Vendor trace not found: {vendor_trace}")

    probe_script = PROGRAM_ROOT / "probe_and_compare.py"
    if not probe_script.exists():
        candidates = list(PROGRAM_ROOT.glob("**/probe_and_compare*.py"))
        if candidates:
            probe_script = candidates[0]
        else:
            raise FileNotFoundError("Cannot find probe_and_compare.py")

    cmd = [
        sys.executable, str(probe_script),
        "--gems-trace", str(gems_trace),
        "--vendor-trace", str(vendor_trace),
        "--decode-idx", str(decode_idx),
        "--out-dir", str(output_dir),
    ]
    run_step(cmd, label="Probe & Compare")


def step_generate_skills(output_dir: Path, decode_idx: int, config_dir: Path, container_context: Path = None) -> None:
    cmd = [
        sys.executable,
        str(PROGRAM_ROOT / "agent" / "generate_skills.py"),
        "--run-dir", str(output_dir),
        "--profile-dir", str(config_dir),
        "--decode-idx", str(decode_idx),
    ]
    # An explicit context file (if already present) takes precedence over building it.
    if container_context and container_context.exists():
        cmd.extend(["--container-context-json", str(container_context)])
    run_step(cmd, label="Generate Agent Skills")


def release_folder_name(config_dir: Path) -> str:
    """Derive a filesystem-safe folder name from release_name in the config."""
    name = None
    for fn in ("local_required.json", "upstream.json"):
        p = config_dir / fn
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if d.get("release_name"):
                    name = str(d["release_name"])
                    break
            except Exception:
                pass
    name = name or "release"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("_")
    return safe or "release"


_STAGE_NAMES = {
    "profile": 1, "probe": 2, "compare": 3,
    "skills": 4, "skill": 4, "agent": 4,
}


def parse_stages(spec: str, profile_only: bool, skip_profile: bool) -> set:
    """Resolve which logical stages to run: 1=profile, 2=probe, 3=compare, 4=skills.

    Note: probe (2) and compare (3) are produced by one script, so selecting either
    runs both. --profile-only and --skip-profile are kept as convenient aliases.
    """
    if profile_only:
        return {1}
    if skip_profile:
        return {2, 3, 4}
    if not spec:
        return {1, 2, 3, 4}

    def tok2num(t: str) -> int:
        t = t.strip().lower()
        if t in _STAGE_NAMES:
            return _STAGE_NAMES[t]
        return int(t)

    out = set()
    for tok in spec.replace(" ", "").split(","):
        if not tok:
            continue
        if "-" in tok:
            a, b = tok.split("-", 1)
            a, b = tok2num(a), tok2num(b)
            out.update(range(min(a, b), max(a, b) + 1))
        else:
            out.add(tok2num(tok))
    return {s for s in out if 1 <= s <= 4}


def organize_outputs(out_dir: Path) -> None:
    """Keep the release folder tidy: <ReleaseName>/ = {compare_dashboard.html, src/, agent/}.

    The compare CSV/JSON exports are tucked into agent/ (the dashboard is self-contained
    and does not read them; the agent/human analysis does).
    """
    agent_dir = out_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in ("compare_nodes.csv", "compare_templates.csv",
                 "compare_hotspots.csv", "compare_unmatched.json"):
        srcf = out_dir / name
        if srcf.exists():
            shutil.move(str(srcf), str(agent_dir / name))
            moved.append(name)
    if moved:
        print(f"[PIPELINE] tucked {len(moved)} compare export(s) into {agent_dir}")


def main() -> None:
    # When stdout/stderr are redirected to a file (e.g. nohup ... > pipeline.log),
    # Python block-buffers by default. Switch to line buffering so progress shows live.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Full pipeline: profile → probe → compare → agent skills")
    ap.add_argument("--task-name", default=None, help="Task name (used for directory naming)")
    ap.add_argument("--task-dir", required=True, help="Task directory (e.g. /tmp/Hygon_Qwen3.6ProfileAnalyse)")
    ap.add_argument("--decode-idx", type=int, default=30, help="Decode index for probe")
    ap.add_argument("--output-dir", default=None, help="Output dir for probe+compare (default: program/output/)")
    ap.add_argument("--config-dir", default=None, help="Config dir for profile_runner (default: auto_profile/)")
    ap.add_argument("--target", default="both", choices=["vendor", "gems", "both"])
    ap.add_argument("--container-context-json", default=None)

    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--skip-profile", action="store_true", help="Alias for --stages 2-4 (use existing traces)")
    mode.add_argument("--profile-only", action="store_true", help="Alias for --stages 1 (only profile)")

    ap.add_argument("--stages", default="",
                    help="Which stages to run: 1=profile, 2=probe, 3=compare, 4=skills. "
                         "Accepts numbers/names/ranges, e.g. '1-3', '2,4', 'skills', 'probe-skills'. "
                         "Note: probe(2)+compare(3) run together. Default: all.")
    ap.add_argument("--dry-run", action="store_true", help="Dry run (profile step only)")
    args = ap.parse_args()

    task_dir = Path(args.task_dir).resolve()
    task_dir.mkdir(parents=True, exist_ok=True)

    config_dir = Path(args.config_dir).resolve() if args.config_dir else PROGRAM_ROOT / "auto_profile"

    # Tidy output: <task_dir>/<ReleaseName>/ = {compare_dashboard.html, src/, agent/}.
    # Bulky intermediates (traces, profile logs) stay in <task_dir> alongside it.
    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    else:
        output_dir = task_dir / release_folder_name(config_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    stages = parse_stages(args.stages, args.profile_only, args.skip_profile)
    if not stages:
        sys.exit("[FATAL] --stages selected no valid stage (use 1..4 / profile,probe,compare,skills)")

    run_profile = 1 in stages
    run_probe_compare = (2 in stages) or (3 in stages)
    run_skills = 4 in stages

    print(f"[PIPELINE] task_dir:   {task_dir}")
    print(f"[PIPELINE] config_dir: {config_dir}")
    print(f"[PIPELINE] output_dir: {output_dir}")
    print(f"[PIPELINE] decode_idx: {args.decode_idx}")
    print(f"[PIPELINE] stages:     {sorted(stages)}  "
          f"(profile={run_profile}, probe+compare={run_probe_compare}, skills={run_skills})")

    t_start = time.time()

    # --- 1. Profile ---
    if run_profile:
        step_profile(task_dir, config_dir, args.target, dry_run=args.dry_run)
        if args.dry_run:
            print(f"\n[PIPELINE] Dry-run done in {time.time()-t_start:.1f}s.")
            return

    # --- 2+3. Probe & Compare (one script) ---
    if run_probe_compare:
        step_probe_and_compare(task_dir, args.decode_idx, output_dir)
        organize_outputs(output_dir)

    # --- 4. Generate Agent Skills ---
    if run_skills:
        ctx_path = Path(args.container_context_json) if args.container_context_json else None
        step_generate_skills(output_dir, args.decode_idx, config_dir, container_context=ctx_path)

    elapsed = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"[PIPELINE] Done ({sorted(stages)}) in {elapsed:.1f}s")
    if run_probe_compare:
        print(f"[PIPELINE] Dashboard:     {output_dir}/compare_dashboard.html")
    if run_skills:
        print(f"[PIPELINE] Triage entry:  {output_dir}/agent/CLAUDE_TRIAGE.md")
    elif run_probe_compare:
        print(f"[PIPELINE] (skills not generated; run later with: --stages 4 --task-dir {task_dir})")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
