#!/usr/bin/env python3
"""
测试后处理脚本 - 独立验证

创建模拟数据并测试 generate_comparison_and_config.py
"""

import json
import yaml
import tempfile
import shutil
from pathlib import Path
import subprocess
import sys


def create_mock_workspace():
    """创建模拟的工作空间"""
    workspace = Path(tempfile.mkdtemp(prefix="flagos_test_"))

    # 创建目录结构
    (workspace / "shared").mkdir(parents=True)
    (workspace / "results").mkdir(parents=True)

    return workspace


def create_mock_nv_baseline(workspace: Path):
    """创建模拟的 NV baseline"""
    baseline = {
        "gpqa_diamond": 66.8,
        "mmlu": 69.1,
        "math_500": 72.5,
    }

    baseline_file = workspace / "shared" / "nv_baseline.yaml"
    with open(baseline_file, 'w') as f:
        yaml.dump(baseline, f)

    return baseline_file


def create_mock_evaluation_result(workspace: Path, candidate: str, accuracy: float):
    """创建模拟的评测结果（使用 fast_gpqa.py 真实 schema：score + total_questions，
    文件名与 run_pipeline.sh 契约一致：v2→gpqa_flagos.json，v3→gpqa_flagos_optimized.json，v4→gpqa_v4.json）"""
    result = {
        "_producer": "fast_gpqa.py",
        "score": accuracy,
        "total_questions": 30,
        "benchmark": "gpqa_diamond",
    }

    fname = {
        "v2": "gpqa_flagos.json",
        "v3": "gpqa_flagos_optimized.json",
        "v4": "gpqa_v4.json",
    }.get(candidate, f"gpqa_{candidate}.json")
    result_file = workspace / "results" / fname
    with open(result_file, 'w') as f:
        json.dump(result, f, indent=2)

    return result_file


def create_mock_context(workspace: Path):
    """创建模拟的 context.yaml"""
    context = {
        "optimization": {
            "enabled_ops": ["torch.nn.functional.relu", "torch.nn.functional.gelu"],
            "disabled_ops": ["torch.nn.functional.softmax"],
            "category": "accuracy_tuned",
            "reason": "Accuracy optimization pass 1",
        }
    }

    context_file = workspace / "shared" / "context.yaml"
    with open(context_file, 'w') as f:
        yaml.dump(context, f)

    return context_file


def test_scenario(name: str, accuracy: float, expected_qualified: bool):
    """测试一个场景"""
    print(f"\n{'='*60}")
    print(f"  Test Scenario: {name}")
    print(f"{'='*60}")

    # 创建模拟环境
    workspace = create_mock_workspace()

    try:
        # 创建模拟数据
        nv_baseline_file = create_mock_nv_baseline(workspace)
        eval_result_file = create_mock_evaluation_result(workspace, "v3", accuracy)
        context_file = create_mock_context(workspace)

        print(f"\n✓ Mock workspace created: {workspace}")
        print(f"  - NV baseline: {nv_baseline_file}")
        print(f"  - Evaluation result: {eval_result_file} (accuracy={accuracy}%)")
        print(f"  - Context: {context_file}")

        # 运行后处理脚本
        script_path = Path(__file__).parent / "generate_comparison_and_config.py"

        cmd = [
            sys.executable,
            str(script_path),
            "--candidate", "v3",
            "--dataset", "gpqa_diamond",
            "--nv-baseline", str(nv_baseline_file),
            "--workspace", str(workspace),
        ]

        print(f"\n✓ Running: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        print("\n--- Script Output ---")
        print(result.stdout)

        if result.stderr:
            print("\n--- Script Errors ---")
            print(result.stderr)

        # 验证结果
        print("\n--- Verification ---")

        # 检查文件是否生成
        comparison_file = workspace / "results" / "accuracy_compare_v3.json"
        config_file = workspace / "results" / "operator_config_v3.json"

        files_ok = True

        if comparison_file.exists():
            print(f"✓ Comparison file generated: {comparison_file}")
            with open(comparison_file, 'r') as f:
                comparison = json.load(f)
                print(f"  - NV: {comparison['nv']}%")
                print(f"  - Current: {comparison['current']}%")
                print(f"  - Rel drop: {comparison['rel_drop_pct']:.2f}%")
                print(f"  - Aligned: {comparison['aligned']}")

                if comparison['aligned'] != expected_qualified:
                    print(f"  ✗ ERROR: Expected aligned={expected_qualified}, got {comparison['aligned']}")
                    files_ok = False
                else:
                    print(f"  ✓ Qualification matches expected: {expected_qualified}")
        else:
            print(f"✗ Comparison file NOT generated")
            files_ok = False

        if config_file.exists():
            print(f"✓ Config file generated: {config_file}")
            with open(config_file, 'r') as f:
                config = json.load(f)
                print(f"  - Enabled ops: {len(config['enabled_ops'])}")
                print(f"  - Disabled ops: {len(config['disabled_ops'])}")
                print(f"  - Category: {config['category']}")
        else:
            print(f"✗ Config file NOT generated")
            files_ok = False

        # 检查退出码
        exit_code_ok = (result.returncode == 0) == expected_qualified

        if not exit_code_ok:
            print(f"✗ Exit code mismatch: expected {'0' if expected_qualified else '1'}, got {result.returncode}")

        success = files_ok and exit_code_ok

        print(f"\n{'='*60}")
        if success:
            print(f"  ✓ Test PASSED: {name}")
        else:
            print(f"  ✗ Test FAILED: {name}")
        print(f"{'='*60}")

        return success

    finally:
        # 清理
        shutil.rmtree(workspace)
        print(f"\n✓ Cleaned up: {workspace}")


def main():
    print("\n" + "="*60)
    print("  Testing generate_comparison_and_config.py")
    print("="*60)

    tests = [
        ("Qualified (accuracy close to NV)", 65.5, True),   # 66.8 * 0.95 = 63.46, 65.5 > 63.46 → qualified
        ("Not qualified (accuracy too low)", 60.0, False),  # 60.0 < 63.46 → not qualified
        ("Qualified (exact threshold)", 63.46, True),       # exactly at 5% drop threshold
    ]

    results = []

    for name, accuracy, expected_qualified in tests:
        success = test_scenario(name, accuracy, expected_qualified)
        results.append((name, success))

    # 总结
    print("\n" + "="*60)
    print("  Test Summary")
    print("="*60)

    passed = sum(1 for _, success in results if success)
    total = len(results)

    for name, success in results:
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"  {status}: {name}")

    print(f"\n  Total: {passed}/{total} passed")
    print("="*60 + "\n")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
