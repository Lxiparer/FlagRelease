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

"""V3 精度评测：判定权在 accuracy_compare.py 退出码（M1a 核心「降权」）"""

import unittest
import sys
import json
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.domain.v3_accuracy import V3AccuracyEvaluation
from workflow.engine.command_executor import FakeExecutor
from workflow.schemas.context_v2 import OperatorRevision


def _fake(eval_rc=0, compare_rc=0, compare_json=None):
    fake = FakeExecutor()
    fake.when("fast_gpqa", returncode=eval_rc, stdout=json.dumps({"score": 30.0}))
    fake.when(
        "accuracy_compare",
        returncode=compare_rc,
        stdout=json.dumps(compare_json if compare_json is not None
                          else {"nv": {"score": 66.8}, "rel_drop": 0.02}),
    )
    return fake


class TestV3AccuracyJudgment(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.rev = OperatorRevision(revision_id="v3-discovered")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _evaluator(self, fake):
        return V3AccuracyEvaluation(
            workspace_root=self.tmpdir,
            container_name="ctr",
            executor=fake,
        )

    def test_exit0_qualified(self):
        ev = self._evaluator(_fake(compare_rc=0))
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertTrue(ok)
        r = results["gpqa_diamond"]
        self.assertTrue(r["qualified"])
        self.assertEqual(r["exit_code"], 0)
        # artifact 键对齐（reducer 需要）
        self.assertEqual(r["nv_reference_value"], 66.8)
        self.assertEqual(r["relative_drop"], 0.02)

    def test_exit1_not_qualified_even_if_reldrop_small(self):
        """退出码=1 判不达标，即使 stdout 的 rel_drop 很小——证明判定来自退出码而非内联计算"""
        ev = self._evaluator(_fake(compare_rc=1,
                                   compare_json={"nv": {"score": 66.8}, "rel_drop": 0.01}))
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertFalse(ok)
        self.assertFalse(results["gpqa_diamond"]["qualified"])
        self.assertEqual(results["gpqa_diamond"]["exit_code"], 1)

    def test_exit3_fail_closed(self):
        """退出码=3（缺 NV 参考）→ fail-closed 不达标"""
        ev = self._evaluator(_fake(compare_rc=3, compare_json={"missing_nv": True}))
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertFalse(ok)
        self.assertEqual(results["gpqa_diamond"]["exit_code"], 3)

    def test_eval_failure_skips_compare(self):
        """评测本身失败 → 该数据集不达标，且不调用 accuracy_compare"""
        fake = _fake(eval_rc=1)
        ev = self._evaluator(fake)
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond"], "Qwen3-8B")
        self.assertFalse(ok)
        self.assertFalse(results["gpqa_diamond"]["success"])
        self.assertEqual(len(fake.calls_containing("accuracy_compare")), 0)

    def test_multi_dataset_all_must_pass(self):
        """多数据集：一个不达标则整体不达标"""
        fake = FakeExecutor()
        fake.when("fast_gpqa", returncode=0, stdout=json.dumps({"score": 30.0}))
        # gpqa 达标、mmlu 不达标（按 metric 区分命令里的 --metric）
        fake.when("--metric gpqa_diamond", returncode=0,
                  stdout=json.dumps({"nv": {"score": 66.8}, "rel_drop": 0.01}))
        fake.when("--metric mmlu", returncode=1,
                  stdout=json.dumps({"nv": {"score": 69.1}, "rel_drop": 0.2}))
        ev = self._evaluator(fake)
        ok, results = ev.evaluate_accuracy("v3", self.rev, ["gpqa_diamond", "mmlu"], "Qwen3-8B")
        self.assertFalse(ok)
        self.assertTrue(results["gpqa_diamond"]["qualified"])
        self.assertFalse(results["mmlu"]["qualified"])


if __name__ == "__main__":
    unittest.main()
