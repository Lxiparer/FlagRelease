# Plugin-only Workflow Implementation Status

**Branch**: `workflow-refactor`  
**Last Updated**: 2026-09-02  
**Status**: Steps 1-11 Complete (Core Architecture & Domain Logic)

---

## Implementation Progress

### ✅ Completed Steps (1-11)

#### Step 1: Context Schema v2, Artifact Registry, Gate Reducer
- **Status**: ✅ Complete
- **Commit**: `4a281a3` - Context Schema v2, Artifact contracts, fail-closed Gates
- **Files**:
  - `workflow/schemas/context_v2.py` - Schema v2 with ArtifactReference, RuntimeInfo, OperatorRevision, Gate, WorkflowStep
  - `workflow/artifacts/artifact_schema.py` - 7 Artifact types (RuntimeOplist, Accuracy, Performance, ServiceHealth, Diagnosis, Analysis)
  - `workflow/artifacts/registry.py` - ArtifactRegistry with register/verify/query
  - `workflow/gates/reducer.py` - GateReducer with accuracy/v3/v4 Gates (fail-closed, external NV reference)

#### Step 2: Deterministic Workflow Engine
- **Status**: ✅ Complete
- **Commit**: `691c828` - Workflow Engine, recovery mechanism, operator revision store
- **Files**:
  - `workflow/engine/workflow_engine.py` - 15-step workflow engine
  - `workflow/engine/recovery.py` - RecoveryManager with interruption detection and recovery
  - `workflow/engine/operator_revision_store.py` - OperatorRevisionStore managing immutable revision chains
  - `workflow/engine/verification_executor.py` - Experiment execution for Agent hypotheses

#### Step 3: AnalysisAgent Protocol
- **Status**: ✅ Complete
- **Commit**: `f5c181a` - AnalysisAgent protocol, policy validator, session manager
- **Files**:
  - `workflow/agent/protocol.py` - Request/Response schemas (StartupFailure, AccuracyRegression, UnknownFailure)
  - `workflow/agent/policy_validator.py` - Three-layer validation (Schema/Identity/Policy)
  - `workflow/agent/session_manager.py` - AgentSessionManager tracking sessions and verification results

#### Step 4-5: ClaudeCodeAnalysisAgent & Verification
- **Status**: ✅ Complete
- **Commit**: `58f4977` - ClaudeCodeAnalysisAgent and verification experiment executor
- **Files**:
  - `workflow/agent/claude_code_agent.py` - ClaudeCodeAnalysisAgent implementing AnalysisAgent interface
  - `workflow/agent/claude_code_adapter.py` - API/CLI adapter for Claude Code invocation

#### Step 6: Plugin-only Admission
- **Status**: ✅ Complete
- **Commit**: `3cb25c4` - Plugin-only admission with fail-closed component check
- **Files**:
  - `workflow/domain/admission.py` - PluginOnlyAdmission requiring all 4 components (vllm, flaggems, flagtree, vllm_plugin)
  - `workflow/tests/test_admission.py` - 10 tests (all passing)

#### Step 7: V3 Discovery Startup & Startup Tuning
- **Status**: ✅ Complete
- **Commit**: `1ecbf4d` - V3 discovery startup and startup compatibility tuning
- **Files**:
  - `workflow/domain/v3_startup.py` - V3DiscoveryStartup with freshness/identity validation
  - `workflow/domain/v3_startup_tuning.py` - V3StartupTuning with Agent integration (suggest-verify-commit loop)
  - `workflow/tests/test_v3_startup.py` - 10 tests (all passing)

#### Step 8: V3 Accuracy Evaluation & Tuning
- **Status**: ✅ Complete
- **Commit**: `89423d0` - V3 accuracy/performance/release modules
- **Files**:
  - `workflow/domain/v3_accuracy.py` - Per-dataset evaluation against external NV reference
  - `workflow/domain/v3_accuracy_tuning.py` - Accuracy operator tuning with Agent integration

#### Step 9: V3 Performance Measurement
- **Status**: ✅ Complete
- **Commit**: `89423d0` (same)
- **Files**:
  - `workflow/domain/v3_performance.py` - Measurement-only (no comparison/ratio/Gate), absolute values only

