#!/usr/bin/env python3
"""Multi-node vLLM distributed deployment script.

SSH uses passwordless public-key authentication only (system default key ~/.ssh/id_rsa
or ssh-agent). Configure master->worker passwordless access via setup_passwordless_ssh.sh
before running.

Usage:
    python deploy_vllm.py --config deploy_config.yaml                  # Deploy and start
    python deploy_vllm.py --config deploy_config.yaml --stop           # Stop services
    python deploy_vllm.py --config deploy_config.yaml --status         # Check status
    python deploy_vllm.py --config deploy_config.yaml --restart-container  # Restart containers
    python deploy_vllm.py --config deploy_config.yaml --delete-container   # Delete containers
    python deploy_vllm.py --config deploy_config.yaml --load-image /path/to/image.tar  # Load docker image
    python deploy_vllm.py --config deploy_config.yaml --pull-image                    # Pull docker image from config
    python deploy_vllm.py --config deploy_config.yaml --pull-image registry/image:tag  # Pull specified image
    python deploy_vllm.py --config deploy_config.yaml --clear-cache    # Clear vllm/triton/torch caches
    python deploy_vllm.py --config deploy_config.yaml --fetch-logs     # Fetch remote logs to local
    python deploy_vllm.py --config deploy_config.yaml --fetch-logs --lines 200  # Fetch last N lines
"""

import argparse
import getpass
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path


# ---------------------------------------------------------------------------
# Dependency check & auto-install
# ---------------------------------------------------------------------------
REQUIRED_PACKAGES = {"paramiko": "paramiko", "yaml": "pyyaml"}


def ensure_dependencies():
    """Check required Python packages, auto-install if missing."""
    missing = []
    for module_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            __import__(module_name)
        except ImportError:
            missing.append(pip_name)

    if not missing:
        return

    print(f"[preflight] Missing packages: {', '.join(missing)}, installing...")

    # Try: python -m pip > pip3 > pip
    pip_commands = [
        [sys.executable, "-m", "pip", "install", "--no-input"] + missing,
        ["pip3", "install", "--no-input"] + missing,
        ["pip", "install", "--no-input"] + missing,
    ]

    for cmd in pip_commands:
        try:
            subprocess.check_call(cmd, timeout=120, stdin=subprocess.DEVNULL)
            print(f"[preflight] Installed: {', '.join(missing)}")
            return
        except subprocess.TimeoutExpired:
            print(f"[preflight] Command timed out: {' '.join(cmd)}")
            continue
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue

    print(f"[preflight] Failed to install packages. Please run manually:")
    print(f"  pip3 install {' '.join(missing)}")
    sys.exit(1)


ensure_dependencies()

import paramiko  # noqa: E402
import yaml  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def get_node_ssh_info(cfg, node):
    """Return (hostname, username, port, key_file) for a node.

    Passwordless SSH only: connects directly to the node's host IP.
    key_file defaults to system default key (~/.ssh/id_rsa) or ssh-agent when empty.
    """
    ssh = cfg.get("ssh", {})
    hostname = node["host"]                              # direct connect to node IP
    username = node.get("ssh_user", ssh.get("username", "root"))
    port = node.get("ssh_port", ssh.get("port", 22))
    key_file = node.get("ssh_key_file", ssh.get("key_file", ""))
    return hostname, username, port, key_file


