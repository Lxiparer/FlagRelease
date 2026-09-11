"""Workflow CLI - 引擎薄入口

- `main.py`：确定性引擎入口（宿主侧运行，经 docker exec 操作容器）
- `generate_comparison_and_config.py`：评测后处理脚本，由编排层以容器内路径调用
  （`run_pipeline.sh` → `/flagos-workspace/workflow/cli/...`），保留不动
"""
