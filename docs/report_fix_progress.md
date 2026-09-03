# 报告数据契约修复 - 当前进度

**Date**: 2026-09-03  
**Session**: 继续 Step 13  
**Status**: Phase 1-3 完成，待端到端验证

---

## 工作目标

确保 Plugin-only 工作流能产出 `generate_report.py` 期望的所有数据文件。

---

## 已完成工作 ✅

### 1. 问题分析和规划
- ✅ `docs/report_generation_data_contract_gap.md` - Gap 分析
- ✅ `docs/report_data_contract_fix_complete.md` - 初步修复记录
- ✅ `docs/complete_code_planning.md` - 完整代码规划

### 2. 域模型修复
- ✅ `workflow/domain/v3_accuracy.py`
  - 移除模拟实现（硬编码 65.2%）
  - 实现真实的 subprocess 调用 `fast_gpqa.py`
  - 添加 `_generate_comparison_file()` 方法
  - 添加 `_export_operator_config()` 方法
  
- ✅ `workflow/domain/v3_performance.py`
  - 修正文件命名：`flagos_optimized` → `v3_performance`
  
- ✅ `shared/generate_report.py`
  - 增强 V3 精度 fallback
  - 增强 V3 性能 fallback
  - 新增 V3/V4 对比文件 fallback

### 3. 后处理脚本
- ✅ `workflow/cli/generate_comparison_and_config.py`
  - 独立的 CLI 脚本
  - 读取评测结果 + NV baseline + context.yaml
  - 生成 `accuracy_compare_{candidate}.json`
  - 生成 `operator_config_{candidate}.json`
  - 完整的错误处理和日志

- ✅ `workflow/cli/test_generate_comparison.py`
  - 单元测试脚本
  - 创建模拟数据
  - 验证后处理脚本输出
  - 3个测试场景（达标/不达标/临界值）
  - **所有测试通过（3/3）**

### 4. Shell 集成
- ✅ `prompts/run_pipeline.sh`
  - 添加 `run_postprocessing()` 函数
  - 在段2（V2 评测）完成后调用后处理
  - 在段4（V3 评测）完成后调用后处理
  - 在 `regenerate_report` 之前执行，确保报告数据完整

- ✅ `skills/flagos-container-preparation/tools/setup_workspace.sh`
  - 添加后处理脚本到部署清单
  - 创建 `workflow/cli` 目录
  - 确保后处理脚本随其他工具一起部署

### 5. 文档
- ✅ `docs/shell_integration_guide.md` - Shell 集成指南
- ✅ `docs/report_fix_progress.md` - 本文档（进度追踪）

---

## 待完成工作 ⏳

### Phase 4: V4 完整实现（估计 2-3 小时）

5. **V4 reduction 集成**
   - 在 `workflow/domain/v4_reduction.py` 添加评测调用（如需）
   - V4 已通过 `operator_reduction.py` 自动调用评测和后处理
   - 验证 V4 产出文件命名正确

### Phase 5: 端到端验证（估计 2-3 小时）

6. **V2 完整流程测试**
   - 准备测试容器和模型
   - 运行 V2 工作流（discovery → startup → accuracy → performance）
   - 验证文件产出：
     - `gpqa_v2.json` 或 `{dataset}_flagos.json`
     - `accuracy_compare_v2.json` 或 `accuracy_compare_{dataset}.json`
     - `v2_performance.json` 或 `{candidate}_performance.json`
     - `operator_config_v2.json`

7. **V3 完整流程测试**
   - 运行 V3 工作流（plugin install → startup → accuracy → performance）
   - 验证文件产出（同上，v3 版本）

8. **报告生成验证**
   ```bash
   python3 shared/generate_report.py --output results/report.md
   ```
   - 检查 V2 数据完整性（精度、性能、算子数）
   - 检查 V3 数据完整性
   - 验证 fallback 逻辑生效

---

## 当前架构

### 数据流图

```
fast_gpqa.py (现有)
    ↓ 产出
gpqa_v2.json / {dataset}_flagos.json
    ↓ 被读取
generate_comparison_and_config.py (新增)
    ├─ 读取 gpqa_v2.json / {dataset}_flagos.json
    ├─ 读取 nv_baseline.yaml
    ├─ 读取 context.yaml (算子配置)
    ↓ 产出
    ├─ accuracy_compare_v2.json / accuracy_compare_{dataset}.json
    └─ operator_config_v2.json
    ↓ 被读取
generate_report.py (已增强 fallback)
    ↓ 产出
report.md (完整数据)
```

### 调用链（Shell 自动化）

```
run_pipeline.sh 段2
    ↓
docker exec fast_gpqa.py → {dataset}_flagos.json (通过 Claude 或兜底逻辑)
    ↓
[段2完成] run_postprocessing() 自动调用
    ↓
docker exec generate_comparison_and_config.py
    ↓ 产出
    ├─ accuracy_compare_{dataset}.json
    └─ operator_config_v2.json
    ↓
benchmark_runner.py → v2_performance.json
    ↓
regenerate_report() 读取所有文件生成报告
    ↓
report.md (完整数据)
```

**关键改进**：后处理现在由 Shell 编排层自动调用，不依赖 Claude 记得执行。

---

## 关键决策记录

### 为什么选择后处理脚本而非域模型直接调用？

