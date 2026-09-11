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

"""V3 性能纯测量：真实跑 benchmark（经 executor）、只记录绝对值、无 gate（M1b）"""

import unittest
import sys
import json
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.domain.v3_performance import V3PerformanceMeasurement
from workflow.engine.command_executor import FakeExecutor
from workflow.schemas.context_v2 import OperatorRevision


class TestV3PerformanceMeasurement(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.rev = OperatorRevision(revision_id="v3-final")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _measurer(self, fake):
        return V3PerformanceMeasurement(
            workspace_root=self.tmpdir,
            container_name="ctr",
            executor=fake,
        )

    def test_measure_success_parses_stdout(self):
        fake = FakeExecutor()
        fake.when("benchmark_runner", returncode=0,
                  stdout=json.dumps({"throughput_tokens_per_sec": 1000.0,
                                     "ttft_ms": 50.0, "tpot_ms": 10.0}))
        m = self._measurer(fake)
        ok, perf = m.measure_performance("v3", self.rev, mode="quick")
        self.assertTrue(ok)
        self.assertEqual(perf["throughput_tokens_per_sec"], 1000.0)
        self.assertIn("artifact_id", perf)
        # 命令确实发给了 benchmark_runner（唯一性能入口）
        self.assertTrue(fake.calls_containing("benchmark_runner"))

    def test_benchmark_failure_returns_false(self):
        fake = FakeExecutor()
        fake.when("benchmark_runner", returncode=1, stderr="boom")
        m = self._measurer(fake)
        ok, perf = m.measure_performance("v3", self.rev, mode="quick")
        self.assertFalse(ok)
        self.assertEqual(perf, {})

    def test_no_json_no_file_returns_false(self):
        """benchmark 退出 0 但既无 stdout JSON 也无结果文件 → 视为无有效结果"""
        fake = FakeExecutor()
        fake.when("benchmark_runner", returncode=0, stdout="running...\n(no json)")
        m = self._measurer(fake)
        ok, perf = m.measure_performance("v3", self.rev, mode="quick")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
