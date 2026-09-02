# Step 14 (LangGraph Migration) - Deferred

**Date**: 2026-09-02  
**Status**: **DEFERRED**  
**Decision**: Pause LangGraph migration, focus on stabilizing current architecture

---

## Decision Rationale

After completing Step 13 (dual pipeline removal), we reassessed the necessity of migrating from Shell orchestration to LangGraph. The decision to defer is based on:

### 1. Current Architecture is Sufficient

**What We Have**:
- ✅ Shell orchestration (`run_pipeline.sh`) - battle-tested, deterministic
- ✅ Python domain models (`workflow/domain/`) - well-structured, testable
- ✅ 38 unit tests (100% passing) - core logic validated
- ✅ Artifact Registry + Gates - fail-closed validation
- ✅ Agent integration - suggest-verify-commit loop working
- ✅ Simplified workflow - Plugin-only (V3→V4), no complex branching

**Post-Refactor Simplification**:
- Before: 3 pipelines (Branch A/B/native) with complex routing
- After: 1 pipeline (Plugin-only) with linear flow
- Reduced complexity by ~60%

### 2. LangGraph ROI Analysis

| Benefit | Value | Can Achieve Without LangGraph? |
|---------|-------|-------------------------------|
| State persistence | High | ✅ Yes - enhance context.yaml checkpointing |
| Parallel execution | Medium | ✅ Yes - Shell `&` + `wait`, Python asyncio |
| Visualization | Low | Flow already simple enough to document |
| Testing | Medium | ✅ Already have 38 unit tests for domain logic |
| Resumability | High | ✅ Yes - improve state markers in run_pipeline.sh |

**Estimated Cost**:
- Development: 5-6 days
- New dependencies: langgraph, langchain-core
- Learning curve for team
- Risk of introducing new bugs

**Conclusion**: 80% of the benefits can be achieved with 20% of the cost by enhancing the existing system.

### 3. Current Pain Points Can Be Solved Incrementally

**Pain Point 1: Parallel Evaluation**
```bash
# Current: Sequential
eval_gpqa.sh && eval_mmlu.sh && eval_math.sh

# Enhancement: Parallel (pure Shell)
eval_gpqa.sh & pid1=$!
eval_mmlu.sh & pid2=$!
eval_math.sh & pid3=$!
wait $pid1 $pid2 $pid3
```

**Pain Point 2: State Recovery**
```bash
# Current: Relies on Shell variables
# Enhancement: Add checkpoint system
prompts/run_pipeline.sh --resume-from step-4
# Reads context.yaml, detects last completed step, resumes from there
```

**Pain Point 3: State Fragmentation**
```bash
# Current: context.yaml + traces/ + logs/
# Enhancement: Consolidate to context.yaml only
# All tools write to single source of truth
```

---

## Alternative Considered: Partial Migration (Rejected)

We considered migrating only the V3/V4 workflow (segments 2-4) to LangGraph while keeping segment 1 (container prep) in Shell.

**Pros**:
- Reduced migration cost (~3 days vs ~6 days)
- Keep proven container management logic
- Get LangGraph benefits for evaluation/optimization steps

**Cons**:
- **Hybrid complexity**: Two orchestration systems instead of one
- **Handoff complexity**: Shell → LangGraph → Shell transitions
- **Debugging harder**: Need to understand both systems
- **Still significant cost**: 3 days for uncertain benefit

**Decision**: Even partial migration not justified at this time.

---

## What Gets Deferred

### Planned LangGraph Implementation (Not Built)

```python
# This will NOT be implemented now

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

class PluginOnlyState(TypedDict):
    context: ContextSchemaV2
    artifacts: dict[str, Artifact]
    gates: dict[str, GateStatus]
    current_step: str

def build_plugin_only_graph():
    graph = StateGraph(PluginOnlyState)
    
    # Admission
    graph.add_node("admission", admission_node)
    
    # V3 workflow
    graph.add_node("v3_discovery", v3_discovery_node)
    graph.add_node("v3_startup", v3_startup_node)
    graph.add_node("v3_accuracy", v3_accuracy_node)
    graph.add_node("v3_performance", v3_performance_node)
    graph.add_node("v3_release", v3_release_node)
    
    # V4 workflow
    graph.add_node("v4_reduction", v4_reduction_node)
    graph.add_node("v4_release", v4_release_node)
    
    # Edges with conditionals
    graph.add_edge(START, "admission")
    graph.add_conditional_edges(
        "admission",
        lambda state: "v3_discovery" if state['gates']['admission'] == 'pass' else END
    )
    # ... more edges
    
    return graph.compile(checkpointer=MemorySaver())

# Deferred: Not implementing this
```

### Features Not Built

1. **State machine visualization**: LangGraph's auto-generated Mermaid diagrams
2. **Built-in checkpointing**: LangGraph's MemorySaver/SqliteSaver
3. **Automatic retry logic**: LangGraph's error handling
4. **Graph-native parallelism**: LangGraph's concurrent node execution

