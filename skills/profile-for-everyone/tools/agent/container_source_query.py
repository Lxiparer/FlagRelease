#!/usr/bin/env python3
"""Controlled container source access for Gems/Vendor triage.

This tool is intentionally conservative:
  - it reads container context from <run-dir>/agent_container_context.json;
  - it uses a filesystem lock so only one side is accessed at a time;
  - it refuses to run when the opposite container is already running unless the
    user explicitly allows it;
  - source/snapshot modes only run read-only commands;
  - verify mode is opt-in and requires --yes.

It is designed for Claude Code to inspect source without freely exploring or
accidentally starting both GPU-owning containers.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def load_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def run_cmd(cmd: List[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print("[cmd] " + " ".join(shlex.quote(x) for x in cmd), file=sys.stderr)
    return subprocess.run(cmd, check=check, text=True, capture_output=capture)


def docker_out(args: List[str], *, check: bool = True) -> str:
    cp = run_cmd(["docker", *args], check=check, capture=True)
    return cp.stdout.strip()


def read_context(run_dir: Path) -> Dict[str, Any]:
    p = run_dir / "agent_container_context.json"
    if not p.exists():
        raise FileNotFoundError(f"Missing container context: {p}. Re-run compare with profile auto config or provide container context.")
    data = load_json(p)
    if "sides" not in data:
        # v0 compatibility: allow top-level vendor/gems.
        data = {"sides": {k: v for k, v in data.items() if k in {"gems", "vendor"}}}
    return data


class AccessLock:
    def __init__(self, run_dir: Path, side: str, container: str):
        self.path = run_dir / ".container_access.lock"
        self.side = side
        self.container = container
        self.fd: Optional[int] = None

    def __enter__(self):
        payload = f"side={self.side}\ncontainer={self.container}\npid={os.getpid()}\ntime={time.time()}\n"
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, payload.encode("utf-8"))
            os.close(self.fd)
            self.fd = None
        except FileExistsError:
            existing = ""
            try:
                existing = self.path.read_text(encoding="utf-8")
            except Exception:
                pass
            raise RuntimeError(f"Another container access appears active: {self.path}\n{existing}")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def side_info(ctx: Dict[str, Any], side: str) -> Dict[str, Any]:
    sides = ctx.get("sides", {})
    if side not in sides:
        raise KeyError(f"side={side!r} not found in agent_container_context.json; available={list(sides)}")
    info = sides[side]
    if not info.get("container"):
        raise KeyError(f"No container name for side={side}; context={info}")
    return info


def is_running(container: str) -> bool:
    try:
        out = docker_out(["inspect", "-f", "{{.State.Running}}", container], check=True)
        return out.strip().lower() == "true"
    except subprocess.CalledProcessError:
        return False


def ensure_single_side(ctx: Dict[str, Any], side: str, *, allow_other_running: bool = False) -> None:
    if allow_other_running:
        return
    for other_side, info in (ctx.get("sides") or {}).items():
        if other_side == side:
            continue
        cont = info.get("container")
        if cont and is_running(cont):
            raise RuntimeError(
                f"Refusing to access {side}: other side container is running: {other_side}={cont}.\n"
                "This system may not support two NPU containers at once. Stop the other container first, "
                "or pass --allow-other-running if you intentionally want to override."
            )


def maybe_start(container: str, *, no_start: bool = False) -> bool:
    if is_running(container):
        return False
    if no_start:
        raise RuntimeError(f"Container {container} is not running and --no-start was set")
    run_cmd(["docker", "start", container], check=True)
    return True


def maybe_stop(container: str, started: bool, *, keep_running: bool = False) -> None:
    if started and not keep_running:
        run_cmd(["docker", "stop", container], check=False)


def q(s: str) -> str:
    return shlex.quote(str(s))


def exec_bash(container: str, script: str, *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return run_cmd(["docker", "exec", container, "bash", "-lc", script], check=check, capture=capture)


def resolve_container_path(container: str, workdir: str, requested: str) -> str:
    """Resolve a user/profile path to a readable file path inside container."""
    requested = str(requested)
    candidates: List[str] = []
    if requested.startswith("/"):
        candidates.append(requested)
        # If profile path has a different absolute root, also try basename under workdir.
        candidates.append(str(Path(workdir) / Path(requested).name))
    else:
        candidates.append(str(Path(workdir) / requested))
        candidates.append(requested)
    # Also strip common profile prefixes when workdir contains a checkout.
    for prefix in ["/vllm-workspace/", "/workspace/"]:
        if requested.startswith(prefix):
            tail = requested[len(prefix):]
            candidates.append(str(Path(workdir) / tail))
            candidates.append(str(Path(workdir) / Path(tail).name))
    seen = set()
    candidates = [x for x in candidates if not (x in seen or seen.add(x))]
    test_script = "\n".join([f"if test -f {q(c)}; then echo {q(c)}; exit 0; fi" for c in candidates])
    cp = exec_bash(container, test_script + "\nexit 1", check=False, capture=True)
    if cp.returncode == 0 and cp.stdout.strip():
        return cp.stdout.strip().splitlines()[-1]
    # Last resort: find by basename under workdir, bounded enough for source trees.
    basename = Path(requested).name
    find_script = f"find {q(workdir)} -name {q(basename)} -type f 2>/dev/null | head -20"
    cp = exec_bash(container, find_script, check=False, capture=True)
    hits = [x for x in cp.stdout.splitlines() if x.strip()]
    if hits:
        return hits[0]
    raise FileNotFoundError(f"Could not resolve {requested!r} inside {container}; tried candidates={candidates}")


def print_file_context(container: str, path: str, *, line: Optional[int], context_lines: int) -> None:
    path_q = q(path)
    if line is None:
        script = f"nl -ba {path_q} | sed -n '1,200p'"
    else:
        start = max(1, int(line) - int(context_lines))
        end = int(line) + int(context_lines)
        script = f"nl -ba {path_q} | sed -n '{start},{end}p'"
    cp = exec_bash(container, script, check=True, capture=True)
    print(cp.stdout, end="")


def copy_file_from_container(container: str, container_path: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(["docker", "cp", f"{container}:{container_path}", str(dest)], check=True)


def extract_source_locations(packet: Dict[str, Any], side: str, limit: int = 20) -> List[Dict[str, Any]]:
    return list((packet.get("source_locations", {}) or {}).get(side, []) or [])[:limit]


def semantic_packet(run_dir: Path, rank: Optional[int], semantic_key: Optional[str], repo_root: Path) -> Dict[str, Any]:
    query = repo_root / "agent" / "semantic_perf_query.py"
    cmd = [sys.executable, str(query), "describe", "--run-dir", str(run_dir), "--format", "json"]
    if rank is not None:
        cmd.extend(["--rank", str(rank)])
    elif semantic_key:
        cmd.extend(["--semantic-key", semantic_key])
    else:
        raise ValueError("snapshot requires --rank or --semantic-key")
    cp = run_cmd(cmd, capture=True, check=True)
    return json.loads(cp.stdout)


def command_show_context(args: argparse.Namespace) -> None:
    ctx = read_context(Path(args.run_dir).resolve())
    print(json.dumps(ctx, ensure_ascii=False, indent=2))


def command_source(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    ctx = read_context(run_dir)
    info = side_info(ctx, args.side)
    container = info["container"]
    workdir = info.get("workdir") or "/"
    with AccessLock(run_dir, args.side, container):
        ensure_single_side(ctx, args.side, allow_other_running=args.allow_other_running)
        started = maybe_start(container, no_start=args.no_start)
        try:
            path = resolve_container_path(container, workdir, args.path)
            print(f"# {args.side}: {container}:{path}")
            print_file_context(container, path, line=args.line, context_lines=args.context_lines)
        finally:
            maybe_stop(container, started, keep_running=args.keep_running)


def command_snapshot(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    ctx = read_context(run_dir)
    packet = semantic_packet(run_dir, args.rank, args.semantic_key, repo_root)
    sides = [args.side] if args.side in {"gems", "vendor"} else ["gems", "vendor"]
    manifest: Dict[str, Any] = {
        "semantic_key": packet.get("semantic_key"),
        "display_path": packet.get("display_path"),
        "rank": args.rank,
        "files": [],
    }
    for side in sides:
        info = side_info(ctx, side)
        container = info["container"]
        workdir = info.get("workdir") or "/"
        locs = extract_source_locations(packet, side, limit=args.limit)
        with AccessLock(run_dir, side, container):
            ensure_single_side(ctx, side, allow_other_running=args.allow_other_running)
            started = maybe_start(container, no_start=args.no_start)
            try:
                for loc in locs:
                    raw = loc.get("file")
                    if not raw:
                        continue
                    try:
                        cpath = resolve_container_path(container, workdir, raw)
                    except Exception as e:
                        manifest["files"].append({"side": side, "profile_path": raw, "error": str(e)})
                        continue
                    rel = cpath.lstrip("/")
                    dest = run_dir / "source_snapshot" / side / rel
                    copy_file_from_container(container, cpath, dest)
                    manifest["files"].append({
                        "side": side,
                        "profile_path": raw,
                        "container": container,
                        "container_path": cpath,
                        "local_path": str(dest),
                        "line": loc.get("line"),
                        "func": loc.get("func"),
                        "time_ns": loc.get("time_ns"),
                        "count": loc.get("count"),
                    })
            finally:
                maybe_stop(container, started, keep_running=args.keep_running)
    out = run_dir / "source_snapshot" / "manifest.json"
    save_json(out, manifest)
    print(f"[+] wrote {out}")
    for item in manifest["files"]:
        if "local_path" in item:
            print(f"{item['side']}: {item['local_path']}")
        else:
            print(f"{item['side']}: {item.get('profile_path')} ERROR {item.get('error')}")


def command_verify(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    ctx = read_context(run_dir)
    info = side_info(ctx, args.side)
    container = info["container"]
    workdir = info.get("workdir") or "/"
    script = Path(args.script).resolve()
    if not script.exists():
        raise FileNotFoundError(script)
    exp_dir = run_dir / "agent_experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    remote_script = f"/tmp/agent_verify_{stamp}_{script.name}"
    print("# Verification plan")
    print(f"side: {args.side}")
    print(f"container: {container}")
    print(f"workdir: {workdir}")
    print(f"script: {script}")
    print("This may execute code inside the container. It will not start the other side container.")
    if not args.yes:
        print("Refusing to run without --yes.")
        return
    with AccessLock(run_dir, args.side, container):
        ensure_single_side(ctx, args.side, allow_other_running=args.allow_other_running)
        started = maybe_start(container, no_start=args.no_start)
        try:
            run_cmd(["docker", "cp", str(script), f"{container}:{remote_script}"], check=True)
            cmd = f"cd {q(workdir)} && {q(args.python_in_container)} {q(remote_script)}"
            cp = exec_bash(container, cmd, check=False, capture=True)
            stdout_p = exp_dir / f"{stamp}_{args.side}_verify.stdout"
            stderr_p = exp_dir / f"{stamp}_{args.side}_verify.stderr"
            meta_p = exp_dir / f"{stamp}_{args.side}_verify.json"
            stdout_p.write_text(cp.stdout, encoding="utf-8")
            stderr_p.write_text(cp.stderr, encoding="utf-8")
            save_json(meta_p, {"side": args.side, "container": container, "returncode": cp.returncode, "script": str(script), "remote_script": remote_script})
            print(f"[+] stdout {stdout_p}")
            print(f"[+] stderr {stderr_p}")
            print(f"[+] meta   {meta_p}")
            if cp.returncode != 0:
                raise SystemExit(cp.returncode)
        finally:
            maybe_stop(container, started, keep_running=args.keep_running)


def add_container_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--run-dir", required=True)
    p.add_argument("--allow-other-running", action="store_true", help="Override safety check that the opposite side container is stopped")
    p.add_argument("--no-start", action="store_true", help="Do not docker start a stopped target container")
    p.add_argument("--keep-running", action="store_true", help="If this tool started the target container, do not stop it afterwards")


def main() -> None:
    ap = argparse.ArgumentParser(description="Controlled container source access for Ascend compare triage")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("show-context", help="Print agent_container_context.json")
    p.add_argument("--run-dir", required=True)

    p = sub.add_parser("source", help="Print source context from one side container")
    add_container_flags(p)
    p.add_argument("--side", choices=["gems", "vendor"], required=True)
    p.add_argument("--path", required=True, help="Container path, profile path, or path relative to side workdir")
    p.add_argument("--line", type=int, default=None)
    p.add_argument("--context-lines", type=int, default=40)

    p = sub.add_parser("snapshot", help="Copy source files referenced by a semantic target to run-dir/source_snapshot")
    add_container_flags(p)
    p.add_argument("--side", choices=["gems", "vendor", "both"], default="both")
    p.add_argument("--rank", type=int, default=None)
    p.add_argument("--semantic-key", default=None)
    p.add_argument("--repo-root", default=None)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("verify", help="Run an explicit verification script in one side container; requires --yes")
    add_container_flags(p)
    p.add_argument("--side", choices=["gems", "vendor"], required=True)
    p.add_argument("--script", required=True, help="Local Python script to docker cp and run inside the side container")
    p.add_argument("--python-in-container", default="python")
    p.add_argument("--yes", action="store_true")

    args = ap.parse_args()
    if args.cmd == "show-context":
        command_show_context(args)
    elif args.cmd == "source":
        command_source(args)
    elif args.cmd == "snapshot":
        command_snapshot(args)
    elif args.cmd == "verify":
        command_verify(args)


if __name__ == "__main__":
    main()