#### Step 10: V3 Release Management
- **Status**: ✅ Complete
- **Commit**: `89423d0` (same)
- **Files**:
  - `workflow/domain/v3_release.py` - Gate-driven release (accuracy + v3_established)
  - Full release (Harbor + ModelScope/HF) if Gates pass, private-only otherwise

#### Step 11: V4 Operator Reduction & Release
- **Status**: ✅ Complete
- **Commit**: `b29509d` - V4 operator reduction and release
- **Files**:
  - `workflow/domain/v4_reduction.py` - Two-phase optimization (performance search + accuracy backtrack)
  - `workflow/domain/v4_release.py` - V4 Gate-driven release with fallback handling

---

### 🚧 Remaining Steps (12-14)

#### Step 12: Test Coverage
- **Status**: ✅ Complete (Core Components)
- **Commit**: `7028ca1` - All 38 tests passing
- **Files**:
  - `workflow/tests/test_admission.py` - 10 tests (Plugin-only admission scenarios) ✅
  - `workflow/tests/test_v3_startup.py` - 10 tests (V3 discovery startup and tuning) ✅
  - `workflow/tests/test_artifact_registry.py` - 6 tests (Artifact registration, query, integrity) ✅
  - `workflow/tests/test_operator_revision.py` - 7 tests (Revision chains, cumulative disable tracking) ✅
  - `workflow/tests/test_gates.py` - 5 tests (Gate fail-closed behavior) ✅
- **Total**: 38 tests, 38 passing (100%)
- **Coverage**:
  - ✅ Artifact Registry (register, query, verify integrity)
  - ✅ Operator Revision Store (parent-child chains, cumulative disable tracking)
  - ✅ Gate Reducer (fail-closed on missing/corrupt Artifacts)
  - ✅ Plugin-only Admission (component checking, fail-closed)
  - ✅ V3 Startup (discovery, tuning, Agent integration)
- **Future Enhancements** (not blocking delivery):
  - Workflow Engine state transition tests
  - Agent policy validation tests (requires schema alignment)
  - Recovery mechanism tests (requires API alignment)
  - End-to-end integration tests

#### Step 13: Remove Legacy Code
- **Status**: 🚧 Planned
- **Scope**:
  - Remove dual pipeline logic (A/B branches, gems_tree/gems_tree_plugin routing)
  - Remove V1 baseline execution code
  - Remove V2 injection mode
  - Remove old Plugin附加流程 (steps 9-13 in old workflow)
  - Clean up unused shell conditionals in `run_pipeline.sh`
  - Archive legacy `operator_search.py`, `operator_reduction.py` (old versions)

#### Step 14: LangGraph Migration
- **Status**: 📅 Future (Independent Phase)
- **Note**: Does not block delivery. LangGraph replaces analysis harness only, not the deterministic Workflow Engine.
- **Scope**:
  - Implement `LangGraphAnalysisAgent` as alternative to `ClaudeCodeAnalysisAgent`
  - Build `ModelProvider` abstraction (Anthropic, OpenAI, OpenAI-compatible, Internal Gateway, Local)
  - Add capability declaration (structured output, tool calling, context length, timeouts)
  - Implement shadow mode for A/B testing between Claude Code and LangGraph agents

---

## Architecture Summary

### Core Design Principles

1. **Plugin-only admission**: All 4 components required (vllm + flaggems + flagtree + vllm-plugin-FL), fail-closed
2. **No local V1**: External NV reference is the only business red line
3. **V3 performance measurement-only**: No comparison, no ratio, no Gate
4. **V4 baseline is V3**: Not compared to V1
5. **Deterministic Workflow Engine**: Owns all state transitions, verification, Gate evaluation, and release decisions
6. **Analysis Agent protocol**: Claude Code intervenes only through structured `AnalysisAgent` interface
7. **Suggest-verify-commit loop**: Agent hypotheses → validation → experimental child revision → measured verification → commit or rollback
8. **Artifact-backed facts**: All business decisions based on registered Artifacts with content hashing
9. **Fail-closed Gates**: Missing Artifact or invalid identity → Gate fails
10. **Immutable operator revisions**: Changes create child revisions with parent-child inheritance

### Module Structure

