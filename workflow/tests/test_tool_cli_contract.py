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

"""容器内工具 CLI 契约测试（真实日志验证的固化）

**为什么需要它**：FakeExecutor 只会照着我发出去的命令返回我预设的假数据——
命令里参数名写错（`--candidate` 而非 `--v2`、`--max-timeout` 而非不存在的参数）
它永远发现不了。本轮真机验证就抓到了 3 处这类错，所以把「我们发的每个参数都必须是
对应工具真实声明的参数」固化成断言：参数集合直接从**仓库里工具源码的 argparse
声明**解析，工具改了/我们改错了都会在这里红。

（注：验证时用的是容器里**部署版**工具，已明显落后于仓库版。这里以仓库版为准——
真跑前 `setup_workspace.sh` 会把仓库版重新部署进容器。）
"""

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.engine.workflow_engine import WorkflowEngine
from workflow.engine.command_executor import ExecResult

from workflow.tests.test_engine_e2e import make_fake

REPO = Path(__file__).parent.parent.parent

# 工具名 → 仓库源码（真跑时由 setup_workspace.sh 部署到容器 /flagos-workspace/scripts/）
TOOL_SOURCES = {
    "inspect_env.py": "skills/flagos-pre-service-inspection/tools/inspect_env.py",
    "eval_wrapper.py": "skills/flagos-eval-comprehensive/tools/eval_wrapper.py",
    "fast_gpqa.py": "skills/flagos-eval-comprehensive/tools/fast_gpqa.py",
    "accuracy_compare.py": "skills/flagos-eval-comprehensive/tools/accuracy_compare.py",
    "apply_op_config.py": "skills/flagos-operator-replacement/tools/apply_op_config.py",
    "flagos_op_config.py": "skills/flagos-operator-replacement/tools/flagos_op_config.py",
    "diagnose_ops.py": "skills/flagos-operator-replacement/tools/diagnose_ops.py",
    "benchmark_runner.py": "skills/flagos-performance-testing/tools/benchmark_runner.py",
}

_FLAG_RE = re.compile(r"(?<![-\w])(--[a-zA-Z][a-zA-Z0-9-]*)")
_ARG_RE = re.compile(r"""add_argument\(\s*["'](--[a-zA-Z0-9-]+)["']""")


def declared_flags(tool: str) -> set:
    """从工具源码的 argparse 声明里解析它接受的参数集合"""
    src = (REPO / TOOL_SOURCES[tool]).read_text(encoding="utf-8")
    return set(_ARG_RE.findall(src))


def flags_per_tool(command: str) -> dict:
    """把一条命令按出现的工具切段，归因每段里出现的参数

    一条命令里可能嵌套两个工具（eval_wrapper 的 --eval-cmd 里包着 fast_gpqa），
    按工具名出现位置切段即可正确归属。
    """
    positions = sorted(
        (command.find(tool), tool) for tool in TOOL_SOURCES if command.find(tool) >= 0
    )
    out = {}
    for idx, (pos, tool) in enumerate(positions):
        end = positions[idx + 1][0] if idx + 1 < len(positions) else len(command)
        out.setdefault(tool, set()).update(_FLAG_RE.findall(command[pos:end]))
    return out


class TestToolCliContract(unittest.TestCase):
    """引擎发出的每条工具命令，参数都必须是该工具真实声明的"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        for sub in ("shared", "results", "logs"):
            (Path(cls.tmpdir) / sub).mkdir(parents=True, exist_ok=True)

        fake = make_fake()
        # V4 两轮探针需要递变吞吐（首轮基线之后两轮都要低于/高于基线各覆盖一次）
        fake.when_sequence("benchmark_runner", [
            ExecResult(0, json.dumps({"throughput_tokens_per_sec": t,
                                      "ttft_ms": 1.0, "tpot_ms": 1.0}))
            for t in (500.0, 400.0, 450.0)
        ])
        # 诊断类输出
        fake.when("diagnose_ops", returncode=0, stdout=json.dumps(
            {"crashed_ops": ["op_x"], "candidate_ops": [], "evidence": []}))
        cls.engine = WorkflowEngine(cls.tmpdir, executor=fake)
        cls.engine.context.runtime.container_name = "ctr"
        cls.engine.context.runtime.model_name = "TestModel"
        cls.engine.context.runtime.model_path = "/models/TestModel"
        cls.engine.run()
        cls.commands = [" ".join(call) for call in fake.calls]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_walked_through_whole_workflow(self):
        """前置条件：确实跑完了全流程（否则下面的契约断言会漏掉工具）"""
        for sid, step in self.engine.context.steps.items():
            self.assertIn(step.status, ("success", "skipped"), f"{sid}: {step.status}")

    def test_every_flag_is_declared_by_its_tool(self):
        """核心断言：发出的每个 --flag 都必须在工具源码里声明过"""
        violations = []
        for cmd in self.commands:
            for tool, flags in flags_per_tool(cmd).items():
                valid = declared_flags(tool)
                for flag in sorted(flags - valid):
                    violations.append(f"{tool} 不认识 {flag}  ← {cmd[:160]}")
        self.assertEqual(violations, [], "发出了工具未声明的参数：\n" + "\n".join(violations))

    def test_accuracy_eval_goes_through_eval_wrapper(self):
        """精度评测必须经 eval_wrapper 监督执行（编排层硬性要求）"""
        evals = [c for c in self.commands if "accuracy_compare.py" not in c
                 and "fast_gpqa.py" in c]
        self.assertTrue(evals, "没有发出评测命令")
        for cmd in evals:
            self.assertIn("eval_wrapper.py", cmd,
                          f"评测绕过了 eval_wrapper（stalled/超时看门狗会失效）：{cmd[:160]}")

    def test_inspect_env_requests_json(self):
        """inspect_env 默认输出人类可读报告，必须显式 --output-json"""
        calls = [c for c in self.commands if "inspect_env.py" in c]
        self.assertTrue(calls)
        for cmd in calls:
            self.assertIn("--output-json", cmd)

    def test_benchmark_uses_strategy_not_mode_for_quick(self):
        """--strategy 才是策略开关；--mode 是标记，不能拿 quick 当标记"""
        calls = [c for c in self.commands if "benchmark_runner.py" in c]
        self.assertTrue(calls)
        for cmd in calls:
            self.assertIn("--strategy quick", cmd)
            self.assertNotIn("--mode quick", cmd)

    def test_accuracy_compare_uses_current_flag_names(self):
        """判定脚本用当前口径的参数名（--candidate/--reference）"""
        calls = [c for c in self.commands if "accuracy_compare.py" in c]
        self.assertTrue(calls)
        for cmd in calls:
            self.assertIn("--candidate", cmd)
            self.assertIn("--nv-baseline", cmd)

    def test_no_stale_deployed_paths(self):
        """命令里不得出现容器中不存在的 /flagos-workspace/skills/ 路径"""
        stale = [c for c in self.commands if "/flagos-workspace/skills/" in c]
        self.assertEqual(stale, [])


if __name__ == "__main__":
    unittest.main()
