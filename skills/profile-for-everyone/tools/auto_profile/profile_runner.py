#!/usr/bin/env python3
"""
H100 / Hygon vLLM profile runner — host-side orchestrator.

Restored from the Ascend v27 profile_runner.py so the host-side contract, config
schema and CLI stay faithful to v27. Only the platform-specific parts differ, and
they are exactly the ones already agreed for H100/Hygon:

  1. No analyse.py / torch_npu post-processing.
     vLLM's torch profiler writes *.pt.trace.json.gz directly into ./vllm_profile.
  2. stop_profile can take >1h on H100/Hygon.
     We POST /stop_profile with a long timeout, then poll the container for the
     json.gz files to appear and for their sizes to stabilize. Size-stable means
     stop_profile finished flushing the trace.
  3. We then pick the rank0 trace (rank0 / rank-0 / rank_0, optionally carrying a
     dp0-ep0-pp0 prefix), docker-cp it to the host and rename it to
     <target>.pt.trace.json.gz under host_output_dir (default ./traceSource_both).
  4. Devices (NVIDIA --gpus / Hygon device flags) live in *_docker_run.sh, not here.

Everything else (config defaults, deep-merge override semantics, container
lifecycle, vLLM command rewriting, warmup/ready logic) is kept identical to v27.

Host-side entry (same flags as v27):
    python profile_runner.py run \
      --upstream upstream.json \
      --config local_required.json \
      --override override.json \
      --target both
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)


def log(msg: str) -> None:
    print(f"[profile-runner] {msg}", flush=True)


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def resolve_text_field(config_obj: Dict[str, Any], field: str, base_dir: Path) -> str:
    """Read a text block from either `field` or `field_file`."""
    direct = config_obj.get(field)
    file_ref = config_obj.get(f"{field}_file")

    if direct is not None and file_ref is not None:
        raise ValueError(f"Only one of `{field}` and `{field}_file` can be set")

    if direct is not None:
        if not isinstance(direct, str):
            raise TypeError(f"`{field}` must be a string")
        return direct

    if file_ref is not None:
        file_path = Path(str(file_ref))
        if not file_path.is_absolute():
            file_path = base_dir / file_path
        return file_path.read_text(encoding="utf-8")

    raise KeyError(f"Missing `{field}` or `{field}_file`")


def resolve_target_or_upstream_text(
    upstream: Dict[str, Any],
    target_info: Dict[str, Any],
    field: str,
    base_dir: Path,
) -> str:
    """Prefer target-specific text/file fields, fallback to upstream root fields."""
    has_target_field = field in target_info or f"{field}_file" in target_info
    if has_target_field:
        return resolve_text_field(target_info, field, base_dir)
    return resolve_text_field(upstream, field, base_dir)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _is_empty_override_value(value: Any) -> bool:
    """Return True for placeholder values in override.json (null / blank / empty)."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (dict, list)) and len(value) == 0:
        return True
    return False


def prune_empty_overrides(obj: Any) -> Any:
    """Recursively remove blank placeholder values from override.json."""
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            pv = prune_empty_overrides(v)
            if _is_empty_override_value(pv):
                continue
            out[k] = pv
        return out
    if isinstance(obj, list):
        return [prune_empty_overrides(v) for v in obj if not _is_empty_override_value(prune_empty_overrides(v))]
    return obj


