#!/usr/bin/env python3

# Copyright 2026 FlagOS Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""LongTaskRunner - 长任务执行（detached + state 文件 + 存活判定 + 断点接管）

**为什么需要它**（原实现的问题，见长任务执行协议）：
前台 `subprocess.run(docker exec …)` 阻塞等待，只有一个最终退出码，会导致
1. 中间进度不可见——6 小时的评测中途什么都看不到；
2. 超时只杀 `docker exec` 客户端，容器内进程可能继续跑（引擎判失败、GPU 还在烧，
   后续候选重启服务会和它撞）；
3. 引擎进程死亡 → 评测变孤儿，且没有锁，重跑会起第二个评测；
4. 任务静默死亡无法识别（state 还写着 running）。

本模块复用容器内既有的 `task_runner.py`（长任务执行协议的事实标准）：
- **detached 启动**：`docker exec -d … task_runner.py --cmd/--state/--log/--timeout`
  → 任务在容器内独立存活，引擎进程死了也不影响它
- **state 文件是事实来源**：task_runner 启动即写 `{status: running, pid, started_at}`，
  结束写 `done|error|timeout`
- **存活判定**：state 仍为 running 时用 `pgrep -f <任务命令文件路径>` 判断进程是否还在
  （排除轮询者自身，否则会自匹配恒真）——**进程消失但 state 还是 running = 静默死亡**，
  这是前台阻塞方案根本发现不了的
- **断点接管**：启动前先读 state，若已有 running 且进程活着则接续等待，绝不重复启动
- **超时清理**：引擎侧放弃时 kill 容器内进程，避免留下无主的评测继续占 GPU

一处 docker exec 完成一次轮询（state + 日志尾 + 进程列表），轮询开销最小。
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from .command_executor import CommandExecutor, parse_json_output

# 轮询命令的分段标记（解析与测试都依赖它们）
MARK_LOG = "===FLAGOS-TASK-LOG==="
MARK_PID = "===FLAGOS-TASK-PID==="

DEFAULT_TASKS_DIR = "/flagos-workspace/logs/tasks"
DEFAULT_POLL_INTERVAL = 60.0  # 引擎不是会被随时杀掉的会话，比 8 分钟更密以便尽早发现死亡
CONDA_PATH = "/opt/conda/bin:$PATH"

# detached 启动后的宽限期（轮询次数）：此时 state 可能还没落盘、进程也未必已出现在
# pgrep 里，不能据此判"静默死亡"。超出宽限仍无进程无 state 才判定起不来。
LAUNCH_GRACE_POLLS = 3


# 状态 → 人话（写进错误信息，让"为什么失败"一眼可辨）
STATUS_TEXT = {
    "done": "任务成功",
    "error": "任务执行出错",
    "timeout": "超过 task_runner 总闸超时",
    "died": "**静默死亡**：state 仍为 running 但进程已消失",
    "engine_gave_up": "引擎等待超限，已清理容器内进程",
    "launch_failed": "任务未能启动",
}


@dataclass
class TaskResult:
    """长任务执行结果"""
    status: str                       # done | error | timeout | died | engine_gave_up | launch_failed
    exit_code: Optional[int] = None
    state: Dict = field(default_factory=dict)
    log_tail: str = ""
    adopted: bool = False             # 是否接管了既有任务（而非新启动）
    polls: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "done"

    def summary(self) -> str:
        bits = [f"status={self.status}", STATUS_TEXT.get(self.status, "")]
        bits = [b for b in bits if b]
        if self.exit_code is not None:
            bits.append(f"exit={self.exit_code}")
        if self.adopted:
            bits.append("接管既有任务")
        if self.state.get("error"):
            bits.append(f"error={str(self.state['error'])[:120]}")
        bits.append(f"轮询{self.polls}次")
        if self.log_tail:
            bits.append(f"日志尾：{self.log_tail[-300:]}")
        return " ".join(bits)