**原因**：
1. **最小改动** - 不改变现有 Shell 编排逻辑
2. **复用现有** - fast_gpqa.py 和 benchmark_runner.py 已验证
3. **解耦** - 后处理脚本是独立工具，可单独测试
4. **向后兼容** - 即使后处理失败，评测结果仍然可用
5. **Shell 自动化** - 通过 run_postprocessing() 函数，Shell 自动在段末调用

### 为什么修改域模型但不直接使用？

**原因**：
1. **未来准备** - 域模型为未来的编排层迁移做准备
2. **测试价值** - 域模型可以被单元测试，提高代码质量
3. **部分复用** - 后处理脚本可以调用域模型的部分逻辑（未实现，但可选）

### Shell 集成方案

**实现方式**：
1. 在 `run_pipeline.sh` 中添加 `run_postprocessing()` 函数
2. 在段2（V2）和段4（V3）的 `regenerate_report` 之前调用
3. 对每个数据集独立调用后处理脚本
4. 全程 `|| true` 兜底，失败不阻塞主流程

**优势**：
- 确定性执行：不依赖 Claude 是否记得调用
- 段末兜底：即使 Claude 漏做，Shell 也会补齐
- 多数据集支持：自动处理逗号分隔的数据集列表

---

## 风险和缓解

### 风险1: 后处理脚本路径问题

**风险**: 脚本路径硬编码，部署时可能不存在

**缓解**:
- ✅ 脚本已加入 `setup_workspace.sh` 部署清单
- ✅ 使用绝对路径：`/flagos-workspace/workflow/cli/generate_comparison_and_config.py`
- ✅ Shell 函数中检查容器是否存在后才执行

### 风险2: NV baseline 文件格式变化

**风险**: nv_baseline.yaml 格式可能有多种变体

**缓解**:
- ✅ 后处理脚本支持两种格式：
  ```yaml
  # 格式1: 扁平
  gpqa_diamond: 66.8
  
  # 格式2: 嵌套
  datasets:
    gpqa_diamond:
      accuracy: 66.8
  ```
- ✅ 详细的错误提示

### 风险3: context.yaml 缺失算子配置

**风险**: 新容器可能没有 optimization 字段

**缓解**:
- ✅ 降级为警告而非错误
- ✅ 使用默认值填充
- ✅ 报告中仍显示"-"但不阻塞流程

---

## 测试策略

### 层级1: 单元测试（独立）✅
- ✅ `test_generate_comparison.py` - 后处理脚本逻辑
- ✅ 使用模拟数据，不依赖真实容器
- ✅ 快速验证（< 10秒）
- ✅ **所有测试通过（3/3）**

### 层级2: 集成测试（容器内）⏳
- 在真实容器中运行后处理脚本
- 使用真实的评测结果文件
- 验证与真实环境的兼容性

### 层级3: 端到端测试（完整流程）⏳
- 完整 V2/V3 工作流
- 验证所有文件产出
- 生成并检查报告

---

## 文件清单

### 新增文件
1. ✅ `workflow/cli/generate_comparison_and_config.py` - 后处理脚本
2. ✅ `workflow/cli/test_generate_comparison.py` - 测试脚本

### 修改文件
1. ✅ `workflow/domain/v3_accuracy.py` - 真实评测实现
2. ✅ `workflow/domain/v3_performance.py` - 文件命名修正
3. ✅ `shared/generate_report.py` - Fallback 增强
4. ✅ `prompts/run_pipeline.sh` - Shell 集成（添加 run_postprocessing 函数）
5. ✅ `skills/flagos-container-preparation/tools/setup_workspace.sh` - 脚本部署

### 待修改文件
- 无（V4 已通过 operator_reduction.py 自动处理）

### 文档文件
1. ✅ `docs/report_generation_data_contract_gap.md` - Gap 分析
2. ✅ `docs/report_data_contract_fix_complete.md` - 修复记录
3. ✅ `docs/complete_code_planning.md` - 代码规划
4. ✅ `docs/shell_integration_guide.md` - 集成指南
5. ✅ `docs/report_fix_progress.md` - 本文档

---

## 下一步行动

### 短期（本次会话或下次）
1. V2/V3 完整流程测试（需要真实容器和模型）
2. 端到端验证报告生成
3. 验证多数据集场景（mmlu, math_500）

### 中期（Step 13 完成后）
4. 完成 Step 13 其他收尾工作
5. 归档旧工具
6. 更新 CLAUDE.md

---

## 估计剩余工作量

- **V4 实现**: 0 小时（已通过 operator_reduction.py 处理）
- **端到端验证**: 2-3 小时
- **总计**: 2-3 小时

**当前完成度**: 约 85%（核心代码和集成已完成，待端到端验证）

---

## 总结

**已完成**：
- ✅ 完整的问题分析和规划
- ✅ 域模型修复（真实评测、正确命名）
- ✅ 后处理脚本实现
- ✅ 测试脚本编写（所有测试通过）
- ✅ 报告 fallback 增强
- ✅ Shell 集成（run_postprocessing 函数）
- ✅ 工具部署（setup_workspace.sh）
- ✅ 详细文档

**待完成**：
- ⏳ 端到端验证（需要真实环境）

**关键成果**：
- 后处理脚本由 Shell 自动调用，不依赖 Claude
- 段末确定性兜底，确保数据完整性
- 所有单元测试通过，逻辑验证完成

**恢复路径**：
1. 准备测试容器和模型
2. 运行完整 V2 工作流
3. 验证文件产出和报告生成
4. 运行完整 V3 工作流
5. 最终验证所有版本报告数据完整性
