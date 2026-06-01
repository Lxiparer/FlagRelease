---
name: gpu-semantic-triage
description: Analyze Gems vs Vendor semantic compare runs on H100/Hygon; rank slower Gems semantic nodes, inspect kernels, CPU stacks, shapes, and operator source context, and propose/verify fixes.
---

# GPU Semantic Triage

Use this skill when the user asks you to investigate why a **Gems** semantic node is
slower than **Vendor** in a GPU (H100 / Hygon) profiler compare run produced by this
pipeline (`probe_and_compare.py` / `run_full_pipeline.py`).

The human-facing dashboard (`compare_dashboard.html`) and the per-side
`*.cpu_stack.html` are useful for navigation, but do **not** use HTML as the analysis
source. Use the structured query script and the pipeline JSON/CSV outputs.

## Run-specific instruction file

If a run directory contains `CLAUDE_TRIAGE.md`, **read it first**. It is auto-generated
and contains the exact `run_dir`, decode index, the already-sorted Gems-slower targets,
how to enter each container for source, any local source path maps, and the recommended
commands for this run. Treat it as the task entry point, but still use this skill's
workflow and `semantic_perf_query.py` for evidence.

## Required input

Ask for or infer a `run_dir` containing a completed compare run, usually created by:

```bash
python probe_and_compare.py \
  --gems-trace  /path/to/gems.pt.trace.json.gz \
  --vendor-trace /path/to/vendor.pt.trace.json.gz \
  --decode-idx 30 \
  --out-dir ./output
```

Expected files under `run_dir`:

```text
src/gems/final*.cpu_stack.normalized_tree.json      (or gems/...)
src/vendor/final*.cpu_stack.normalized_tree.json    (or vendor/...)
compare_dashboard.html, compare_hotspots.csv, compare_nodes.csv
agent_triage_queue.jsonl        (built by step 1 below if missing)
agent_semantic_index.json       (built by step 1 below if missing)
agent_container_context.json    (optional; enables container-aware source access)
```

If the `agent_*` files are missing, build them first.

## Query script

Prefer this script in the repository:

```bash
python agent/semantic_perf_query.py ...
```

If this skill has been copied outside the repository (e.g. bundled into a run dir),
use the bundled copy and point it at the repo root that holds
`clean_compare_and_generate_dashboard.py`:

```bash
python scripts/semantic_perf_query.py ... --repo-root /path/to/program
```

## Workflow

### 1. Build or refresh the agent index

```bash
python agent/semantic_perf_query.py build-index --run-dir <run_dir>
```

This creates:

```text
<run_dir>/agent_triage_queue.jsonl
<run_dir>/agent_semantic_index.json
```

### 2. Pick an optimization target

Default queue is Gems slower than Vendor, sorted by absolute kernel-time delta:

```bash
python agent/semantic_perf_query.py list-targets --run-dir <run_dir> --sort delta --limit 30
```

Use absolute delta as the default priority. Ratio-only rankings can be misleading for
tiny kernels.

### 3. Describe one target

Use rank or exact semantic key:

```bash
python agent/semantic_perf_query.py describe --run-dir <run_dir> --rank 1
python agent/semantic_perf_query.py describe --run-dir <run_dir> --rank 1 --format json
```

Inspect: Gems/Vendor kernel time and delta, kernel mix, shape diff, CPU stack clusters,
source locations.

### 4. Drill into kernel mix

```bash
python agent/semantic_perf_query.py kernels --run-dir <run_dir> --rank 1
```

Decide whether the slowdown is:

- same semantic path but different kernel lowering;
- same kernel name but much slower kernel time;
- additional Gems-only kernels;
- shape/layout mismatch.

### 5. Drill into CPU stacks

```bash
python agent/semantic_perf_query.py stacks --run-dir <run_dir> --rank 1 --side gems
python agent/semantic_perf_query.py stacks --run-dir <run_dir> --rank 1 --side vendor
```

Use `--full` only when the clustered stack summary is insufficient.

### 6. Read operator source context

Map profiler paths to local paths if local source exists:

```bash
python agent/semantic_perf_query.py source \
  --run-dir <run_dir> --rank 1 --side gems \
  --path-map /vllm-workspace=/Users/<you>/workspace \
  --context-lines 40
```

Read the Vendor side too when comparing lowering choices.

## Container-aware source access

If the run directory contains `agent_container_context.json`, use the controlled
container tool for source access instead of manually running arbitrary docker commands:

```bash
python agent/container_source_query.py show-context --run-dir <run_dir>
python agent/container_source_query.py snapshot --run-dir <run_dir> --rank 1 --side both
python agent/container_source_query.py source --run-dir <run_dir> --side gems --path <path> --context-lines 80
```

Rules:

- Do not start both Gems and Vendor containers simultaneously (they contend for the GPU).
- Do not start vLLM unless the user explicitly asks for verification.
- Use `snapshot` or `source` for read-only source inspection.
- Use `verify` only when the user explicitly asks for a verification run; it requires `--yes`.
- Prefer source files from `source_snapshot/` after snapshotting, so repeated analysis
  does not need repeated container access.

## Analysis rules

When reporting a performance hypothesis, structure it as:

1. Finding: what semantic node is slow and by how much (absolute kernel-time delta).
2. Evidence: kernel mix, shapes, CPU stack path, source locations.
3. Likely cause: lowering choice, extra cast/copy, non-fused path, scalar/broadcast
   handling, layout conversion, suboptimal Triton/FlagGems kernel selection, etc.
4. Verification experiment: microbenchmark or targeted trace check.
5. Suggested code area: file/function names from source locations.
6. Risk: correctness, shape generality, graph/eager differences.

Do not claim an optimization is correct unless the profile evidence supports it. If the
CPU stack or shape differs substantially between Gems and Vendor, mark the conclusion as
low confidence and recommend verifying semantic equivalence first.

## Important conventions

- Compare only semantic subtree kernel execution time.
- Do not use gap, host time, or communication time as the optimization metric.
- Use exact kernel IDs from the query output, not folded dashboard paths.
- Avoid re-analyzing ancestor/child duplicates; the queue is already hotspot-frontier
  oriented (non-overlapping).
- Prefer self/direct kernel evidence when available. If a parent node is slow only
  because of its children, continue down to the child target.