def ssh_connect(hostname: str, username: str, port: int, key_filename: str = "") -> paramiko.SSHClient:
    """Connect to remote host using public key authentication (passwordless).

    Args:
        hostname: Target host IP or hostname
        username: SSH username
        port: SSH port
        key_filename: Explicit key file path; empty = use default ~/.ssh/id_rsa or ssh-agent

    Returns:
        Connected paramiko.SSHClient instance
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = dict(
        hostname=hostname,
        port=port,
        username=username,
        timeout=15,
        look_for_keys=True,
        allow_agent=True
    )
    if key_filename:
        connect_kwargs["key_filename"] = key_filename
    client.connect(**connect_kwargs)
    return client


# ---------------------------------------------------------------------------
# Preflight: connectivity & container check
# ---------------------------------------------------------------------------
def preflight_check(cfg: dict) -> bool:
    """Check SSH connectivity to all remote nodes and verify containers are running.
    Returns True if all checks pass, False otherwise.
    """
    nodes = cfg["nodes"]
    container = cfg["docker"]["container_name"]
    results = {}
    threads = []

    def check_node(rank, node):
        node_name = node.get("name", node["host"])
        tag = f"[node-{rank} {node_name}]"
        try:
            if node.get("local", False):
                # Local: just check container
                proc = subprocess.run(
                    ["docker", "inspect", "-f", "{{.State.Running}}", container],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                )
                running = proc.stdout.decode().strip()
                if running == "true":
                    log.info(f"{tag} Local - container '{container}' is running")
                    results[rank] = True
                else:
                    log.error(f"{tag} Local - container '{container}' is NOT running")
                    results[rank] = False
            else:
                # Remote: check SSH + container
                hostname, username, port, key_file = get_node_ssh_info(cfg, node)
                client = ssh_connect(hostname, username, port, key_file)
                cmd = f"docker inspect -f '{{{{.State.Running}}}}' {container}"
                stdin, stdout, stderr = client.exec_command(cmd, timeout=10)
                running = stdout.read().decode().strip()
                client.close()
                if running == "true":
                    log.info(f"{tag} SSH OK, container '{container}' is running")
                    results[rank] = True
                else:
                    log.error(f"{tag} SSH OK, but container '{container}' is NOT running")
                    results[rank] = False
        except Exception as e:
            log.error(f"{tag} Preflight FAILED: {e}")
            results[rank] = False

    for rank, node in enumerate(nodes):
        t = threading.Thread(target=check_node, args=(rank, node))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    passed = all(results.get(r, False) for r in range(len(nodes)))
    if passed:
        log.info(f"Preflight check PASSED: all {len(nodes)} nodes reachable, containers running")
    else:
        failed = [nodes[r].get("name", nodes[r]["host"]) for r in range(len(nodes)) if not results.get(r, False)]
        log.error(f"Preflight check FAILED for: {', '.join(failed)}")
    return passed


def build_vllm_command(cfg: dict, node_rank: int) -> str:
    """Build the docker exec command for a given node rank."""
    vllm = cfg["vllm"]
    container = cfg["docker"]["container_name"]
    master_addr = cfg["nodes"][0]["host"]
    nnodes = len(cfg["nodes"])

    # Build environment exports
    env_exports = []
    env_inline = {}
    extra_env = vllm.get("extra_env", {})
    for key, val in extra_env.items():
        if any(c in str(val) for c in [" ", ",", ";"]):
            env_inline[key] = val
        else:
            env_exports.append(f"export {key}={val}")

    exports_str = "\n".join(env_exports)

    # Build inline env prefix for values with special chars
    inline_parts = []
    for key, val in env_inline.items():
        inline_parts.append(f'{key}="{val}"')
    inline_env = " ".join(inline_parts)
    if inline_env:
        inline_env = "env " + inline_env + " "

    # Build vllm serve arguments
    args = [
        f"vllm serve {vllm['model_path']}",
        f"--served-model-name {vllm['served_model_name']}",
        f"--host {vllm['host']} --port {vllm['port']}",
        f"--tensor-parallel-size {vllm['tensor_parallel_size']}",
        f"--pipeline-parallel-size {vllm['pipeline_parallel_size']}",
        f"--max-model-len {vllm['max_model_len']}",
    ]

    if vllm.get("trust_remote_code"):
        args.append("--trust-remote-code")
    if vllm.get("enforce_eager"):
        args.append("--enforce-eager")

    limit_mm = vllm.get("limit_mm_per_prompt")
    if limit_mm:
        escaped = limit_mm.replace('"', '\\"')
        args.append(f'--limit-mm-per-prompt "{escaped}"')

    args.append(f"--nnodes {nnodes} --node-rank {node_rank}")
    args.append(f"--master-addr {master_addr}")

    if node_rank > 0:
        args.append("--headless")

    vllm_cmd = " \\\n  ".join(args)
    log_file = f"/root/vllm_node{node_rank}.log"

    # Execute in /root directory inside the container
    inner_script = f"""cd /root
