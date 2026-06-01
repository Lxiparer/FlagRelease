# Agent Triage Output Schema

## agent_triage_queue.jsonl

One JSON object per optimization target. Targets are Gems-slower semantic nodes, sorted
by absolute kernel-time delta (largest first).

Fields:

- `rank`: queue rank (1 = highest absolute Gems-slower delta).
- `semantic_key`: exact semantic key accepted by `semantic_perf_query.py --semantic-key`.
- `display_path`: human-readable path.
- `name`: leaf semantic name.
- `priority_reason`: why this node is in the queue.
- `gems_kernel_ns`, `vendor_kernel_ns`: semantic subtree kernel execution time.
- `delta_gems_minus_vendor_ns`: positive means Gems is slower.
- `ratio_gems_over_vendor`: Gems time divided by Vendor time.
- `gems_kernel_count`, `vendor_kernel_count`: matched subtree kernel counts.
- `evidence_tags`: dashboard evidence tags when available.

## agent_semantic_index.json

Contains all exact compare nodes and the optimization queue. Use it for programmatic
navigation, but prefer the query script for summaries.

- `kind`: `gpu_gems_vendor_agent_index`.
- `run_dir`, `gems_json`, `vendor_json`: provenance.
- `compare_nodes`: map of semantic_key -> node (status, gems/vendor ns, delta, ratio,
  counts, gems/vendor kernel ids).
- `queue`: same rows as `agent_triage_queue.jsonl`.

## describe packet

`semantic_perf_query.py describe --format json` returns:

- `summary`: total times, delta, ratio, kernel counts.
- `kernel_mix`: top kernels by time for Gems and Vendor.
- `shape_diff`: common / Gems-only / Vendor-only shape transitions.
- `cpu_stack_clusters`: stack signatures grouped by total kernel time.
- `source_locations`: source file/line/function extracted from CPU stacks.
- `kernel_ids`: exact GPU task IDs for both sides.

## agent_container_context.json

Optional. Produced from the auto_profile config (`build_profile_context`). Drives
`container_source_query.py`. Per-side fields under `sides.<gems|vendor>`:

- `container`: container name to `docker exec` into.
- `workdir`: working directory inside the container.
- `image`, `model_path`, `served_model_name`, `port`, `profile_dir_in_container`.

`safety` enforces: do not start both containers, read-only source access by default,
verification requires explicit `--yes`, never start vLLM from the agent.
