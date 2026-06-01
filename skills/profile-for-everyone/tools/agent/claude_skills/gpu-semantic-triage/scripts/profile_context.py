#!/usr/bin/env python3
"""Discovery helpers for DBSource_both/ and auto_profile_script/.

The profile runner config may evolve, so the parser is intentionally tolerant:
it prefers known keys such as targets.<side>.container.name and
runtime.workdir, but also falls back to recursive key search.
"""
from __future__ import annotations

import importlib.util
import sys
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SIDE_KEYS = ("gems", "vendor")


def load_json_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def recursive_values(obj: Any, key_names: Iterable[str]) -> List[Any]:
    keys = {k.lower() for k in key_names}
    out: List[Any] = []
    def rec(x: Any):
        if isinstance(x, dict):
            for k, v in x.items():
                if str(k).lower() in keys:
                    out.append(v)
                rec(v)
        elif isinstance(x, list):
            for v in x:
                rec(v)
    rec(obj)
    return out


def get_path(obj: Dict[str, Any], path: str) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def first_str(*vals: Any) -> Optional[str]:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return None


def side_target(profile: Dict[str, Any], side: str) -> Dict[str, Any]:
    candidates = [
        get_path(profile, f"targets.{side}"),
        get_path(profile, f"target.{side}"),
        profile.get(side),
    ]
    for c in candidates:
        if isinstance(c, dict):
            return c
    return {}


def find_side_related_json(profile_dir: Path) -> Dict[str, List[Path]]:
    out = {"all": [], "local_profile": [], "upstream": [], "override": []}
    for p in sorted(profile_dir.rglob("*.json")):
        out["all"].append(p)
        n = p.name.lower()
        if "local_profile" in n or "profile" in n:
            out["local_profile"].append(p)
        if "upstream" in n:
            out["upstream"].append(p)
        if n == "override.json" or ("override" in n and "example" not in n):
            out["override"].append(p)
    return out


def parse_docker_run_files(profile_dir: Path) -> Dict[str, List[Dict[str, str]]]:
    """Best-effort parse of docker run/setup scripts for image and volume hints."""
    candidates = []
    for ext in ("*.sh", "*.bash", "*.txt"):
        candidates.extend(profile_dir.rglob(ext))
    info: Dict[str, List[Dict[str, str]]] = {"files": [], "volumes": [], "images": []}
    volume_re = re.compile(r"(?:-v|--volume)\s+([^\s]+)")
    image_re = re.compile(r"docker\s+run(?:.|\n)*?\s([\w./:-]+)\s*(?:\\\n|$)", re.MULTILINE)
    for p in sorted(set(candidates)):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        low = text.lower()
        if "docker" not in low and "vllm" not in low and "npu" not in low:
            continue
        info["files"].append({"path": str(p), "name": p.name})
        for m in volume_re.finditer(text.replace("\\\n", " ")):
            val = m.group(1).strip().strip('"\'')
            if ":" in val:
                host, cont = val.split(":", 1)[:2]
                info["volumes"].append({"file": str(p), "host": host, "container": cont})
        for m in image_re.finditer(text):
            img = m.group(1).strip()
            if ":" in img or "/" in img:
                info["images"].append({"file": str(p), "image": img})
    return info