# Load container environment (docker exec bash -c is non-interactive,
# so /etc/bash.bashrc skips itself; setting PS1 bypasses that guard)
PS1=x source /etc/bash.bashrc 2>/dev/null
for f in /etc/profile.d/*.sh; do [ -f "$f" ] && source "$f" 2>/dev/null; done
{exports_str}
nohup {inline_env}{vllm_cmd} \\
  > {log_file} 2>&1 &
echo "vLLM started on node {node_rank}, logging to {log_file}"
"""

    cmd = f"docker exec {container} bash -c '{inner_script}'"
    return cmd


def build_stop_command(cfg: dict, node_rank: int = 0) -> str:
    """Build command to kill vllm processes inside the container."""
    container = cfg["docker"]["container_name"]
    return f"docker exec {container} bash -c 'pkill -f \"vllm serve\" || true; echo \"vLLM processes stopped\"'"


def build_status_command(cfg: dict, node_rank: int) -> str:
    """Build command to check vllm process status and tail logs."""
    container = cfg["docker"]["container_name"]
    log_file = f"/root/vllm_node{node_rank}.log"
    return (
        f"docker exec {container} bash -c '"
        f"echo \"=== Process ===\"; "
        f"ps aux | grep \"vllm serve\" | grep -v grep || echo \"No vLLM process running\"; "
        f"echo \"=== Last 10 lines of log ===\"; "
        f"tail -n 10 {log_file} 2>/dev/null || echo \"No log file found\"'"
    )


def build_restart_container_command(cfg: dict, node_rank: int) -> str:
    """Build command to restart the docker container."""
    container = cfg["docker"]["container_name"]
    return f"docker restart {container} && echo \"Container {container} restarted successfully\""


def build_create_container_command(cfg: dict, node_rank: int) -> str:
    """Build command to create a new docker container."""
    docker = cfg["docker"]
    container = docker["container_name"]
    image = docker["image"]
    run_args = docker.get("run_args", "")
    return (
        f"docker inspect {container} >/dev/null 2>&1 && "
        f"echo \"Container {container} already exists, skipping\" || "
        f"(docker run --name {container} {run_args} -itd {image} /bin/bash && "
        f"echo \"Container {container} created successfully\")"
    )


def build_delete_container_command(cfg, node_rank):
    """Build command to force-remove a docker container."""
    container = cfg["docker"]["container_name"]
    return (
        f"docker rm -f {container} 2>/dev/null && "
        f"echo \"Container {container} deleted successfully\" || "
        f"echo \"Container {container} not found, skipping\""
    )


def build_load_image_command(cfg, node_rank, image_path=""):
    """Build command to load a docker image from a tar file."""
    return f"docker load -i {image_path} && echo \"Image loaded successfully from {image_path}\""


def build_pull_image_command(cfg, node_rank, image_name=""):
    """Build command to pull a docker image from registry."""
    image = image_name or cfg["docker"]["image"]
    return f"docker pull {image} && echo \"Image pulled successfully: {image}\""


def build_clear_cache_command(cfg: dict, node_rank: int) -> str:
    """Build command to clear vllm, triton, and torch caches inside the container."""
    container = cfg["docker"]["container_name"]
    cache_paths = [
        "~/.cache/vllm",
        "~/.cache/triton",
        "~/.cache/torch",
        "~/.cache/huggingface/hub",
        "~/.triton",
        "/tmp/vllm*",
        "/tmp/triton*",
        "/tmp/torch*",
    ]
    rm_cmds = " ".join(f"rm -rf {p}" for p in cache_paths)
    return (
        f"docker exec {container} bash -c '"
        f"{rm_cmds}; "
        f"echo \"Caches cleared: vllm, triton, torch, huggingface\"'"
    )


def build_fetch_log_command(cfg: dict, node_rank: int, lines: int = 0) -> str:
    """Build command to read log content from container."""
    container = cfg["docker"]["container_name"]
    log_file = f"/root/vllm_node{node_rank}.log"
    if lines > 0:
        return f"docker exec {container} tail -n {lines} {log_file} 2>/dev/null || echo 'No log file found'"
    else:
        return f"docker exec {container} cat {log_file} 2>/dev/null || echo 'No log file found'"


def run_local_streaming(command, node_rank, node_name, results, timeout=30):
    """Execute a command locally with real-time streaming output."""
    tag = f"[node-{node_rank} {node_name}]"
    try:
        log.info(f"{tag} Executing locally...")
        proc = subprocess.Popen(
            ["bash", "-c", command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        out_lines = []
        err_lines = []

        def read_stream(stream, lines, prefix=""):
            for raw in stream:
                line = raw.decode("utf-8", errors="replace").rstrip("\n\r")
                if line:
                    log.info(f"{tag}{prefix} {line}")
                    lines.append(line)

        t_out = threading.Thread(target=read_stream, args=(proc.stdout, out_lines))
        t_err = threading.Thread(target=read_stream, args=(proc.stderr, err_lines, " STDERR:"))
        t_out.start()
        t_err.start()

        proc.wait(timeout=timeout)
        t_out.join()
        t_err.join()

        out = "\n".join(out_lines)
        err = "\n".join(err_lines)
        results[node_rank] = {"node_name": node_name, "exit_code": proc.returncode, "stdout": out, "stderr": err}

    except subprocess.TimeoutExpired:
        proc.kill()
        log.error(f"{tag} Command timed out after {timeout}s")
        results[node_rank] = {"node_name": node_name, "exit_code": -1, "stdout": "", "stderr": "timeout"}
    except Exception as e:
        log.error(f"{tag} Failed: {e}")
        results[node_rank] = {"node_name": node_name, "exit_code": -1, "stdout": "", "stderr": str(e)}


def run_local(command, node_rank, node_name,
              results, log_dir=None, timeout=30):
    """Execute a command locally (for the node where this script is running)."""
    tag = f"[node-{node_rank} {node_name}]"
    try:
        log.info(f"{tag} Executing locally...")
        proc = subprocess.run(
            ["bash", "-c", command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        exit_code = proc.returncode

        if out.strip():
            for line in out.strip().split("\n"):
                log.info(f"{tag} {line}")
        if err.strip():
            for line in err.strip().split("\n"):
                log.warning(f"{tag} STDERR: {line}")

        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"node{node_rank}_{node_name}.log"
            with open(log_file, "a") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                f.write(f"COMMAND: {command}\n")
                f.write(f"EXIT CODE: {exit_code}\n")
                f.write(f"STDOUT:\n{out}\n")
                if err:
                    f.write(f"STDERR:\n{err}\n")

        results[node_rank] = {"node_name": node_name, "exit_code": exit_code, "stdout": out, "stderr": err}

    except Exception as e:
        log.error(f"{tag} Failed: {e}")
        results[node_rank] = {"node_name": node_name, "exit_code": -1, "stdout": "", "stderr": str(e)}


def run_on_node_streaming(hostname, username, port,
                          command, node_rank, node_name,
                          results, timeout=30, key_filename=""):
    """Execute a command on a remote node via SSH with real-time streaming output."""
    tag = f"[node-{node_rank} {node_name}]"
    try:
        log.info(f"{tag} Connecting to {hostname}:{port} as {username}...")
        client = ssh_connect(hostname, username, port, key_filename)
        log.info(f"{tag} Executing command...")

        channel = client.get_transport().open_session()
        channel.settimeout(timeout)
        channel.exec_command(command)

        out_buf = []
        err_buf = []
        out_line = ""
        err_line = ""

        while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="replace")
                out_buf.append(chunk)
                out_line += chunk
                while "\n" in out_line:
                    line, out_line = out_line.split("\n", 1)
                    line = line.rstrip("\r")
                    if line:
                        log.info(f"{tag} {line}")
            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                err_buf.append(chunk)
                err_line += chunk
                while "\n" in err_line:
                    line, err_line = err_line.split("\n", 1)
                    line = line.rstrip("\r")
                    if line:
                        log.info(f"{tag} {line}")
            time.sleep(0.1)

        # Read remaining data
        while channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            out_buf.append(chunk)
        while channel.recv_stderr_ready():
            chunk = channel.recv_stderr(4096).decode("utf-8", errors="replace")
            err_buf.append(chunk)

        # Print leftover partial lines
        if out_line.strip():
            log.info(f"{tag} {out_line.strip()}")
        if err_line.strip():
            log.info(f"{tag} {err_line.strip()}")

        exit_code = channel.recv_exit_status()
        out = "".join(out_buf)
        err = "".join(err_buf)
        results[node_rank] = {"node_name": node_name, "exit_code": exit_code, "stdout": out, "stderr": err}
        client.close()

    except Exception as e:
        log.error(f"{tag} Failed: {e}")
        results[node_rank] = {"node_name": node_name, "exit_code": -1, "stdout": "", "stderr": str(e)}


def run_on_node(hostname, username, port,
                command, node_rank, node_name,
                results, log_dir=None, timeout=30, key_filename=""):
    """Execute a command on a remote node via SSH."""
    tag = f"[node-{node_rank} {node_name}]"
    try:
        log.info(f"{tag} Connecting to {hostname}:{port} as {username}...")
        client = ssh_connect(hostname, username, port, key_filename)
        log.info(f"{tag} Executing command...")

        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)

        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()

        if out.strip():
            for line in out.strip().split("\n"):
                log.info(f"{tag} {line}")
        if err.strip():
            for line in err.strip().split("\n"):
                log.warning(f"{tag} STDERR: {line}")

        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"node{node_rank}_{node_name}.log"
            with open(log_file, "a") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                f.write(f"COMMAND: {command}\n")
                f.write(f"EXIT CODE: {exit_code}\n")
                f.write(f"STDOUT:\n{out}\n")
                if err:
                    f.write(f"STDERR:\n{err}\n")

        results[node_rank] = {"node_name": node_name, "exit_code": exit_code, "stdout": out, "stderr": err}
        client.close()

    except Exception as e:
        log.error(f"{tag} Failed: {e}")
        results[node_rank] = {"node_name": node_name, "exit_code": -1, "stdout": "", "stderr": str(e)}


def deploy_parallel_streaming(cfg, command_fn, timeout=30):
    """Run commands on all nodes in parallel with real-time streaming output."""
    results = {}
    threads = []

    for rank, node in enumerate(cfg["nodes"]):
        cmd = command_fn(cfg, rank)
        node_name = node.get("name", node["host"])

        if node.get("local", False):
            t = threading.Thread(
                target=run_local_streaming,
                args=(cmd, rank, node_name, results, timeout),
            )
        else:
            hostname, username, port, key_file = get_node_ssh_info(cfg, node)
            t = threading.Thread(
                target=run_on_node_streaming,
                args=(hostname, username, port, cmd, rank, node_name, results, timeout, key_file),
            )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    return results


def deploy_parallel(cfg, command_fn, log_dir=None, timeout=30):
    """Run commands on all nodes in parallel. command_fn(cfg, rank) -> str.
    Nodes with 'local: true' execute locally via subprocess, others via SSH.
    """
    results = {}
    threads = []

    for rank, node in enumerate(cfg["nodes"]):
        cmd = command_fn(cfg, rank)
        node_name = node.get("name", node["host"])

        if node.get("local", False):
            t = threading.Thread(
                target=run_local,
                args=(cmd, rank, node_name, results, log_dir, timeout),
            )
        else:
            hostname, username, port, key_file = get_node_ssh_info(cfg, node)
            t = threading.Thread(
                target=run_on_node,
                args=(hostname, username, port, cmd, rank, node_name, results, log_dir, timeout, key_file),
            )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    return results


def health_check(master_host: str, port: int, timeout: int = 300, interval: int = 10) -> bool:
    """Poll master node /health endpoint until ready or timeout."""
    url = f"http://{master_host}:{port}/health"
    log.info(f"Health check: polling {url} (timeout={timeout}s)")

    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    log.info("Health check PASSED - service is ready!")
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass
        elapsed = int(time.time() - start)
        log.info(f"Health check: not ready yet ({elapsed}s elapsed), retrying in {interval}s...")
        time.sleep(interval)

    log.error(f"Health check FAILED after {timeout}s")
    return False


def print_summary(action, results):
    """Print a formatted summary table of operation results."""
    print("")
    print(f"{'=' * 60}")
    print(f"  {action} - Summary")
    print(f"{'=' * 60}")
    print(f"  {'Node':<20} {'Status':<10} {'Message'}")
    print(f"  {'-' * 20} {'-' * 10} {'-' * 27}")
    for rank in sorted(results.keys()):
        r = results[rank]
        name = r["node_name"]
        if r["exit_code"] == 0:
            status = "OK"
            msg = r["stdout"].strip().split("\n")[-1] if r["stdout"].strip() else ""
        else:
            status = "FAILED"
            msg = r["stderr"].strip().split("\n")[-1] if r["stderr"].strip() else f"exit_code={r['exit_code']}"
        print(f"  {name:<20} {status:<10} {msg}")
    ok_count = sum(1 for r in results.values() if r["exit_code"] == 0)
    fail_count = len(results) - ok_count
    print(f"  {'-' * 20} {'-' * 10} {'-' * 27}")
    print(f"  Total: {len(results)}  |  OK: {ok_count}  |  Failed: {fail_count}")
    print(f"{'=' * 60}")
    print("")


def sync_files_to_workers(cfg: dict):
    """Sync files from master container to all worker containers (optional).

    Reads cfg['sync_files'] (optional list of container paths) and syncs them from master to workers.
    Use case: propagate /root/flaggems_ops_control.json + /etc/environment for multi-node operator tuning.
    Uses passwordless SSH (system default key or ssh-agent).
    """
    sync_files = cfg.get("sync_files")
    if not sync_files:
        log.debug("No sync_files configured, skipping file sync.")
        return True

    if len(cfg["nodes"]) < 2:
        log.debug("Single node, skipping file sync.")
        return True

    master_container = cfg["docker"]["container_name"]
    log.info(f"Syncing {len(sync_files)} file(s) from master to {len(cfg['nodes'])-1} worker(s)...")

    # Step 1: docker cp from master container to local temp
    tmp_dir = Path("/tmp/deploy_vllm_sync")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for fpath in sync_files:
        fname = Path(fpath).name
        local_path = tmp_dir / fname
        cmd = f"docker cp {master_container}:{fpath} {local_path} 2>/dev/null || echo 'File not found: {fpath}'"
        log.debug(f"Copying {fpath} from master to {local_path}")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        if "File not found" in result.stdout or result.returncode != 0:
            log.warning(f"Skipping sync of {fpath} (not found in master container)")
            continue
        if not local_path.exists():
            log.warning(f"Skipping sync of {fpath} (docker cp failed)")
            continue

        # Step 2: docker cp from local temp to each worker container
        for rank, node in enumerate(cfg["nodes"]):
            if rank == 0:  # Skip master
                continue

            node_name = node.get("name", node["host"])
            if node.get("local", False):
                # Local worker: direct docker cp
                cmd_worker = f"docker cp {local_path} {master_container}:{fpath}"
                log.debug(f"[{node_name}] Copying {fpath} (local)")
                subprocess.run(cmd_worker, shell=True, capture_output=True, timeout=10)
            else:
                # Remote worker: scp to worker host, then docker cp into container
                hostname, username, port, key_file = get_node_ssh_info(cfg, node)

                # Auth: use explicit key_file if set, otherwise system default key/ssh-agent
                if key_file:
                    scp_auth, ssh_auth = f"scp -i {key_file}", f"ssh -i {key_file}"
                else:
                    scp_auth, ssh_auth = "scp", "ssh"

                # Upload to worker host /tmp (scp uses -P for port)
                remote_tmp = f"/tmp/{fname}"
                scp_cmd = f"{scp_auth} -P {port} -o StrictHostKeyChecking=no {local_path} {username}@{hostname}:{remote_tmp}"

                log.debug(f"[{node_name}] Uploading {fpath} via scp")
                subprocess.run(scp_cmd, shell=True, capture_output=True, timeout=30)

                # Docker cp from worker host /tmp into container (ssh uses -p for port)
                docker_cp_cmd = f"docker cp {remote_tmp} {master_container}:{fpath}"
                ssh_cmd = f"{ssh_auth} -p {port} -o StrictHostKeyChecking=no {username}@{hostname} '{docker_cp_cmd}'"

                log.debug(f"[{node_name}] Copying {fpath} into container")
                subprocess.run(ssh_cmd, shell=True, capture_output=True, timeout=10)

    log.info(f"File sync complete: {len(sync_files)} file(s) synced to {len(cfg['nodes'])-1} worker(s).")
    return True


def do_deploy(cfg: dict):
    """Deploy vLLM to all nodes."""
    log.info(f"Deploying vLLM across {len(cfg['nodes'])} nodes...")

    # Sync files from master to workers (optional, for operator tuning state propagation)
    sync_files_to_workers(cfg)

    log_dir = Path("logs")
    results = deploy_parallel(cfg, build_vllm_command, log_dir)

    print_summary("Deploy vLLM", results)

    failed = [r for r in results.values() if r["exit_code"] != 0]
    if failed:
        log.error(f"{len(failed)} node(s) failed to start.")
        return False

    log.info("All nodes started successfully. Beginning health check...")
    master_host = cfg["nodes"][0]["host"]
    vllm_port = cfg["vllm"]["port"]
    return health_check(master_host, vllm_port)


def do_stop(cfg: dict):
    """Stop vLLM on all nodes."""
    log.info(f"Stopping vLLM across {len(cfg['nodes'])} nodes...")
    results = deploy_parallel(cfg, build_stop_command)
    print_summary("Stop vLLM", results)


def do_status(cfg: dict):
    """Check vLLM status on all nodes."""
    log.info(f"Checking vLLM status across {len(cfg['nodes'])} nodes...")
    results = deploy_parallel(cfg, build_status_command)
    print_summary("Status Check", results)

    master_host = cfg["nodes"][0]["host"]
    vllm_port = cfg["vllm"]["port"]
    url = f"http://{master_host}:{vllm_port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            log.info(f"Health endpoint: HTTP {resp.status} - service is UP")
    except Exception as e:
        log.warning(f"Health endpoint: {e} - service may not be ready")


def do_create_container(cfg: dict):
    """Create docker containers on all nodes."""
    docker = cfg["docker"]
    if not docker.get("image"):
        log.error("docker.image not configured in config file, cannot create container.")
        sys.exit(1)
    log.info(f"Creating container '{docker['container_name']}' across {len(cfg['nodes'])} nodes...")
    results = deploy_parallel(cfg, build_create_container_command, timeout=120)
    print_summary("Create Container", results)


def do_restart_container(cfg: dict):
    """Restart docker containers on all nodes."""
    log.info(f"Restarting containers across {len(cfg['nodes'])} nodes...")
    results = deploy_parallel(cfg, build_restart_container_command, timeout=120)
    print_summary("Restart Container", results)


def do_delete_container(cfg):
    """Delete docker containers on all nodes."""
    container = cfg["docker"]["container_name"]
    log.info(f"Deleting container '{container}' across {len(cfg['nodes'])} nodes...")
    results = deploy_parallel(cfg, build_delete_container_command, timeout=120)
    print_summary("Delete Container", results)


def do_load_image(cfg, image_path):
    """Load docker image from tar file on all nodes."""
    log.info(f"Loading docker image from '{image_path}' across {len(cfg['nodes'])} nodes...")
    command_fn = lambda cfg, rank: build_load_image_command(cfg, rank, image_path)
    results = deploy_parallel(cfg, command_fn, timeout=600)
    print_summary("Load Image", results)


def do_pull_image(cfg, image_name=""):
    """Pull docker image on all nodes."""
    image = image_name or cfg["docker"]["image"]
    log.info(f"Pulling docker image '{image}' across {len(cfg['nodes'])} nodes...")
    command_fn = lambda cfg, rank: build_pull_image_command(cfg, rank, image_name)
    results = deploy_parallel_streaming(cfg, command_fn, timeout=1800)
    print_summary("Pull Image", results)


def do_clear_cache(cfg: dict):
    """Clear vllm/triton/torch caches on all nodes."""
    log.info(f"Clearing caches across {len(cfg['nodes'])} nodes...")
    results = deploy_parallel(cfg, build_clear_cache_command)
    print_summary("Clear Cache", results)


def do_fetch_logs(cfg: dict, lines: int = 0):
    """Fetch remote vLLM logs from all nodes to local directory."""
    log.info(f"Fetching logs from {len(cfg['nodes'])} nodes...")

    local_log_dir = Path("logs/remote")
    local_log_dir.mkdir(parents=True, exist_ok=True)

    command_fn = lambda cfg, rank: build_fetch_log_command(cfg, rank, lines)
    results = deploy_parallel(cfg, command_fn, timeout=60)

    for rank, result in sorted(results.items()):
        node_name = result["node_name"]
        local_file = local_log_dir / f"vllm_node{rank}_{node_name}.log"
        content = result["stdout"]
        if content.strip() and content.strip() != "No log file found":
            with open(local_file, "w") as f:
                f.write(content)
            log.info(f"[node-{rank} {node_name}] Log saved to {local_file} ({len(content)} bytes)")
        else:
            log.warning(f"[node-{rank} {node_name}] No log content available")

    log.info(f"Logs saved to {local_log_dir}/")


def main():
    parser = argparse.ArgumentParser(description="Multi-node vLLM deployment tool")
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument("--stop", action="store_true", help="Stop vLLM on all nodes")
    parser.add_argument("--status", action="store_true", help="Check vLLM status on all nodes")
    parser.add_argument("--create-container", action="store_true", help="Create docker containers on all nodes")
    parser.add_argument("--delete-container", action="store_true", help="Delete docker containers on all nodes")
    parser.add_argument("--restart-container", action="store_true", help="Restart docker containers on all nodes")
    parser.add_argument("--load-image", type=str, default="", help="Load docker image from tar file path on each node")
    parser.add_argument("--pull-image", type=str, nargs="?", const="", default=None,
                        help="Pull docker image on all nodes (default: image from config)")
    parser.add_argument("--container-name", type=str, default="", help="Override container name from config (for --restart/--delete-container)")
    parser.add_argument("--clear-cache", action="store_true", help="Clear vllm/triton/torch caches on all nodes")
    parser.add_argument("--fetch-logs", action="store_true", help="Fetch remote vLLM logs to local")
    parser.add_argument("--lines", type=int, default=0, help="Number of log lines to fetch (0=all)")
    args = parser.parse_args()

    cfg = load_config(args.config)

    # Override container name if specified
    if args.container_name:
        log.info(f"Overriding container name: '{cfg['docker']['container_name']}' -> '{args.container_name}'")
        cfg["docker"]["container_name"] = args.container_name

    # Preflight: skip for operations that don't require a running container
    if not args.create_container and not args.delete_container and not args.load_image and args.pull_image is None:
        if not preflight_check(cfg):
            log.error("Preflight check failed, aborting.")
            sys.exit(1)

    if args.stop:
        do_stop(cfg)
    elif args.status:
        do_status(cfg)
    elif args.create_container:
        do_create_container(cfg)
    elif args.delete_container:
        do_delete_container(cfg)
    elif args.restart_container:
        do_restart_container(cfg)
    elif args.load_image:
        do_load_image(cfg, args.load_image)
    elif args.pull_image is not None:
        do_pull_image(cfg, args.pull_image)
    elif args.clear_cache:
        do_clear_cache(cfg)
    elif args.fetch_logs:
        do_fetch_logs(cfg, args.lines)
    else:
        success = do_deploy(cfg)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

