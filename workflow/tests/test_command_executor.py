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

"""CommandExecutor seam 测试"""

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.engine.command_executor import (
    ExecResult,
    FakeExecutor,
    SubprocessExecutor,
    build_docker_exec_argv,
)


class TestBuildDockerExecArgv(unittest.TestCase):
    def test_basic_argv_has_conda_path(self):
        argv = build_docker_exec_argv("ctr", "python3 x.py")
        self.assertEqual(argv[:3], ["docker", "exec", "ctr"])
        self.assertEqual(argv[3:5], ["bash", "-lc"])
        self.assertIn("PATH=/opt/conda/bin:$PATH", argv[-1])
        self.assertIn("python3 x.py", argv[-1])

    def test_detach_and_env(self):
        argv = build_docker_exec_argv("ctr", "run", detach=True, env={"A": "1"})
        self.assertIn("-d", argv)
        self.assertIn("-e", argv)
        self.assertIn("A=1", argv)


class TestFakeExecutor(unittest.TestCase):
    def test_rule_matching_and_default(self):
        fake = FakeExecutor()
        fake.when("inspect_env", returncode=0, stdout="OK")
        fake.when("accuracy_compare", returncode=1)

        r1 = fake.docker_exec("ctr", "python3 inspect_env.py")
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r1.stdout, "OK")

        r2 = fake.docker_exec("ctr", "python3 accuracy_compare.py --candidate x")
        self.assertEqual(r2.returncode, 1)

        # 未命中规则 → default（returncode 0）
        r3 = fake.run(["echo", "hi"])
        self.assertEqual(r3.returncode, 0)

    def test_records_calls(self):
        fake = FakeExecutor()
        fake.docker_exec("ctr", "python3 a.py")
        fake.run(["ls", "-l"])
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(fake.calls_containing("a.py")), 1)

    def test_first_matching_rule_wins(self):
        fake = FakeExecutor()
        fake.when("python3", returncode=7)
        fake.when("inspect_env", returncode=9)  # 更具体但注册在后
        r = fake.docker_exec("ctr", "python3 inspect_env.py")
        self.assertEqual(r.returncode, 7)  # 先注册的先命中


class TestSubprocessExecutor(unittest.TestCase):
    def test_echo_smoke(self):
        ex = SubprocessExecutor()
        r = ex.run(["echo", "hello"])
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout.strip(), "hello")

    def test_nonzero_exit(self):
        ex = SubprocessExecutor()
        r = ex.run(["bash", "-c", "exit 3"])
        self.assertEqual(r.returncode, 3)
        self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main()