def load_optional_override_json(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {}
    path = Path(path)
    if not path.exists():
        return {}
    data = load_json(path)
    return prune_empty_overrides(data)


def merge_local_and_override_config(local_config: Dict[str, Any], override_config: Dict[str, Any]) -> Dict[str, Any]:
    """Merge local_required.json with (pruned) override.json; override wins."""
    if not override_config:
        return local_config
    return deep_merge(local_config, override_config)


def slugify_name(value: str) -> str:
    value = value.strip().replace("/", "_").replace(" ", "_")
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "model"


def path_from_config_base(raw: str, config_base_dir: Path) -> str:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config_base_dir / path
    return str(path.resolve())


def pick_release_name(upstream: Dict[str, Any], config: Dict[str, Any], target_input: Dict[str, Any]) -> str:
    local_name = config.get("release_name")
    if isinstance(local_name, str) and local_name.strip():
        return local_name.strip()
    upstream_name = upstream.get("release_name")
    if isinstance(upstream_name, str) and upstream_name.strip():
        return upstream_name.strip()
    model_path = target_input.get("model_path") or target_input.get("override", {}).get("model_path")
    if isinstance(model_path, str) and model_path.strip():
        return Path(model_path.rstrip("/")).name
    raise KeyError("Missing release_name and model_path; cannot derive defaults")


def minimal_target_input(config: Dict[str, Any], target: str) -> Dict[str, Any]:
    targets = config.get("targets")
    if isinstance(targets, dict):
        item = targets.get(target, {})
        if item is None:
            item = {}
        if not isinstance(item, dict):
            raise TypeError(f"local config targets.{target} must be an object")
        return item
    if target != str(config.get("target", target)):
        raise KeyError(f"single-target config cannot run target={target}")
    return config


def _dict_or_empty(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str_from_maybe_container(value: Any) -> Optional[str]:
    """Allow either {"name": "..."} or "container_name" style."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


# NOTE: "analyse" intentionally dropped vs v27 — H100/Hygon has no analyse.py step.
NESTED_OVERRIDE_BLOCKS = (
    "vllm_rewrite",
    "profile",
    "timeouts",
    "requests",
    "logging",
    "precheck",
    "container",
    "runtime",
    "override",
)


def collect_nested_overrides(config: Dict[str, Any], raw_target: Dict[str, Any]) -> Dict[str, Any]:
    """Collect optional nested overrides without making them mandatory.

    Order:
      1. defaults are generated first;
      2. root-level blocks in local_required.json override generated defaults;
      3. targets.<side> blocks override root-level blocks;
      4. targets.<side>.advanced is kept as a backwards-compatible final merge.
    """
    merged: Dict[str, Any] = {}
    for source in (config, raw_target):
        for block in NESTED_OVERRIDE_BLOCKS:
            value = source.get(block)
            if isinstance(value, dict):
                merged = deep_merge(merged, {block: value})

    advanced = raw_target.get("advanced", {})
    if advanced is None:
        advanced = {}
    if not isinstance(advanced, dict):
        raise TypeError("targets.<side>.advanced must be an object if provided")
    merged = deep_merge(merged, advanced)
    return merged


def build_effective_target_config(
    upstream: Dict[str, Any],
    config: Dict[str, Any],
    target: str,
    config_base_dir: Path,
) -> Dict[str, Any]:
    """Build full target config from a minimal local_required.json.

    Minimal expected shape:
      {
        "target": "both",
        "targets": {
          "vendor": {"model_path": "/data/YourModel"},
          "gems":   {"model_path": "/data/YourModel"}
        }
      }
    """
    raw_target = minimal_target_input(config, target)

    # If a fully-expanded config is provided, still support it.
    has_old_shape = all(k in raw_target for k in ("container", "runtime", "override", "profile", "requests"))
    if has_old_shape:
        return raw_target

    model_path = raw_target.get("model_path") or raw_target.get("override", {}).get("model_path")
    if not isinstance(model_path, str) or not model_path.strip():
        raise KeyError(f"local config must set targets.{target}.model_path")
    model_path = model_path.strip()

    release_name = pick_release_name(upstream, config, raw_target)
    release_slug = slugify_name(release_name)
    served_model_name = str(
        raw_target.get("served_model_name")
        or config.get("served_model_name")
        or release_name
    )

    host = str(raw_target.get("host") or config.get("host") or "0.0.0.0")
    client_host = str(raw_target.get("client_host") or config.get("client_host") or "127.0.0.1")
    port = int(raw_target.get("port") or config.get("port") or 9181)

    # H100/Hygon default output dir: ./traceSource_both (v27 used ../DBSource_both).
    output_dir_raw = str(
        raw_target.get("host_output_dir")
        or config.get("host_output_dir")
        or "./traceSource_both"
    )
    log_dir_raw = str(
        raw_target.get("host_log_dir")
        or config.get("host_log_dir")
        or "./profile_runner_logs"
    )

    # On H100/Hygon the host artifact is a renamed json.gz, not a .db.
    trace_output_name = str(
        raw_target.get("trace_output_name")
        or f"{target}.pt.trace.json.gz"
    )

    raw_container = raw_target.get("container")
    raw_runtime = _dict_or_empty(raw_target.get("runtime"))
    container_name = str(
        raw_target.get("container_name")
        or _str_from_maybe_container(raw_container)
        or f"{release_slug}_{target}"
    )
    workdir = str(
        raw_target.get("workdir")
        or raw_runtime.get("workdir")
        or f"/workspace/{release_slug}_{target}"
    )

    prompt_warmup = raw_target.get("warmup_prompt") or config.get("warmup_prompt") or "Hi boy"
    prompt_profile = raw_target.get("profile_prompt") or config.get("profile_prompt") or "Well kids today I wanna show you the story how America build from"
    max_tokens = int(raw_target.get("max_tokens") or config.get("max_tokens") or 128)

    defaults: Dict[str, Any] = {
        "vllm_rewrite": {
            "remove_args": ["--profiler-config", "--host", "--port", "--served-model-name"],
            "keep_unknown_args": True,
        },
        "profile": {
            "profiler_dir": "./vllm_profile",
            "host_output_dir": path_from_config_base(output_dir_raw, config_base_dir),
            "wait_after_stop_profile_s": int(raw_target.get("wait_after_stop_profile_s") or config.get("wait_after_stop_profile_s") or 30),
            "profile_request_count": int(raw_target.get("profile_request_count") or config.get("profile_request_count") or 1),
            "profiler_config": {
                "profiler": "torch",
                "torch_profiler_dir": "./vllm_profile",
                "torch_profiler_with_stack": True,
                "torch_profiler_record_shapes": True,
            },
            # H100/Hygon specific: how long to wait for stop_profile to finish
            # flushing the json.gz, and how often to poll for size-stabilization.
            "trace_wait_timeout_s": int(raw_target.get("trace_wait_timeout_s") or config.get("trace_wait_timeout_s") or 7200),
            "trace_poll_interval_s": int(raw_target.get("trace_poll_interval_s") or config.get("trace_poll_interval_s") or 30),
            "trace_output_name": trace_output_name,
        },
        "timeouts": {
            "ready_s": int(raw_target.get("ready_s") or config.get("ready_s") or 3600),
            "http_request_s": int(raw_target.get("http_request_s") or config.get("http_request_s") or 600),
            "vllm_stop_s": int(raw_target.get("vllm_stop_s") or config.get("vllm_stop_s") or 180),
        },
        "requests": {
            "warmup": {
                "endpoint": "/v1/completions",
                "payload": {
                    "model": served_model_name,
                    "prompt": prompt_warmup,
                    "max_tokens": max_tokens,
                    "ignore_eos": True,
                },
            },
            "profile": {
                "endpoint": "/v1/completions",
                "payload": {
                    "model": served_model_name,
                    "prompt": prompt_profile,
                    "max_tokens": max_tokens,
                    "ignore_eos": True,
                },
            },
        },
        "logging": {
            "host_log_dir": path_from_config_base(log_dir_raw, config_base_dir),
            "stream_vllm_log_to_console": bool(raw_target.get("stream_vllm_log_to_console", config.get("stream_vllm_log_to_console", True))),
        },
        "precheck": {
            "require_model_config_json": bool(raw_target.get("require_model_config_json", config.get("require_model_config_json", True))),
        },
        "container": {
            "name": container_name,
            "start_if_stopped": bool(raw_target.get("start_if_stopped", config.get("start_if_stopped", True))),
            "create_if_missing": bool(raw_target.get("create_if_missing", config.get("create_if_missing", True))),
            "stop_after_run": bool(raw_target.get("stop_after_run", config.get("stop_after_run", True))),
            "stop_timeout_s": int(raw_target.get("stop_timeout_s") or config.get("stop_timeout_s") or 120),
        },
        "runtime": {
            "workdir": workdir,
            "python_bin_in_container": str(raw_target.get("python_bin_in_container") or config.get("python_bin_in_container") or "python3"),
        },
        "override": {
            "model_path": model_path,
            "served_model_name": served_model_name,
            "host": host,
            "port": port,
            "client_host": client_host,
        },
    }

    optional_overrides = collect_nested_overrides(config, raw_target)
    return deep_merge(defaults, optional_overrides)


def run_cmd(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture: bool = True,
    timeout: Optional[int] = None,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    log("+ " + shlex.join([str(x) for x in cmd]))
    cp = subprocess.run(
        list(map(str, cmd)),
        text=True,
        capture_output=capture,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )
    if check and cp.returncode != 0:
        if cp.stdout:
            eprint(cp.stdout)
        if cp.stderr:
            eprint(cp.stderr)
        raise RuntimeError(f"Command failed with exit code {cp.returncode}: {shlex.join([str(x) for x in cmd])}")
    return cp


def normalize_shell_command(command: str) -> str:
    command = command.replace("\\\r\n", " ")
    command = command.replace("\\\n", " ")
    return command


def shell_split(command: str) -> List[str]:
    normalized = normalize_shell_command(command)
    tokens = shlex.split(normalized, posix=True)
    filtered: List[str] = []
    dropped: List[str] = []
    for i, token in enumerate(tokens):
        if token.strip() == "":
            dropped.append(f"blank-token@{i}")
            continue
        if token == "\\":
            dropped.append(f"standalone-backslash@{i}")
            continue
        filtered.append(token)
    if dropped:
        log("Warning: dropped suspicious shell tokens while parsing command: " + ", ".join(dropped))
    return filtered


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def tail_text(path: Path, max_lines: int = 120) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return f"<log file not found: {path}>"
    except Exception as exc:
        return f"<failed to read log file {path}: {exc!r}>"
    return "\n".join(lines[-max_lines:])


def docker_container_exists(name: str) -> bool:
    cp = subprocess.run(
        ["docker", "inspect", "-f", "{{.Id}}", name],
        text=True,
        capture_output=True,
    )
    return cp.returncode == 0


def docker_container_running(name: str) -> bool:
    cp = run_cmd(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        check=True,
        capture=True,
    )
    return cp.stdout.strip().lower() == "true"


def normalize_docker_run_command(raw: str, container_name: str, image: str) -> List[str]:
    tokens = shell_split(raw)
    if len(tokens) < 3 or tokens[0] != "docker" or tokens[1] != "run":
        raise ValueError("upstream target docker_run must start with: docker run")

    out: List[str] = []
    patched_name = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--name":
            out.extend([tok, container_name])
            patched_name = True
            i += 2
            continue
        if tok.startswith("--name="):
            out.append(f"--name={container_name}")
            patched_name = True
            i += 1
            continue
        out.append(tok)
        i += 1

    if not patched_name:
        out = out[:2] + ["--name", container_name] + out[2:]

    return out


def ensure_container_for_target(upstream: Dict[str, Any], target_config: Dict[str, Any], target: str) -> str:
    target_info = upstream.get("targets", {}).get(target)
    if not isinstance(target_info, dict):
        raise KeyError(f"upstream.json missing targets.{target}")

    image = target_info.get("image")
    if not image:
        raise KeyError(f"upstream.json missing targets.{target}.image")

    container_cfg = target_config.get("container", {})
    container_name = container_cfg.get("name")
    if not container_name:
        raise KeyError("local config missing container.name")

    start_if_stopped = bool(container_cfg.get("start_if_stopped", True))
    create_if_missing = bool(container_cfg.get("create_if_missing", True))

    # Reuse an already-loaded container if it exists (v27 behavior).
    if docker_container_exists(container_name):
        if docker_container_running(container_name):
            log(f"Container already running (reuse existing): {container_name}")
            return container_name

        if not start_if_stopped:
            raise RuntimeError(f"Container exists but is not running, and start_if_stopped=false: {container_name}")

        log(f"Container exists but stopped. Starting: {container_name}")
        run_cmd(["docker", "start", container_name], check=True, capture=True)
        return container_name

    if not create_if_missing:
        raise RuntimeError(f"Container does not exist and create_if_missing=false: {container_name}")

    upstream_base_dir = Path(str(upstream.get("__base_dir__", ".")))
    docker_run = resolve_text_field(target_info, "docker_run", upstream_base_dir)

    log(f"Container does not exist. Creating from upstream docker_run: {container_name}")
    cmd = normalize_docker_run_command(str(docker_run), container_name, str(image))
    run_cmd(cmd, check=True, capture=True)
    return container_name


@dataclass
class CliArg:
    name: str
    value: Optional[str] = None

    def render(self) -> List[str]:
        if self.value is None:
            return [self.name]
        return [self.name, self.value]


@dataclass
class VllmCommand:
    model_path: str
    args: List[CliArg]

    def render_shell(self) -> str:
        parts = ["vllm", "serve", self.model_path]
        for arg in self.args:
            parts.extend(arg.render())
        return " ".join(shlex.quote(x) for x in parts)


def parse_vllm_serve(raw: str) -> VllmCommand:
    tokens = shell_split(raw)
    if len(tokens) < 3:
        raise ValueError("vllm_serve command is too short")
    if tokens[0:2] != ["vllm", "serve"]:
        raise ValueError(f"vllm_serve must start with 'vllm serve', got: {tokens[:2]}")

    model_path = tokens[2]
    rest = tokens[3:]

    args: List[CliArg] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if not tok.startswith("--"):
            args.append(CliArg(tok, None))
            i += 1
            continue

        if "=" in tok:
            name, value = tok.split("=", 1)
            args.append(CliArg(name, value))
            i += 1
            continue

        if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
            args.append(CliArg(tok, rest[i + 1]))
            i += 2
        else:
            args.append(CliArg(tok, None))
            i += 1

    return VllmCommand(model_path=model_path, args=args)


def rewrite_vllm_command(
    upstream: Dict[str, Any],
    target_info: Dict[str, Any],
    target_config: Dict[str, Any],
    upstream_base_dir: Path,
) -> VllmCommand:
    raw_vllm = resolve_target_or_upstream_text(upstream, target_info, "vllm_serve", upstream_base_dir)
    cmd = parse_vllm_serve(str(raw_vllm))

    override = target_config.get("override", {})
    profile = target_config.get("profile", {})
    rewrite = target_config.get("vllm_rewrite", {})

    remove_args = set(rewrite.get("remove_args", []) or [])
    force_args: Dict[str, str] = {
        "--host": str(override["host"]),
        "--port": str(override["port"]),
        "--served-model-name": str(override["served_model_name"]),
    }

    owned = remove_args | set(force_args.keys()) | {"--profiler-config"}
    kept_args = [a for a in cmd.args if a.name not in owned]

    profiler_config = profile.get("profiler_config")
    if not isinstance(profiler_config, dict):
        raise KeyError("local config missing profile.profiler_config")

    new_args = kept_args
    for k, v in force_args.items():
        new_args.append(CliArg(k, str(v)))

    new_args.append(CliArg("--profiler-config", json.dumps(profiler_config, ensure_ascii=False, separators=(",", ":"))))

    force_model_path = override.get("model_path")
    if not force_model_path:
        raise KeyError("local config missing override.model_path")

    return VllmCommand(model_path=str(force_model_path), args=new_args)


def make_run_vllm_script(
    *,
    workdir: str,
    setup_script: str,
    final_vllm_cmd: VllmCommand,
) -> str:
    return f"""#!/usr/bin/env bash
set -e

mkdir -p {shlex.quote(workdir)}
cd {shlex.quote(workdir)}

# ---- upstream setup ----
{setup_script.rstrip()}

# ---- rewritten vLLM command ----
echo "[container] launching vLLM..."
echo {shlex.quote(final_vllm_cmd.render_shell())}
exec {final_vllm_cmd.render_shell()}
"""


def docker_cp_to_container(container: str, host_path: Path, container_path: str) -> None:
    run_cmd(["docker", "cp", str(host_path), f"{container}:{container_path}"], check=True, capture=True)


def docker_cp_from_container(container: str, container_path: str, host_path: Path) -> None:
    ensure_dir(host_path.parent)
    run_cmd(["docker", "cp", f"{container}:{container_path}", str(host_path)], check=True, capture=True)


def docker_exec(container: str, args: Sequence[str], *, timeout: Optional[int] = None, capture: bool = True) -> subprocess.CompletedProcess:
    return run_cmd(["docker", "exec", container, *args], check=True, capture=capture, timeout=timeout)


def docker_exec_sh(container: str, script: str, *, check: bool = False, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """Run a `bash -lc` script inside the container. check=False by default because
    find/stat probes legitimately return non-zero while nothing matches yet."""
    return run_cmd(["docker", "exec", container, "bash", "-lc", script], check=check, capture=True, timeout=timeout)


def docker_stop_container(container: str, timeout_s: int = 120) -> None:
    # docker stop is done after the trace is copied out, so a stopped target
    # releases the GPU before the next target starts.
    try:
        if docker_container_exists(container) and docker_container_running(container):
            log(f"Stopping container to release GPU: {container}")
            run_cmd(["docker", "stop", "-t", str(timeout_s), container], check=True, capture=True)
        else:
            log(f"Container already stopped or missing: {container}")
    except Exception as exc:
        eprint(f"[profile-runner] WARNING: failed to docker stop {container}: {exc!r}")


def should_stop_container_after_target(target_config: Dict[str, Any]) -> bool:
    container_cfg = target_config.get("container", {})
    lifecycle_cfg = target_config.get("lifecycle", {})
    return bool(
        container_cfg.get("stop_after_run", lifecycle_cfg.get("stop_container_after_target", False))
    )


def container_stop_timeout_s(target_config: Dict[str, Any]) -> int:
    container_cfg = target_config.get("container", {})
    lifecycle_cfg = target_config.get("lifecycle", {})
    return int(container_cfg.get("stop_timeout_s", lifecycle_cfg.get("docker_stop_timeout_s", 120)))


def precheck_model_config(container: str, model_path: str) -> None:
    quoted = shlex.quote(model_path.rstrip("/"))
    cmd = f"test -d {quoted} && test -f {quoted}/config.json"
    cp = subprocess.run(["docker", "exec", container, "bash", "-lc", cmd], text=True, capture_output=True)
    if cp.returncode == 0:
        log(f"Model precheck passed: {model_path}/config.json")
        return

    listing = subprocess.run(
        ["docker", "exec", container, "bash", "-lc", "ls -ld /data /data/* 2>/dev/null | head -80"],
        text=True, capture_output=True,
    )
    raise RuntimeError(
        "Model path precheck failed inside container.\n"
        f"Expected: {model_path}/config.json\n"
        "Common cause: the docker -v mount changes the in-container path, so the host "
        "path and the container model_path differ. Check your *_docker_run.sh -v mounts.\n"
        "Container /data listing:\n" + (listing.stdout or listing.stderr or "<no listing>")
    )


def http_post_json(url: str, payload: Optional[Dict[str, Any]], timeout_s: int) -> Tuple[int, str]:
    data: Optional[bytes]
    headers = {}
    if payload is None:
        data = b""
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return int(resp.status), body


def wait_until_ready(
    *,
    client_host: str,
    port: int,
    endpoint: str,
    payload: Dict[str, Any],
    ready_timeout_s: int,
    request_timeout_s: int,
    proc: Optional[subprocess.Popen] = None,
    vllm_log: Optional[Path] = None,
) -> None:
    url = f"http://{client_host}:{port}{endpoint}"
    log(f"Waiting for vLLM ready via POST {url}")
    deadline = time.time() + ready_timeout_s
    last_err: Optional[str] = None

    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            tail = tail_text(vllm_log, 120) if vllm_log is not None else "<no vLLM log path>"
            raise RuntimeError(
                f"vLLM process exited before ready. exit_code={proc.returncode}.\n"
                f"Last vLLM log lines:\n{tail}"
            )
        try:
            status, _body = http_post_json(url, payload, request_timeout_s)
            if 200 <= status < 300:
                log("vLLM is ready")
                return
            last_err = f"HTTP {status}"
        except Exception as exc:
            last_err = repr(exc)
        time.sleep(5)

    tail = tail_text(vllm_log, 120) if vllm_log is not None else "<no vLLM log path>"
    raise TimeoutError(
        f"vLLM not ready after {ready_timeout_s}s. Last HTTP error: {last_err}.\n"
        f"Last vLLM log lines:\n{tail}"
    )


def start_vllm_process(
    *,
    container: str,
    script_path_in_container: str,
    log_file: Path,
    stream_to_console: bool,
) -> Tuple[subprocess.Popen, threading.Thread]:
    cmd = ["docker", "exec", container, "bash", script_path_in_container]
    log("+ " + shlex.join(cmd))
    f = log_file.open("w", encoding="utf-8", buffering=1)

    proc = subprocess.Popen(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )

    def _reader() -> None:
        assert proc.stdout is not None
        with f:
            for line in proc.stdout:
                f.write(line)
                if stream_to_console:
                    print("[vllm] " + line, end="", flush=True)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return proc, t


def stop_vllm_process(container: str, proc: subprocess.Popen, stop_timeout_s: int) -> None:
    if proc.poll() is not None:
        log(f"vLLM process already exited with code {proc.returncode}")
        return

    log("Stopping vLLM with SIGINT (Ctrl+C) via pkill -INT -f 'vllm serve'")
    subprocess.run(["docker", "exec", container, "pkill", "-INT", "-f", "vllm serve"], text=True)

    try:
        proc.wait(timeout=stop_timeout_s)
        log(f"vLLM process exited with code {proc.returncode}")
        return
    except subprocess.TimeoutExpired:
        log("vLLM did not exit after SIGINT. Sending SIGTERM.")
        subprocess.run(["docker", "exec", container, "pkill", "-TERM", "-f", "vllm serve"], text=True)

    try:
        proc.wait(timeout=30)
        log(f"vLLM process exited with code {proc.returncode}")
        return
    except subprocess.TimeoutExpired:
        log("vLLM did not exit after SIGTERM. Sending SIGKILL.")
        subprocess.run(["docker", "exec", container, "pkill", "-KILL", "-f", "vllm serve"], text=True)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log("Warning: local docker exec process still did not exit")


# ---------------------------------------------------------------------------
# H100 / Hygon trace collection (replaces v27's analyse.py post-processing)
# ---------------------------------------------------------------------------

def _abs_profiler_dir(workdir: str, profiler_dir: str) -> str:
    """Resolve the profiler dir to an absolute container path."""
    p = profiler_dir.strip()
    if p.startswith("/"):
        return p.rstrip("/")
    if p.startswith("./"):
        p = p[2:]
    return f"{workdir.rstrip('/')}/{p}".rstrip("/")


def _list_trace_sizes(container: str, profiler_dir_abs: str) -> Dict[str, int]:
    """Return {path: size_bytes} for every *.json.gz under profiler_dir_abs."""
    script = (
        f"find {shlex.quote(profiler_dir_abs)} -name '*.json.gz' -printf '%s\\t%p\\n' 2>/dev/null "
        f"|| find {shlex.quote(profiler_dir_abs)} -name '*.json.gz' -exec stat -c '%s\\t%n' {{}} + 2>/dev/null "
        f"|| true"
    )
    cp = docker_exec_sh(container, script, check=False)
    sizes: Dict[str, int] = {}
    for line in (cp.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        size_str, path = parts[0].strip(), parts[1].strip()
        try:
            sizes[path] = int(size_str)
        except ValueError:
            continue
    return sizes


def wait_for_trace_stable(
    container: str,
    profiler_dir_abs: str,
    *,
    timeout_s: int,
    poll_interval_s: int,
    stable_polls: int = 2,
) -> Dict[str, int]:
    """Poll the container until *.json.gz files exist and their sizes stop changing.

    On H100/Hygon stop_profile can take >1h; size-stability is our signal that the
    profiler has finished flushing the trace. Returns the final {path: size} map.
    """
    log(f"Polling for trace files in {profiler_dir_abs} (timeout={timeout_s}s, every {poll_interval_s}s)")
    deadline = time.time() + timeout_s
    last: Dict[str, int] = {}
    stable_count = 0

    while time.time() < deadline:
        cur = _list_trace_sizes(container, profiler_dir_abs)
        if cur and cur == last:
            stable_count += 1
            log(f"  ... {len(cur)} json.gz file(s), sizes unchanged ({stable_count}/{stable_polls})")
            if stable_count >= stable_polls:
                log(f"Trace files stabilized: {len(cur)} file(s).")
                return cur
        else:
            if cur:
                log(f"  ... {len(cur)} json.gz file(s) found, waiting for sizes to stabilize")
            else:
                log("  ... no json.gz yet, still waiting for stop_profile to flush")
            stable_count = 0
            last = cur
        time.sleep(poll_interval_s)

    if last:
        log(f"WARNING: trace size did not stabilize within {timeout_s}s; proceeding with {len(last)} file(s).")
        return last
    raise TimeoutError(f"No *.json.gz trace appeared in {container}:{profiler_dir_abs} within {timeout_s}s")


_RANK0_RE = re.compile(r"(?:^|[^0-9])rank[-_]?0(?:[^0-9]|$)", re.IGNORECASE)


def _is_rank0_name(path: str) -> bool:
    name = Path(path).name.lower()
    if _RANK0_RE.search(name):
        return True
    # parallel-prefix style, e.g. dp0-ep0-pp0_...rank...
    if "dp0" in name and "ep0" in name and "pp0" in name:
        return True
    return False


def find_rank0_trace(container: str, profiler_dir_abs: str) -> str:
    """Find the rank0 trace json.gz inside the container; return its container path.

    Prefers names matching rank0 / rank-0 / rank_0 (optionally with a dp0-ep0-pp0
    prefix). Falls back to the single / first file when no explicit rank0 is found.
    """
    script = f"find {shlex.quote(profiler_dir_abs)} -name '*.json.gz' 2>/dev/null | sort"
    cp = docker_exec_sh(container, script, check=False)
    files = [l.strip() for l in (cp.stdout or "").splitlines() if l.strip()]
    if not files:
        raise FileNotFoundError(f"No *.json.gz trace files found in {container}:{profiler_dir_abs}")

    for f in files:
        if _is_rank0_name(f):
            log(f"Selected rank0 trace: {f}")
            return f

    if len(files) == 1:
        log(f"No explicit rank0 match; single trace file: {files[0]}")
        return files[0]

    log(f"No explicit rank0 match among {len(files)} files; using first: {files[0]}")
    return files[0]


def run_target(upstream_path: Path, config_path: Path, target: str, override_path: Optional[Path] = None,
               host_output_dir_override: Optional[str] = None,
               host_log_dir_override: Optional[str] = None) -> Dict[str, Any]:
    upstream = load_json(upstream_path)
    upstream["__base_dir__"] = str(upstream_path.resolve().parent)
    local_config = load_json(config_path)
    override_config = load_optional_override_json(override_path)
    config = merge_local_and_override_config(local_config, override_config)

    target_info = upstream.get("targets", {}).get(target)
    if not isinstance(target_info, dict):
        raise KeyError(f"upstream.json missing targets.{target}")
    target_config = build_effective_target_config(upstream, config, target, config_path.resolve().parent)

    # Optional CLI override so an orchestrator can force the trace output location.
    if host_output_dir_override:
        target_config.setdefault("profile", {})["host_output_dir"] = str(
            Path(host_output_dir_override).expanduser().resolve()
        )
    if host_log_dir_override:
        target_config.setdefault("logging", {})["host_log_dir"] = str(
            Path(host_log_dir_override).expanduser().resolve()
        )

    container = ensure_container_for_target(upstream, target_config, target)

    runtime = target_config.get("runtime", {})
    workdir = runtime.get("workdir")
    if not workdir:
        raise KeyError("local config missing runtime.workdir")

    upstream_base_dir = upstream_path.resolve().parent
    # Prefer per-target setup_file/setup; fallback to setup_common_file/setup_common.
    try:
        setup_common = resolve_target_or_upstream_text(upstream, target_info, "setup", upstream_base_dir)
    except KeyError:
        setup_common = resolve_target_or_upstream_text(upstream, target_info, "setup_common", upstream_base_dir)

    final_vllm = rewrite_vllm_command(upstream, target_info, target_config, upstream_base_dir)

    precheck_cfg = target_config.get("precheck", {})
    if bool(precheck_cfg.get("require_model_config_json", True)):
        precheck_model_config(container, final_vllm.model_path)

    profile = target_config.get("profile", {})
    profiler_dir = profile.get("profiler_dir", "./vllm_profile")
    profiler_dir_abs = _abs_profiler_dir(str(workdir), str(profiler_dir))
    trace_output_name = profile.get("trace_output_name", f"{target}.pt.trace.json.gz")
    trace_wait_timeout_s = int(profile.get("trace_wait_timeout_s", 7200))
    trace_poll_interval_s = int(profile.get("trace_poll_interval_s", 30))
    wait_after_stop_profile_s = int(profile.get("wait_after_stop_profile_s", 30))

    host_output_dir = Path(profile.get("host_output_dir", "./traceSource_both")).expanduser().resolve()
    ensure_dir(host_output_dir)

    log_dir_root = Path(target_config.get("logging", {}).get("host_log_dir", "./profile_runner_logs")).expanduser().resolve()
    log_dir = log_dir_root / f"{target}_{now_stamp()}"
    ensure_dir(log_dir)

    run_vllm_script = make_run_vllm_script(
        workdir=str(workdir),
        setup_script=setup_common,
        final_vllm_cmd=final_vllm,
    )

    host_run_script = log_dir / f"run_vllm_{target}.sh"
    host_final_command = log_dir / "final_vllm_command.sh"
    host_run_script.write_text(run_vllm_script, encoding="utf-8")
    host_final_command.write_text(final_vllm.render_shell() + "\n", encoding="utf-8")
    log(f"Final rewritten vLLM command saved to: {host_final_command}")

    remote_run_script = f"/tmp/profile_runner_run_vllm_{target}.sh"
    docker_cp_to_container(container, host_run_script, remote_run_script)
    docker_exec(container, ["chmod", "+x", remote_run_script], capture=True)

    override = target_config.get("override", {})
    timeouts = target_config.get("timeouts", {})
    requests_cfg = target_config.get("requests", {})

    client_host = str(override.get("client_host", "127.0.0.1"))
    port = int(override["port"])
    ready_timeout_s = int(timeouts.get("ready_s", 3600))
    http_request_s = int(timeouts.get("http_request_s", 600))
    vllm_stop_s = int(timeouts.get("vllm_stop_s", 180))

    warmup = requests_cfg["warmup"]
    profile_req = requests_cfg["profile"]

    vllm_log = log_dir / "vllm.log"
    proc, reader_thread = start_vllm_process(
        container=container,
        script_path_in_container=remote_run_script,
        log_file=vllm_log,
        stream_to_console=bool(target_config.get("logging", {}).get("stream_vllm_log_to_console", True)),
    )

    host_trace_path = host_output_dir / str(trace_output_name)

    try:
        wait_until_ready(
            client_host=client_host,
            port=port,
            endpoint=str(warmup["endpoint"]),
            payload=dict(warmup["payload"]),
            ready_timeout_s=ready_timeout_s,
            request_timeout_s=http_request_s,
            proc=proc,
            vllm_log=vllm_log,
        )

        warmup_url = f"http://{client_host}:{port}{warmup['endpoint']}"
        profile_url = f"http://{client_host}:{port}{profile_req['endpoint']}"
        start_profile_url = f"http://{client_host}:{port}/start_profile"
        stop_profile_url = f"http://{client_host}:{port}/stop_profile"

        log("Sending warmup request")
        http_post_json(warmup_url, dict(warmup["payload"]), http_request_s)

        log("POST /start_profile")
        http_post_json(start_profile_url, None, http_request_s)

        profile_request_count = int(profile.get("profile_request_count", 1))
        for idx in range(profile_request_count):
            log(f"Sending profile request {idx + 1}/{profile_request_count}")
            http_post_json(profile_url, dict(profile_req["payload"]), http_request_s)

        # On H100/Hygon stop_profile can block for a very long time while it
        # serializes the trace; give it the full trace_wait_timeout_s budget.
        log("POST /stop_profile (H100/Hygon: this can take >1h)")
        http_post_json(stop_profile_url, None, trace_wait_timeout_s)

        log(f"Waiting {wait_after_stop_profile_s}s grace after stop_profile")
        time.sleep(wait_after_stop_profile_s)

        # Confirm stop_profile actually finished by polling json.gz size-stability
        # (vLLM is still alive here so the profiler can finish flushing).
        wait_for_trace_stable(
            container,
            profiler_dir_abs,
            timeout_s=trace_wait_timeout_s,
            poll_interval_s=trace_poll_interval_s,
        )

        rank0_container_path = find_rank0_trace(container, profiler_dir_abs)
        log(f"Copying trace to host: {container}:{rank0_container_path} -> {host_trace_path}")
        docker_cp_from_container(container, rank0_container_path, host_trace_path)

    finally:
        # Only after the trace is safely on disk do we Ctrl+C vLLM.
        stop_vllm_process(container, proc, vllm_stop_s)
        reader_thread.join(timeout=5)

    summary = {
        "target": target,
        "container": container,
        "workdir": workdir,
        "trace": str(host_trace_path),
        "log_dir": str(log_dir),
        "final_vllm_command_file": str(host_final_command),
        "vllm_log": str(vllm_log),
        "finished_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    (log_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if should_stop_container_after_target(target_config):
        docker_stop_container(container, container_stop_timeout_s(target_config))

    log(f"DONE target={target}")
    log(f"Trace: {host_trace_path}")
    log(f"Logs: {log_dir}")
    return summary


def build_effective_config_bundle(upstream_path: Path, config_path: Path, target_arg: Optional[str] = None,
                                  override_path: Optional[Path] = None) -> Dict[str, Any]:
    """Resolve defaults + local_required.json + override.json without touching Docker."""
    upstream = load_json(upstream_path)
    upstream["__base_dir__"] = str(upstream_path.resolve().parent)
    local_config = load_json(config_path)
    override_config = load_optional_override_json(override_path)
    config = merge_local_and_override_config(local_config, override_config)
    target_value = str(target_arg or config.get("target", "vendor"))
    targets = ["vendor", "gems"] if target_value == "both" else [target_value]
    out: Dict[str, Any] = {
        "upstream": str(upstream_path.resolve()),
        "config": str(config_path.resolve()),
        "override": str(Path(override_path).resolve()) if override_path and Path(override_path).exists() else None,
        "target": target_value,
        "targets": {},
    }
    for target in targets:
        out["targets"][target] = build_effective_target_config(
            upstream, config, target, config_path.resolve().parent)
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Host-side H100/Hygon vLLM profile runner; no third-party Python packages required")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="run one or both profile targets")
    run_p.add_argument("--upstream", required=True, type=Path)
    run_p.add_argument("--config", required=True, type=Path)
    run_p.add_argument("--override", default=None, type=Path, help="Optional override.json. Blank/null fields are ignored; non-empty fields override local_required.json/defaults")
    run_p.add_argument("--target", default=None, help="vendor, gems, or both. Defaults to config.target or vendor")
    run_p.add_argument("--host-output-dir", default=None, help="Override where the renamed <target>.pt.trace.json.gz is written on the host (default: profile.host_output_dir, i.e. ./traceSource_both)")
    run_p.add_argument("--host-log-dir", default=None, help="Override where per-target run logs (vllm.log, summary.json, scripts) are written on the host (default: logging.host_log_dir, i.e. ./profile_runner_logs)")
    run_p.add_argument("--dry-run", action="store_true", help="Print effective config and exit without touching Docker")

    cfg_p = sub.add_parser("print-config", help="print effective config after defaults + JSON overrides; does not touch Docker")
    cfg_p.add_argument("--upstream", required=True, type=Path)
    cfg_p.add_argument("--config", required=True, type=Path)
    cfg_p.add_argument("--override", default=None, type=Path, help="Optional override.json. Blank/null fields are ignored; non-empty fields override local_required.json/defaults")
    cfg_p.add_argument("--target", default=None, help="vendor, gems, or both. Defaults to config.target or vendor")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.cmd == "print-config":
        bundle = build_effective_config_bundle(args.upstream, args.config, args.target, args.override)
        print(json.dumps(bundle, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "run":
        if getattr(args, "dry_run", False):
            bundle = build_effective_config_bundle(args.upstream, args.config, args.target, args.override)
            log("DRY-RUN: effective config (Docker not touched)")
            print(json.dumps(bundle, ensure_ascii=False, indent=2))
            return 0

        local_config = load_json(args.config)
        override_config = load_optional_override_json(args.override)
        config = merge_local_and_override_config(local_config, override_config)
        target_arg = str(args.target or config.get("target", "vendor"))
        targets = ["vendor", "gems"] if target_arg == "both" else [target_arg]
        summaries: List[Dict[str, Any]] = []
        for target in targets:
            log(f"========== Running target: {target} ==========")
            summaries.append(run_target(args.upstream, args.config, target, args.override,
                                        host_output_dir_override=args.host_output_dir,
                                        host_log_dir_override=args.host_log_dir))
        if len(summaries) > 1:
            log("========== All targets finished ==========")
            for item in summaries:
                log(f"{item['target']}: {item['trace']}")
        return 0

    parser.error(f"Unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
