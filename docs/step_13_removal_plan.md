# Step 13: Remove Legacy Code - Execution Plan

**Date**: 2026-09-02  
**Branch**: `workflow-refactor`  
**Goal**: Remove dual pipeline logic (A/B branches, gems_tree/gems_tree_plugin, native)

---

## 1. Identified Legacy Components

### 1.1 Environment Classification Logic

**File**: `skills/flagos-pre-service-inspection/tools/inspect_env.py`

**Lines to Remove/Modify**:
- Line 400-441: `classify_entry_image_type()` function
  - Returns: `gems_tree` (Branch A), `gems_tree_plugin` (Branch B), `native`
  - Should be replaced with Plugin-only admission logic

**Current Logic**:
```python
if not has_flaggems:
    entry_type = "native"  # 无 FlagGems
elif has_plugin:
    entry_type = "gems_tree_plugin"  # Branch B
else:
    entry_type = "gems_tree"  # Branch A
```

**New Logic** (Plugin-only):
```python
# All 4 components required: vllm + flaggems + flagtree + vllm-plugin-FL
if not (has_vllm and has_flaggems and has_flagtree and has_plugin):
    return {"accepted": False, "rejection_reasons": [...]}
return {"accepted": True, "profile": "plugin_only"}
```

### 1.2 Pipeline Branch Routing

**File**: `prompts/run_pipeline.sh`

**Lines to Remove**:
- Line 1275-1276: Branch A/B comments
- Line 1291-1293: `PIPELINE_BRANCH` assignment based on `SEG_ENV`
- Line 1301: Setting `workflow.pipeline_branch` in context
- Line 1311-1327: Branch-specific directives (A/B/native)

**Current Logic**:
```bash
case "${SEG_ENV}" in
    gems_tree)         PIPELINE_BRANCH="A" ;;
    gems_tree_plugin)  PIPELINE_BRANCH="B" ;;
    native)            PIPELINE_BRANCH="native" ;;
esac
```

**New Logic**: No branching, single Plugin-only workflow

### 1.3 V1 Baseline Execution

**Files**:
- `prompts/run_pipeline.sh` - V1 baseline evaluation logic
- Related scripts in `scripts/` for V1 execution

**Logic to Remove**:
- V1 (native baseline) execution
- V1 accuracy evaluation
- V1 performance measurement
- V1 vs V2 comparison logic

**Rationale**: Plugin-only workflow has no local V1. External NV reference is the only baseline.

### 1.4 V2 Code Injection Mode

**Files**:
- Operator injection scripts
- V2.1/V2.2 routing logic
- Code modification logic for operator replacement

**Logic to Remove**:
- V2 operator code injection
- V2.1 (with performance tuning) vs V2.2 (accuracy only) routing
- Dynamic code patching logic

**Rationale**: Plugin-only workflow uses vllm-plugin-FL, no code injection needed.

### 1.5 Old Plugin附加流程 (Steps 9-13)

**Context**: Old workflow had separate "Plugin附加" steps after V2
- Step 9: Plugin preparation
- Step 10: Plugin全量启动
- Step 11: Plugin精度评测
- Step 12: Plugin性能评测
- Step 13: Plugin发布

**New Workflow**: These are integrated into V3 (Plugin is the foundation, not附加)

### 1.6 Legacy Operator Search/Reduction

**Files**:
- `skills/flagos-operator-replacement/tools/operator_search.py` (old version)
- `skills/flagos-operator-replacement/tools/operator_reduction.py` (old version)

**New Implementation**:
- `workflow/domain/v3_startup_tuning.py` - Startup compatibility tuning
- `workflow/domain/v3_accuracy_tuning.py` - Accuracy operator tuning
- `workflow/domain/v4_reduction.py` - V4 two-phase optimization

**Action**: Archive old files, keep new workflow modules

---

## 2. Deletion Strategy

### Phase 1: Safe Archive (Before Deletion)
1. Create archive branch: `git branch archive/legacy-dual-pipeline workflow-refactor`
2. Tag current state: `git tag legacy-code-before-cleanup`

### Phase 2: Remove Classification Logic
1. Update `inspect_env.py`:
   - Remove `classify_entry_image_type()`
   - Replace with Plugin-only admission call to `workflow.domain.admission`