class LongTaskRunner:
    """容器内长任务的启动 / 轮询 / 存活判定 / 接管"""

    def __init__(
        self,
        executor: CommandExecutor,
        container: str,
        tasks_dir: str = DEFAULT_TASKS_DIR,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        path_prefix: str = CONDA_PATH,
    ):
        self.executor = executor
        self.container = container
        self.tasks_dir = tasks_dir
        # 注意：0 是合法值（测试用，不睡），所以不能写成 `poll_interval or DEFAULT`
        self.poll_interval = (
            DEFAULT_POLL_INTERVAL if poll_interval is None else poll_interval
        )
        self.path_prefix = path_prefix
        self.logger = logging.getLogger("workflow.long_task")

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    def run(
        self,
        task_id: str,
        cmd: str,
        timeout: int,
        max_polls: Optional[int] = None,
        poll_interval: Optional[float] = None,
    ) -> TaskResult:
        """执行长任务并等待结束

        Args:
            task_id: 任务标识（决定 .cmd/.state/.log 路径）
            cmd: 要执行的命令（会被写入 .cmd 文件后由 task_runner 拉起）
            timeout: 任务总超时秒数（task_runner --timeout，也是引擎侧的等待上限）
            max_polls: 轮询次数上限（测试用；None = 由 timeout 决定）
            poll_interval: 轮询间隔（秒）；None 用实例默认

        Returns:
            TaskResult（status 含 done/error/timeout/died/engine_gave_up/launch_failed）

        Note:
            存活判定特征**由本方法自己推导**（= 任务命令文件路径），不接受调用方传入——
            早前让调用方传 `<task_id>.state` 是个自匹配陷阱：轮询命令里就有
            `cat <state>`，于是 `pgrep -f '<state>'` 命中的是轮询者自己，
            `alive` 恒真、静默死亡永远检不出来，`kill` 也杀不到真正的执行进程。
            用 `.cmd` 路径则：task_runner 的 argv（`--cmd 'bash <cmd>'`）与执行体
            （`bash <cmd>`）都含它，而轮询命令只引用 `.state`/`.log`，不含 `.cmd`。
        """
        state_path = f"{self.tasks_dir}/{task_id}.state"
        log_path = f"{self.tasks_dir}/{task_id}.log"
        cmd_path = f"{self.tasks_dir}/{task_id}.cmd"
        interval = self.poll_interval if poll_interval is None else poll_interval

        adopted = False
        existing = self._poll_once(state_path, log_path, cmd_path)
        if existing["status"] == "running" and existing["alive"]:
            # 断点接管：已有同任务在跑且进程活着 → 接续等待，绝不重复启动
            self.logger.warning(f"任务 {task_id} 已在运行（pid={existing['state'].get('pid')}），接管等待")
            adopted = True
        elif existing["status"] == "running" and not existing["alive"]:
            self.logger.warning(f"任务 {task_id} 状态为 running 但进程已消失（静默死亡），将重新启动")
        elif existing["status"] in ("done", "error", "timeout"):
            self.logger.info(f"任务 {task_id} 已有终态 {existing['status']}，直接采用（不重跑）")
            return TaskResult(status=existing["status"],
                              exit_code=existing["state"].get("exit_code"),
                              state=existing["state"], log_tail=existing["log_tail"])

        if not adopted:
            if not self._write_cmd_file(cmd_path, cmd):
                return TaskResult(status="launch_failed", state={}, log_tail="写任务命令文件失败")
            if not self._launch(task_id, cmd_path, state_path, log_path, timeout):
                return TaskResult(status="launch_failed", state={}, log_tail="detached 启动失败")

        deadline = time.time() + timeout + 60  # 给 task_runner 的总闸一点收尾余量
        polls = 0
        seen_alive = adopted  # 接管场景下进程必然已活着
        last = {"status": "unknown", "alive": True, "state": {}, "log_tail": ""}
        while True:
            if max_polls is not None and polls >= max_polls:
                self.logger.error(f"任务 {task_id} 达到轮询上限 {max_polls}，放弃")
                self._kill(cmd_path)
                return TaskResult(status="engine_gave_up", state=last["state"],
                                  log_tail=last["log_tail"], adopted=adopted, polls=polls)

            last = self._poll_once(state_path, log_path, cmd_path)
            polls += 1
            seen_alive = seen_alive or last["alive"]

            if last["status"] in ("done", "error", "timeout"):
                return TaskResult(status=last["status"],
                                  exit_code=last["state"].get("exit_code"),
                                  state=last["state"], log_tail=last["log_tail"],
                                  adopted=adopted, polls=polls)

            # state 还是非终态：看进程是否还在——这是"静默死亡"的唯一识别手段。
            # 宽限期内不判死（detached 启动后 state/进程都可能还没就绪）。
            if not last["alive"] and (seen_alive or polls > LAUNCH_GRACE_POLLS):
                self.logger.error(
                    f"任务 {task_id} 静默死亡：state={last['status']} 但进程已消失"
                    f"（日志尾：{last['log_tail'][-200:]}）"
                )
                return TaskResult(status="died", state=last["state"],
                                  log_tail=last["log_tail"], adopted=adopted, polls=polls)

            if time.time() > deadline:
                self.logger.error(f"任务 {task_id} 超过引擎等待上限 {timeout + 60}s，放弃并清理")
                self._kill(cmd_path)
                return TaskResult(status="engine_gave_up", state=last["state"],
                                  log_tail=last["log_tail"], adopted=adopted, polls=polls)

            if interval > 0:
                time.sleep(interval)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _write_cmd_file(self, cmd_path: str, cmd: str) -> bool:
        script = (
            f"mkdir -p {self.tasks_dir} && cat > {cmd_path} << 'FLAGOS_CMD_EOF'\n"
            f"{cmd}\n"
            f"FLAGOS_CMD_EOF"
        )
        res = self.executor.docker_exec(self.container, script, timeout=60)
        if not res.ok:
            self.logger.error(f"写任务命令文件失败：{res.stderr[:200]}")
            return False
        return True

    def _launch(self, task_id: str, cmd_path: str, state_path: str,
                log_path: str, timeout: int) -> bool:
        inner = (
            f"cd {self.tasks_dir} && {self.path_prefix} python3 "
            f"/flagos-workspace/scripts/task_runner.py "
            f"--cmd 'bash {cmd_path}' --state {state_path} --log {log_path} "
            f"--timeout {timeout}"
        )
        res = self.executor.docker_exec(self.container, inner, detach=True)
        if not res.ok:
            self.logger.error(f"detached 启动失败：{res.stderr[:200]}")
            return False
        self.logger.info(f"长任务已 detached 启动：{task_id}（timeout={timeout}s）")
        return True

    def _poll_once(self, state_path: str, log_path: str, cmd_path: str) -> Dict:
        """一次轮询：读 state + 日志尾 + 进程列表（单次 docker exec）"""
        script = (
            f"cat {state_path} 2>/dev/null || true; "
            f"echo '{MARK_LOG}'; "
            f"tail -n 3 {log_path} 2>/dev/null || true; "
            f"echo '{MARK_PID}'; "
            f"{self._ps_pids_cmd(cmd_path)} | head -3 || true"
        )
        res = self.executor.docker_exec(self.container, script, timeout=60)
        out = res.stdout or ""

        state_text, _, rest = out.partition(MARK_LOG)
        log_tail, _, pids_text = rest.partition(MARK_PID)

        parsed = parse_json_output(state_text)
        state = parsed if isinstance(parsed, dict) else {}
        pids = [p.strip() for p in pids_text.split() if p.strip().isdigit()]

        return {
            # state 还没落盘时统一记 unknown（由存活判定决定是"还在启动"还是"真死了"）
            "status": state.get("status") or "unknown",
            "alive": bool(pids),
            "state": state,
            "log_tail": log_tail.strip(),
        }

    @staticmethod
    def _ps_pids_cmd(cmd_path: str) -> str:
        """列出属于本任务的进程 pid（排除轮询 shell 自身与其父进程）

        用 `cmd_path` 作特征 + 排除 `$$`/`$PPID`：轮询命令本身也含该字符串，
        不排除的话 pgrep 会把自己算成"任务还活着"。
        """
        return (f"pgrep -f '{cmd_path}' 2>/dev/null "
                f'| grep -vx "$$" | grep -vx "$PPID"')

    def _kill(self, cmd_path: str):
        """清理容器内仍在跑的进程（超时/放弃时用，避免无主任务继续占 GPU）

        逐个 TERM 真正属于本任务的进程；早前用 `pkill -f '<task_id>.state'`
        既匹配不到执行体（它跑的是 `bash <cmd>`），又会误伤轮询者。
        """
        script = (
            f'for p in $({self._ps_pids_cmd(cmd_path)}); do '
            f'kill -TERM "$p" 2>/dev/null || true; done; true'
        )
        res = self.executor.docker_exec(self.container, script, timeout=60)
        self.logger.info(f"已尝试清理任务进程（cmd_path={cmd_path}）rc={res.returncode}")