def extract_side_context(profile: Dict[str, Any], upstream: Dict[str, Any], side: str) -> Dict[str, Any]:
    t = side_target(profile, side)
    u = side_target(upstream, side)
    container = t.get("container") if isinstance(t.get("container"), dict) else {}
    runtime = t.get("runtime") if isinstance(t.get("runtime"), dict) else {}
    prof = t.get("profile") if isinstance(t.get("profile"), dict) else {}
    override = t.get("override") if isinstance(t.get("override"), dict) else {}
    u_container = u.get("container") if isinstance(u.get("container"), dict) else {}
    u_runtime = u.get("runtime") if isinstance(u.get("runtime"), dict) else {}

    info: Dict[str, Any] = {
        "container": first_str(
            get_path(t, "container.name"), t.get("container_name"), t.get("container"),
            get_path(u, "container.name"), u.get("container_name"), u.get("container"),
        ),
        "workdir": first_str(
            get_path(t, "runtime.workdir"), t.get("workdir"),
            get_path(u, "runtime.workdir"), u.get("workdir"),
        ),
        "python_bin_in_container": first_str(
            get_path(t, "runtime.python_bin_in_container"), runtime.get("python"), runtime.get("python_bin"),
            get_path(u, "runtime.python_bin_in_container"), u_runtime.get("python"),
        ),
        "host_output_dir": first_str(get_path(t, "profile.host_output_dir"), prof.get("host_output_dir")),
        "output_db_name": first_str(get_path(t, "profile.output_db_name"), prof.get("output_db_name")),
        "stop_after_run": get_path(t, "container.stop_after_run") if get_path(t, "container.stop_after_run") is not None else container.get("stop_after_run"),
        "stop_timeout_s": get_path(t, "container.stop_timeout_s") if get_path(t, "container.stop_timeout_s") is not None else container.get("stop_timeout_s"),
        "model_path": first_str(get_path(t, "override.model_path"), override.get("model_path"), get_path(u, "override.model_path")),
        "image": first_str(get_path(u, "image"), get_path(u, "container.image"), u.get("docker_image"), u.get("image")),
    }
    # Recursion fallbacks for unknown shapes.
    if not info["container"]:
        vals = recursive_values(t, ["name", "container_name"])
        vals = [v for v in vals if isinstance(v, str) and side in v.lower()]
        info["container"] = vals[0] if vals else None
    if not info["workdir"]:
        vals = recursive_values(t, ["workdir", "working_dir"])
        vals = [v for v in vals if isinstance(v, str) and v.startswith("/")]
        info["workdir"] = vals[0] if vals else None
    return info


def _pick_json_file(paths: List[Path], *, preferred_names: List[str], reject_words: List[str]) -> Optional[Path]:
    by_name = {p.name: p for p in paths}
    for name in preferred_names:
        if name in by_name:
            return by_name[name]
    for p in paths:
        low = p.name.lower()
        if any(w in low for w in reject_words):
            continue
        return p
    return paths[0] if paths else None


