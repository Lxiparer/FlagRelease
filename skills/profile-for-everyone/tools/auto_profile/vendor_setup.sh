# Vendor (baseline) setup, sourced inside the container before vLLM launches.
# This file mirrors v27's setup convention: plain shell, mostly env exports.
# profile_runner.py rewrites --host/--port/--served-model-name/model_path and
# injects --profiler-config; it does NOT touch this file.
export OMP_NUM_THREADS=1
export VLLM_ENGINE_READY_TIMEOUT_S=6000
# If the base image does not already ship vLLM, uncomment:
# pip install vllm --quiet