2. Update tests

### Phase 3: Remove Pipeline Branching
1. Update `run_pipeline.sh`:
   - Remove `PIPELINE_BRANCH` logic
   - Remove branch-specific directives
   - Simplify to single Plugin-only path
2. Remove V1/V2 execution blocks

### Phase 4: Remove V1 Baseline Logic
1. Remove V1 evaluation commands
2. Remove V1 accuracy/performance scripts
3. Remove V1 vs V2 comparison logic

### Phase 5: Remove V2 Injection Logic
1. Remove operator code injection scripts
2. Remove V2.1/V2.2 routing
3. Remove dynamic patching logic

### Phase 6: Archive Old Operator Tools
1. Move old `operator_search.py` to `archive/`
2. Move old `operator_reduction.py` to `archive/`
3. Keep new workflow modules active

### Phase 7: Update CLAUDE.md
1. Remove Branch A/B descriptions
2. Document Plugin-only workflow
3. Reference new workflow modules

---

## 3. Risk Mitigation

### Risks:
1. **Breaking existing users**: Some users may still be on gems_tree/native images
2. **Loss of fallback path**: No native fallback if Plugin fails
3. **Testing gap**: Cannot test without actual Plugin-enabled container

### Mitigation:
1. **Gradual rollout**: Keep legacy code in archive branch for 1-2 releases
2. **Clear documentation**: Migration guide for gems_tree → plugin_only
3. **Feature flag**: Optional `FLAGOS_WORKFLOW_PROFILE=plugin_only` to enable new workflow
4. **Monitoring**: Track adoption rate before full removal

---

## 4. Testing Strategy

### Before Deletion:
- ✅ All 38 unit tests passing
- ✅ Core components validated

### After Deletion:
- Run existing 38 tests (should still pass)
- Manual smoke test with mock environment
- Verify no references to removed logic

---

## 5. Execution Steps (Recommended Order)

**Step 13a: Preparation**
```bash
# Create safety net
git branch archive/legacy-dual-pipeline workflow-refactor
git tag legacy-code-before-cleanup
```

**Step 13b: Feature Flag (Recommended First)**
Add migration switch to allow coexistence:
```bash
if [ "${FLAGOS_WORKFLOW_PROFILE}" = "plugin_only" ]; then
    # Use new workflow
    python3 workflow/cli.py
else
    # Use legacy workflow (to be deprecated)
    # ... existing logic ...
fi
```

**Step 13c: Gradual Removal**
1. Remove classification logic → replace with Plugin-only admission
2. Remove pipeline branching → single path
3. Remove V1 execution → external NV reference only
4. Remove V2 injection → Plugin-based only
5. Archive old operator tools

**Step 13d: Documentation**
1. Update CLAUDE.md
2. Add migration guide
3. Document deprecation timeline

---

## 6. Completion Criteria

- [ ] No references to `gems_tree`, `gems_tree_plugin`, `native` in active code
- [ ] No references to `PIPELINE_BRANCH`, `Branch A`, `Branch B`
- [ ] No V1 baseline execution code
- [ ] No V2 code injection logic
- [ ] Old operator tools archived
- [ ] All 38 tests still passing
- [ ] CLAUDE.md updated
- [ ] Migration guide written

---

## 7. Timeline

**Phase 1** (Safety): 10 min - Archive and tag  
**Phase 2** (Classification): 30 min - Update inspect_env.py  
**Phase 3** (Branching): 1 hour - Simplify run_pipeline.sh  
**Phase 4-5** (V1/V2): 1 hour - Remove baseline and injection logic  
**Phase 6** (Archive): 15 min - Move old operator tools  
**Phase 7** (Docs): 30 min - Update CLAUDE.md

**Total Estimated Time**: ~3.5 hours

---

## 8. Notes

- **Conservative Approach**: Consider feature flag first (Step 13b)
- **Breaking Change**: This is a major refactor, requires careful testing
- **Rollback Plan**: Archive branch provides safety net
- **User Impact**: Existing gems_tree/native users will need to migrate

**Recommendation**: Implement feature flag first, then gradual removal over 2-3 releases.