def _load_profile_runner(profile_dir: Path):
    for name in ["profile_runner.py", "profile_runner_json_file_blocks.py"]:
        path = profile_dir / name
        if not path.exists():
            continue
        spec = importlib.util.spec_from_file_location("auto_profile_runner_for_context", path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            if hasattr(mod, "build_effective_target_config"):
                return mod
    return None


def _build_effective_targets(profile_dir: Path, local_path: Optional[Path], upstream_path: Optional[Path],
                             override_path: Optional[Path], local_obj: Dict[str, Any],
                             upstream_obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Use the auto profile runner's own default expansion when possible.

    local_required.json is intentionally tiny.  A raw JSON parser cannot know
    the derived container name, workdir, output_db_name, host_output_dir, etc.
    The runner does know these defaults, so reuse it here instead of
    reimplementing the profile contract in the agent layer.
    """
    if not local_obj or not upstream_obj or not local_path or not upstream_path:
        return None
    try:
        runner = _load_profile_runner(profile_dir)
        if runner is None:
            return None
        # Newer runners expose build_effective_config_bundle(upstream, local, target, override).
        # Use it so agent context sees the same override.json semantics as auto_probe.
        if hasattr(runner, "build_effective_config_bundle"):
            bundle = runner.build_effective_config_bundle(upstream_path, local_path, "both", override_path)
            targets = bundle.get("targets")
            if isinstance(targets, dict):
                return {side: targets.get(side, {}) or {} for side in SIDE_KEYS}
        # Backward-compatible fallback for older runner versions.
        upstream = dict(upstream_obj)
        upstream["__base_dir__"] = str(upstream_path.resolve().parent)
        override_obj = runner.load_optional_override_json(override_path) if hasattr(runner, "load_optional_override_json") else {}
        config = runner.merge_local_and_override_config(local_obj, override_obj) if hasattr(runner, "merge_local_and_override_config") else local_obj
        out: Dict[str, Any] = {}
        for side in SIDE_KEYS:
            out[side] = runner.build_effective_target_config(
                upstream, config, side, local_path.resolve().parent)
        return out
    except Exception:
        # Tolerant by design: fall back to best-effort raw JSON parsing.
        return None


def build_profile_context(profile_dir: Path) -> Dict[str, Any]:
    profile_dir = Path(profile_dir).resolve()
    groups = find_side_related_json(profile_dir)
    all_json = groups["all"]

    local_path = _pick_json_file(
        all_json,
        preferred_names=["local_required.json", "local_profile.json", "local.json"],
        reject_words=["example", "legacy", "upstream"],
    )
    upstream_path = _pick_json_file(
        all_json,
        preferred_names=["upstream.json"],
        reject_words=["example", "legacy", "local"],
    )
    override_path = _pick_json_file(
        groups.get("override", []),
        preferred_names=["override.json"],
        reject_words=["example", "legacy"],
    )

    local_obj = load_json_file(local_path) if local_path else None
    upstream_obj = load_json_file(upstream_path) if upstream_path else None
    local_obj = local_obj if isinstance(local_obj, dict) else {}
    upstream_obj = upstream_obj if isinstance(upstream_obj, dict) else {}

    effective_targets = _build_effective_targets(profile_dir, local_path, upstream_path, override_path, local_obj, upstream_obj)
    context_profile = {"targets": effective_targets} if effective_targets else local_obj

    docker_info = parse_docker_run_files(profile_dir)
    sides = {side: extract_side_context(context_profile, upstream_obj, side) for side in SIDE_KEYS}
    # Attach a few high-value fields directly from upstream/effective configs.
    if effective_targets:
        for side in SIDE_KEYS:
            eff = effective_targets.get(side, {}) or {}
            upstream_side = (upstream_obj.get("targets", {}) or {}).get(side, {}) or {}
            if not sides[side].get("image"):
                sides[side]["image"] = upstream_side.get("image")
            sides[side]["release_name"] = upstream_obj.get("release_name") or local_obj.get("release_name")
            sides[side]["served_model_name"] = ((eff.get("override") or {}).get("served_model_name"))
            sides[side]["client_host"] = ((eff.get("override") or {}).get("client_host"))
            sides[side]["port"] = ((eff.get("override") or {}).get("port"))
            sides[side]["profile_dir_in_container"] = ((eff.get("profile") or {}).get("profiler_dir"))
    ctx = {
        "schema_version": 2,
        "kind": "ascend_profile_runner_container_context",
        "profile_dir": str(profile_dir),
        "config_files": {
            "local_profile": str(local_path) if local_path else None,
            "upstream": str(upstream_path) if upstream_path else None,
            "json_files": [str(p) for p in groups["all"]],
        },
        "sides": sides,
        "docker_hints": docker_info,
        "safety": {
            "do_not_start_both_containers": True,
            "default_source_access": "read_only",
            "verification_requires_explicit_yes": True,
            "do_not_start_vllm_from_agent": True,
        },
    }
    return ctx


def detect_db_pair(db_dir: Path, context: Optional[Dict[str, Any]] = None) -> Tuple[Path, Path]:
    db_dir = Path(db_dir).resolve()
    dbs = sorted([p for p in db_dir.glob("*.db") if p.is_file()])
    if not dbs:
        raise FileNotFoundError(f"No .db files found under {db_dir}")
    def find_side(side: str) -> Optional[Path]:
        if context:
            name = (context.get("sides", {}).get(side, {}) or {}).get("output_db_name")
            if name:
                for p in dbs:
                    if p.name == name or p.name.endswith(str(name)):
                        return p
        kws = [side]
        if side == "vendor":
            kws += ["origin", "nogems", "no_gems", "baseline"]
        for p in dbs:
            low = p.name.lower()
            if any(k in low for k in kws):
                return p
        return None
    gems = find_side("gems")
    vendor = find_side("vendor")
    if gems and vendor and gems != vendor:
        return gems, vendor
    if len(dbs) == 2:
        a, b = dbs
        # Prefer name hints if one side missing.
        if gems == a or ("gems" in a.name.lower()):
            return a, b
        if gems == b or ("gems" in b.name.lower()):
            return b, a
    raise RuntimeError(
        f"Could not unambiguously detect Gems/Vendor DB pair under {db_dir}.\n"
        f"Found: {[p.name for p in dbs]}\n"
        "Use --gems-db and --vendor-db with run_compare_from_db.py, or name files with gems/vendor."
    )


def write_context(path: Path, context: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(context, f, ensure_ascii=False, indent=2)