---

## What We Do Instead

### Near-Term Enhancements (Recommended)

#### 1. Enhance State Management
```bash
# Add to run_pipeline.sh

# Checkpoint function
checkpoint_state() {
    local step=$1
    docker exec $CONTAINER bash -c "
        PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/update_context.py \
            --set workflow.last_completed_step='${step}' \
            --set workflow.checkpoint_timestamp='$(date -Iseconds)' \
            --json
    "
}

# Resume function
resume_from_checkpoint() {
    local last_step=$(docker exec $CONTAINER bash -c "
        PATH=/opt/conda/bin:\$PATH python3 -c \"
import yaml
with open('/flagos-workspace/shared/context.yaml') as f:
    print(yaml.safe_load(f).get('workflow', {}).get('last_completed_step', ''))
\"")
    echo "Resuming from: ${last_step}"
    # Jump to next step logic
}
```

#### 2. Add Parallel Evaluation Support
```bash
# Add to run_pipeline.sh segment 2

parallel_eval() {
    local datasets="$1"  # "gpqa_diamond,mmlu,math_500"
    local pids=()
    
    for ds in ${datasets//,/ }; do
        eval_dataset "$ds" &
        pids+=($!)
    done
    
    # Wait for all
    local failed=0
    for pid in "${pids[@]}"; do
        wait $pid || ((failed++))
    done
    
    return $failed
}
```

#### 3. Consolidate State Files
```python
# Update all tools to write only to context.yaml
# Deprecate separate trace files

# workflow/domain/v3_accuracy.py
def v3_accuracy_evaluation(...):
    result = run_evaluation(...)
    
    # Before: Write to traces/04_accuracy.json + context.yaml
    # After: Only write to context.yaml
    context_manager.update({
        'eval.v3_accuracy': result,
        'workflow_ledger.steps.v3_accuracy': {
            'status': 'success',
            'timestamp': datetime.now().isoformat(),
        }
    })
```

---

## Conditions for Reconsidering LangGraph

We will **reconsider** LangGraph migration if:

1. **Workflow becomes complex again**: Need for conditional branching beyond simple if/else
2. **Parallel execution requirements grow**: Need to orchestrate >5 concurrent tasks
3. **Team preference shifts**: Python-first team uncomfortable with Shell
4. **Debugging pain increases**: State management bugs become frequent
5. **External integration needs**: Need to plug into LangChain ecosystem

**Threshold**: If we spend >2 days fixing orchestration bugs in a month, revisit this decision.

---

## Current Priority Stack (Updated)

### Immediate (This Week)

1. ✅ **Step 13 completion** (80% done)
   - [x] Remove dual pipeline logic
   - [x] Plugin-only admission
   - [ ] Archive old operator tools
   - [ ] Update CLAUDE.md
   - [ ] Write migration guide

2. 🔜 **Near-term enhancements** (instead of LangGraph)
   - [ ] Add checkpoint/resume to run_pipeline.sh
   - [ ] Add parallel evaluation support
   - [ ] Consolidate state to context.yaml only

### Short-Term (Next 2 Weeks)

3. 🔜 **Step 15: End-to-end validation**
   - [ ] Test with real container
   - [ ] Validate all 15 workflow steps
   - [ ] Performance benchmark
   - [ ] Document known issues

### Medium-Term (Next Month)

4. ⏸️ **Step 14: LangGraph migration** (DEFERRED)
   - Status: On hold pending reevaluation
   - Trigger: See "Conditions for Reconsidering" above

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-09-02 | Defer Step 14 | Current architecture sufficient post-refactor, ROI too low |
| 2026-09-02 | Prioritize Step 13 completion | Finish what we started before new features |
| 2026-09-02 | Add near-term enhancements | Get 80% of LangGraph benefits with 20% of cost |

---

## Communication

**To Team**:
> We've completed the Plugin-only workflow refactor (Steps 1-13), which simplified the system significantly. After analysis, we're deferring the planned LangGraph migration (Step 14) because:
> 1. Current Shell + Python domain model is working well
> 2. Workflow is now simple enough (V3→V4 linear flow)
> 3. We can get most benefits through incremental enhancements
> 
> Next focus: Complete Step 13 cleanup and enhance state management.

**To Future Self**:
> If you're reading this and wondering "why didn't we use LangGraph?", the answer is pragmatism. The dual pipeline removal (Step 13) already gave us the simplification we needed. LangGraph would be over-engineering at this point. If the workflow grows complex again, revisit this decision.

---

## References

- **Architecture Before**: `docs/plugin_only_workflow_refactor_plan.md` (15-step plan with LangGraph)
- **Architecture After**: Shell orchestration + Python domain models (Steps 1-13 only)
- **Step 13 Summary**: `docs/session_summary_step13_2026-09-02.md`
- **Test Coverage**: 38 tests in `workflow/tests/` (100% passing)

---

**Status**: Step 14 officially deferred. Proceeding with Step 13 completion and near-term enhancements.