```
workflow/
├── schemas/
│   └── context_v2.py              # Context Schema v2
├── artifacts/
│   ├── artifact_schema.py         # 7 Artifact types
│   └── registry.py                # ArtifactRegistry
├── gates/
│   └── reducer.py                 # GateReducer (accuracy/v3/v4)
├── engine/
│   ├── workflow_engine.py         # 15-step deterministic workflow
│   ├── recovery.py                # RecoveryManager
│   ├── operator_revision_store.py # OperatorRevisionStore
│   └── verification_executor.py   # VerificationExperimentExecutor
├── agent/
│   ├── protocol.py                # AnalysisAgent interface
│   ├── policy_validator.py        # 3-layer validation
│   ├── session_manager.py         # AgentSessionManager
│   ├── claude_code_agent.py       # ClaudeCodeAnalysisAgent
│   └── claude_code_adapter.py     # API/CLI adapter
└── domain/
    ├── admission.py               # Plugin-only admission
    ├── v3_startup.py              # V3 discovery startup
    ├── v3_startup_tuning.py       # V3 startup compatibility tuning
    ├── v3_accuracy.py             # V3 accuracy evaluation
    ├── v3_accuracy_tuning.py      # V3 accuracy operator tuning
    ├── v3_performance.py          # V3 performance measurement
    ├── v3_release.py              # V3 release manager
    ├── v4_reduction.py            # V4 operator reduction
    └── v4_release.py              # V4 release manager
```

### Test Coverage

- `workflow/tests/test_admission.py` - 10 tests ✅
- `workflow/tests/test_v3_startup.py` - 10 tests ✅
- `workflow/tests/test_artifact_registry.py` - 6 tests ✅
- `workflow/tests/test_operator_revision.py` - 7 tests ✅
- `workflow/tests/test_gates.py` - 5 tests ✅
- **Total: 38 tests, 38 passing (100%)**

---

## Version Definitions

| Version | Definition | Image Tag | Delivery Repository |
|---------|-----------|-----------|---------------------|
| **V3 (Max)** | Full components (vllm + flaggems + flagtree + vllm-plugin-FL), accuracy qualified (relative_drop ≤ 5% per dataset), performance measured (no Gate) | `-v3` | `harbor.baai.ac.cn/flagrelease-project` (SVT delivery) |
| **V4 (Flag-express)** | V3 with reduced operators for performance optimization, must outperform V3 + accuracy qualified + ≥1 operator | `-v4` | `harbor.baai.ac.cn/flagrelease-project` |

**Fallback**: V4 not established → V3 remains final delivery version (no independent V4 release)

---

## Key Invariants

### Admission
- All 4 components required, fail-closed if any missing
- Runtime fixed: `VLLM_PLUGINS=fl` and `USE_FLAGGEMS=1`
- No Native fallback, no V1 execution

### Accuracy
- External NV reference (`nv_baseline.yaml`) is sole business red line
- Per-dataset evaluation (each dataset independently qualified)
- Relative drop = (NV_reference - accuracy) / NV_reference ≤ 5%
- Missing NV reference → fail-closed

### Performance
- V3 performance is measurement-only (no comparison, no Gate)
- V4 baseline is V3 (not V1)
- V4 success criteria: throughput > V3 + ≥1 operator + accuracy qualified

### Operator Revisions
- Immutable: changes create child revisions
- Parent-child inheritance with cumulative disable tracking
- Discovery → startup-tuning → accuracy-tuning → v3-final → v4-reduction → v4-final
- Each revision tracks: `enabled_ops`, `disabled_ops`, `disable_reason_categories`

### Agent Integration
- Agent provides hypotheses via bounded prompts
- Three-layer validation: Schema → Identity → Policy
- Experimental child revision created for verification
- Only measured Artifact from verification can advance state
- Negative evidence recorded to avoid re-attempting failed experiments

---

## Next Actions

1. **Step 12 (Test Coverage)**: Expand test suite to cover state transitions, recovery, Gates, and revision chains
2. **Step 13 (Remove Legacy)**: Clean up dual pipeline code, V1/V2 logic, old operator_search.py
3. **Integration Testing**: End-to-end workflow validation with mock containers
4. **Documentation**: Update CLAUDE.md to reference new Plugin-only workflow
5. **Step 14 (LangGraph)**: Independent future phase, doesn't block delivery

---

## References

- Design doc: `docs/plugin_only_workflow_optimization.md`
- Implementation plan: `docs/plugin_only_workflow_refactor_plan.md`
- Branch: `workflow-refactor`
