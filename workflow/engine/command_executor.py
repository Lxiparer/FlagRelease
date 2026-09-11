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

"""CommandExecutor - 命令执行注入 seam

背景（见 memory engine-takeover-direction，M1a）：
domain 执行器过去各写各的——有的直接 subprocess.run(shell=True, "docker exec…")、
有的拼了命令却返回 mock、有的直接 success=True 占位。本模块把「怎么执行命令」抽象成
统一接口，由 Engine 注入：
- SubprocessExecutor：真实后端（默认）。
- FakeExecutor：测试替身，脚本化返回 + 记录调用，让 domain 逻辑脱离容器可单测。

domain 从此调 self.executor.run(...) / .docker_exec(...)，不再内联 subprocess 或 mock。
真实上卡验证在 M4/Step 15（本 seam 只保证「发对命令、解析对输出」可测）。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import subprocess


@dataclass
class ExecResult:
    """命令执行结果"""
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def build_docker_exec_argv(
    container: str,
    script: str,
    detach: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> List[str]:
    """构造容器内执行的 argv（统一 conda PATH 前缀 + 可选 env/detach）。

    生成 ["docker","exec"(,"-d")(,"-e","K=V"...), container, "bash","-lc",
          "PATH=/opt/conda/bin:$PATH && <script>"]
    """
    argv: List[str] = ["docker", "exec"]
    if detach:
        argv.append("-d")
    if env:
        for k, v in env.items():
            argv += ["-e", f"{k}={v}"]
    inner = f"PATH=/opt/conda/bin:$PATH && {script}"
    argv += [container, "bash", "-lc", inner]
    return argv


class CommandExecutor(ABC):
    """命令执行后端抽象接口"""

    @abstractmethod
    def run(self, argv: List[str], timeout: Optional[int] = None) -> ExecResult:
        """执行 argv（列表形式，非 shell 字符串），返回 ExecResult"""
        raise NotImplementedError

    def docker_exec(
        self,
        container: str,
        script: str,
        detach: bool = False,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> ExecResult:
        """在容器内执行 bash 脚本（便捷方法，统一 PATH 前缀）"""
        argv = build_docker_exec_argv(container, script, detach=detach, env=env)
        return self.run(argv, timeout=timeout)


class SubprocessExecutor(CommandExecutor):
    """真实后端：subprocess.run（argv 列表，不用 shell）"""

    def run(self, argv: List[str], timeout: Optional[int] = None) -> ExecResult:
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return ExecResult(
                returncode=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
            )
        except subprocess.TimeoutExpired as e:
            return ExecResult(
                returncode=124,  # 约定：超时
                stdout=(e.stdout or "") if isinstance(e.stdout, str) else "",
                stderr=f"TimeoutExpired after {timeout}s",
            )
        except FileNotFoundError as e:
            return ExecResult(returncode=127, stderr=str(e))


@dataclass
class _FakeRule:
    match: str          # argv 拼接后需包含的子串
    result: ExecResult
    sequence: Optional[List[ExecResult]] = None  # 非空时按调用次序依次返回（末项重复）
    cursor: int = 0

    def next_result(self) -> ExecResult:
        """取本次应返回的结果（序列规则推进 cursor，末项用尽后重复）"""
        if not self.sequence:
            return self.result
        idx = min(self.cursor, len(self.sequence) - 1)
        self.cursor += 1
        return self.sequence[idx]


class FakeExecutor(CommandExecutor):
    """测试替身：按「argv 含子串 → scripted ExecResult」返回，并记录所有调用。

    用法:
        fake = FakeExecutor()
        fake.when("inspect_env", stdout=json.dumps({...}))
        fake.when("accuracy_compare", returncode=1)   # 判为不达标
        ...
        fake.calls  # -> List[List[str]]，断言发了对的命令
    未命中任何规则时返回 default（默认 returncode=0，空输出）。
    """

    def __init__(self, default: Optional[ExecResult] = None):
        self.rules: List[_FakeRule] = []
        self.calls: List[List[str]] = []
        self.default = default if default is not None else ExecResult(returncode=0)

    def when(
        self,
        match: str,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> "FakeExecutor":
        """注册一条规则（链式）。先注册的先匹配。"""
        self.rules.append(_FakeRule(match, ExecResult(returncode, stdout, stderr)))
        return self

    def when_sequence(
        self,
        match: str,
        results: List[ExecResult],
    ) -> "FakeExecutor":
        """注册一条**序列**规则（链式）：第 N 次命中返回 results[N]，末项重复。

        用于「同一命令多次调用、输出需递变」的场景（如 V4 性能搜索逐算子试禁用，
        每轮 benchmark 吞吐不同）。先注册的先匹配。
        """
        if not results:
            raise ValueError("when_sequence 需要至少一个结果")
        self.rules.append(_FakeRule(match, results[-1], sequence=list(results)))
        return self

    def run(self, argv: List[str], timeout: Optional[int] = None) -> ExecResult:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for rule in self.rules:
            if rule.match in joined:
                return rule.next_result()
        return self.default

    def calls_containing(self, substr: str) -> List[List[str]]:
        """便捷断言：返回所有含 substr 的调用"""
        return [c for c in self.calls if substr in " ".join(c)]
