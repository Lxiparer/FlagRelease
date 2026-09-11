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

"""V3 Performance Measurement - V3 性能纯测量

设计原则（plugin-only 工作流）：
- V3 性能只测量、只记录绝对值，**不做对比、不算 ratio、不设 Gate**
- 无本地 V1 基线，外部 NV 精度是唯一业务红线
- 性能数据作为 Artifact 落盘，供 V4 优化阶段消费（V4 的对比基准是 V3）
- 性能不达标不阻断流程，仅作为发布报告参考

与旧工作流的区别：
- 移除 performance_compare.py 的 ratio 计算
- 移除 performance_ok Gate 判定
- 只保留 benchmark_runner.py 的原始测量结果
"""

import json
import logging
import os
from typing import Dict, List, Optional, Tuple

from ..schemas.context_v2 import OperatorRevision
from ..artifacts.registry import ArtifactRegistry
from ..engine.command_executor import CommandExecutor, SubprocessExecutor, parse_json_output


# benchmark_runner.py 容器内路径（唯一性能测量入口）
BENCHMARK_RUNNER = "/flagos-workspace/scripts/benchmark_runner.py"

# --mode 是测试模式**标记**（native/flagos_initial/flagos_optimized），与 output-name 口径一致
BENCHMARK_MODE_LABEL = "flagos_optimized"


class V3PerformanceMeasurement:
    """V3 性能纯测量（无对比、无 Gate）"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
        executor: Optional[CommandExecutor] = None,
    ):
        self.workspace_root = workspace_root
        self.container_name = container_name
        self.artifact_registry = artifact_registry or ArtifactRegistry(workspace_root)
        self.executor = executor or SubprocessExecutor()
        self.logger = logging.getLogger("workflow.domain.v3_performance")

    def measure_performance(
        self,
        candidate: str,
        revision: OperatorRevision,
        mode: str = "quick",
        output_name: str = "flagos_optimized",
    ) -> Tuple[bool, Dict]:
        """测量 V3 性能（纯测量，只记录绝对值）

        Args:
            candidate: 版本标识（v3）
            revision: 当前算子 revision
            mode: benchmark 模式（quick / comprehensive）
            output_name: benchmark 输出命名。V3 固定 flagos_optimized（约束22）；
                V4 试禁用探针用 v4_probe_roundN（对齐 operator_reduction.py）

        Returns:
            (是否测量成功, 性能结果字典)

        Note:
            无论性能高低都返回 success=True（只要测量本身成功）。
            性能不达标不是失败——V3 无本地基线可比，不设性能 Gate。
        """
        self.logger.info(
            f"Measuring V3 performance (candidate={candidate}, "
            f"mode={mode}, revision={revision.revision_id})"
        )

        # 执行 benchmark（V3 标准命名 flagos_optimized，见约束22）
        success, perf_data = self._run_benchmark(
            output_name=output_name,
            mode=mode,
        )

        if not success:
            self.logger.error("Benchmark execution failed")
            return False, {}

        # 记录绝对值（不做任何对比）
        self.logger.info(
            f"V3 performance measured: "
            f"throughput={perf_data.get('throughput_tokens_per_sec', 0):.1f} tokens/s, "
            f"TTFT={perf_data.get('ttft_ms', 0):.1f} ms"
        )

        # 落盘为 Artifact，供 V4 消费
        artifact_id = self.register_performance_artifact(
            candidate,
            revision,
            perf_data,
            mode,
            output_name=output_name,
        )
        perf_data["artifact_id"] = artifact_id

        return True, perf_data

    def _run_benchmark(
        self,
        output_name: str,
        mode: str,
        mode_label: str = BENCHMARK_MODE_LABEL,
    ) -> Tuple[bool, Dict]:
        """执行 benchmark_runner.py（唯一性能测量入口，经注入的 executor）

        Args:
            output_name: 输出命名（flagos_optimized for V3）
            mode: quick / comprehensive

        Returns:
            (是否成功, 性能数据)
        """
        # --strategy 选 quick/comprehensive；--mode 是**标记**（native/flagos_initial/
        # flagos_optimized），不是策略——早前误写成 `--mode quick`（argparse 不报错但标签错）。
        script = (
            f"python3 {BENCHMARK_RUNNER} --strategy {mode} "
            f"--mode {mode_label} --output-name {output_name}"
        )
        res = self.executor.docker_exec(self.container_name, script, timeout=3600)
        if not res.ok:
            self.logger.error(f"benchmark exit={res.returncode}: {res.stderr[:300]}")
            return False, {}

        # 优先解析 stdout JSON（可单测）；回退读结果文件（真实运行 benchmark 落盘）
        perf_data = self._safe_json(res.stdout)
        if perf_data is None:
            result_file = os.path.join(
                self.workspace_root, "results", f"{output_name}.json"
            )
            if os.path.exists(result_file):
                try:
                    with open(result_file, "r") as f:
                        perf_data = json.load(f)
                except (json.JSONDecodeError, IOError) as e:
                    self.logger.error(f"Failed to read benchmark result: {e}")
                    return False, {}

        if not isinstance(perf_data, dict):
            self.logger.warning("benchmark 无有效结果（stdout 非 JSON 且无结果文件）")
            return False, {}
        return True, perf_data

    @staticmethod
    def _safe_json(text: str):
        """从可能混杂日志的 stdout 中提取 JSON 对象（best-effort，共享实现）"""
        return parse_json_output(text)

    def register_performance_artifact(
        self,
        candidate: str,
        revision: OperatorRevision,
        perf_data: Dict,
        mode: str,
        output_name: str = "flagos_optimized",
    ) -> str:
        """注册性能结果 Artifact

        Args:
            candidate: 版本标识
            revision: 算子 revision
            perf_data: 性能数据
            mode: benchmark 模式
            output_name: benchmark 输出命名（决定结果文件路径）

        Returns:
            artifact_id
        """
        # 纯测量内容：只记录绝对值，无 ratio / baseline / qualified 字段
        content = {
            "candidate": candidate,
            "operator_revision": revision.revision_id,
            "test_type": mode,
            "throughput_tokens_per_sec": perf_data.get("throughput_tokens_per_sec", 0.0),
            "ttft_ms": perf_data.get("ttft_ms", 0.0),
            "tpot_ms": perf_data.get("tpot_ms", 0.0),
            "concurrency_results": perf_data.get("concurrency_results", []),
            "input_length": perf_data.get("input_length", 4096),
            "output_length": perf_data.get("output_length", 1024),
            "_meta": {"measurement_only": "true"},
        }

        file_path = os.path.join("results", f"{output_name}.json")

        artifact_id = self.artifact_registry.register_artifact(
            artifact_type="performance-result",
            content=content,
            file_path=file_path,
            generated_by="script",
            generator_version="v3_performance_measurement_1.0",
            tags={
                "candidate": candidate,
                "operator_revision": revision.revision_id,
                "measurement_only": "true",
            },
        )
        self.logger.info(f"Registered performance artifact: {artifact_id}")
        return artifact_id
