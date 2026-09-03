# 报告数据契约修复 - 完成总结

**Date**: 2026-09-03  
**Status**: ✅ 核心实现完成，待端到端验证

---

## 问题背景

Plugin-only 工作流（V2/V3/V4）产出的评测结果文件无法被 `generate_report.py` 完整读取，导致报告缺失关键数据：
- 精度对比文件 `accuracy_compare_{candidate}.json`
- 算子配置文件 `operator_config_{candidate}.json`

---

## 解决方案

采用**混合方案**（方案 C）：
1. **评测层**：Shell 编排层调用现有评测脚本（fast_gpqa.py, benchmark_runner.py）
2. **后处理层**：新增独立脚本 `generate_comparison_and_config.py` 生成报告所需的对比和配置文件
3. **集成层**：Shell 函数 `run_postprocessing()` 在每段评测完成后自动调用后处理
4. **报告层**：`generate_report.py` 增强 fallback 逻辑，兼容多种文件命名

### 架构图

```
Shell (run_pipeline.sh)
    ↓
评测脚本 (fast_gpqa.py) → 产出评测结果
    ↓
后处理脚本 (generate_comparison_and_config.py) → 产出对比和配置
    ↓
报告生成 (generate_report.py) → 读取所有文件生成完整报告
```

---

## 核心实现

### 1. 后处理脚本 (`workflow/cli/generate_comparison_and_config.py`)

**功能**：
- 读取评测结果文件 (`gpqa_{candidate}.json` 或 `{dataset}_flagos.json`)
- 读取 NV baseline (`nv_baseline.yaml`)
- 读取算子配置 (`context.yaml` 的 `optimization` 字段)
- 生成精度对比文件 (`accuracy_compare_{candidate}.json`)
- 生成算子配置文件 (`operator_config_{candidate}.json`)
- 返回退出码：0=达标，1=不达标

**特性**：
- 支持多种 NV baseline 格式（扁平/嵌套）
- 算子配置缺失时使用默认值（降级为警告）
- 详细的错误提示和日志输出

### 2. Shell 集成 (`prompts/run_pipeline.sh`)

**新增函数**：
```bash
run_postprocessing() {
    local ctr="$1"
    local candidate="$2"
    local datasets="${3:-gpqa_diamond}"
    
    # 对每个数据集独立生成对比和配置文件
    # 自动检查评测结果是否存在
    # 调用后处理脚本
}
```

**调用位置**：
- 段2完成后（V2 评测）：`run_postprocessing "${SEG_CTR}" "v2" "${DATASETS_CSV}"`
- 段4完成后（V3 评测）：`run_postprocessing "${SEG_CTR}" "v3" "${DATASETS_CSV}"`
- 所有调用都在 `regenerate_report` 之前执行

### 3. 工具部署 (`setup_workspace.sh`)

**新增部署**：
- 创建 `/flagos-workspace/workflow/cli/` 目录
- 部署后处理脚本到容器

---

## 测试结果

### 单元测试 ✅

运行 `python3 workflow/cli/test_generate_comparison.py`：

```
============================================================
  Test Summary
============================================================
  ✓ PASS: Qualified (accuracy close to NV)
  ✓ PASS: Not qualified (accuracy too low)
  ✓ PASS: Qualified (exact threshold)

  Total: 3/3 passed
============================================================
```

**测试覆盖**：
- ✅ 精度达标场景
- ✅ 精度不达标场景
- ✅ 临界值场景
- ✅ 文件格式验证
- ✅ 退出码验证

---

## 关键优势

### 1. 确定性执行
- **不依赖 Claude**：Shell 编排层自动调用，即使 Claude 漏做也能补齐
- **段末兜底**：每段完成时都会刷新数据

### 2. 最小改动
- **复用现有**：评测脚本（fast_gpqa.py）不变
- **增量增强**：只添加后处理层，不改变主流程

### 3. 健壮性
- **全程兜底**：所有调用都是 `|| true`，失败不阻塞主流程
- **多数据集支持**：自动处理逗号分隔的数据集列表
- **多格式兼容**：支持多种 baseline 和文件命名格式

### 4. 可维护性
- **独立测试**：后处理脚本可单独测试
- **清晰职责**：评测、后处理、报告生成三层分离
- **向前兼容**：为未来 LangGraph 迁移做好准备

---

## 文件变更总览

### 新增文件 (2)
1. `workflow/cli/generate_comparison_and_config.py` - 后处理脚本（287 行）
2. `workflow/cli/test_generate_comparison.py` - 测试脚本（222 行）

### 修改文件 (5)
1. `workflow/domain/v3_accuracy.py` - 真实评测实现
2. `workflow/domain/v3_performance.py` - 文件命名修正
3. `shared/generate_report.py` - Fallback 增强
4. `prompts/run_pipeline.sh` - Shell 集成（+44 行）
5. `skills/flagos-container-preparation/tools/setup_workspace.sh` - 脚本部署（+2 行）

### 文档文件 (5)
1. `docs/report_generation_data_contract_gap.md` - Gap 分析
2. `docs/report_data_contract_fix_complete.md` - 修复记录
3. `docs/complete_code_planning.md` - 代码规划
4. `docs/shell_integration_guide.md` - 集成指南
5. `docs/report_fix_progress.md` - 进度追踪

### Git 提交记录
```
62aa29b fix(pipeline): remove orphan fi statements from step13 refactoring
b7284c9 docs(report): update progress - Shell integration complete
c28cc9a feat(pipeline): integrate post-processing script into Shell orchestration
39354ba feat(report): add post-processing script for report data generation
```

---

## 待完成工作

### 端到端验证（估计 2-3 小时）

需要真实环境测试：
1. 准备测试容器和模型
2. 运行完整 V2 工作流
3. 验证文件产出：
   - `{dataset}_flagos.json`
   - `accuracy_compare_{dataset}.json`
   - `{candidate}_performance.json`
   - `operator_config_{candidate}.json`
4. 运行完整 V3 工作流
5. 验证报告生成
6. 测试多数据集场景（mmlu, math_500）

---

## 技术亮点

### 1. 智能数据集处理
```bash
# 自动处理逗号分隔的数据集列表
IFS=',' read -ra DS_ARRAY <<< "${datasets}"
for ds in "${DS_ARRAY[@]}"; do
    # 对每个数据集独立生成对比文件
done
```

### 2. 文件存在性检查
```bash
# 只处理已存在的评测结果
if ! docker exec "${ctr}" test -f "/flagos-workspace/results/${eval_result}" 2>/dev/null; then
    continue  # 跳过不存在的结果
fi
```

### 3. 多格式兼容
```python
# 支持两种 NV baseline 格式
if dataset in baseline_data:
    value = baseline_data[dataset]
    if isinstance(value, dict):
        return value.get('accuracy')
    return value

# 嵌套格式
datasets = baseline_data.get('datasets', {})
```

### 4. 退出码语义
```python
# 0=达标，1=不达标，供 Shell 判断
sys.exit(0 if qualified else 1)
```

---

## 经验教训

### 1. 用户反馈的价值
用户的关键反馈："你都没有完整规划完代码，怎么跑端到端测试"

**启发**：
- 不要急于执行，先完整规划
- 不仅修复域模型，还要规划调用路径
- 后处理脚本是连接 Shell 和域模型的关键桥梁

### 2. 架构选择
初始方案：直接在域模型中调用评测

**问题**：
- Shell 编排层不知道如何调用域模型
- 域模型和 Shell 之间缺少连接层

**最终方案**：
- 后处理脚本作为独立 CLI 工具
- Shell 可以直接通过 docker exec 调用
- 域模型为未来迁移保留

### 3. 测试驱动
先写测试再集成，发现问题早

**收益**：
- 单元测试全部通过再集成到 Shell
- 避免在真实环境中调试基础逻辑
- 测试脚本可作为使用示例

---

## 总结

✅ **核心实现已完成**：
- 后处理脚本实现并测试通过
- Shell 集成完成，自动调用
- 工具部署配置完成
- Shell 语法错误修复（orphan fi 清理）

⏳ **待端到端验证**：
- 需要真实容器和模型
- 验证完整工作流
- 测试多数据集场景

📈 **完成度**：90%（核心代码、集成、语法修复全部完成）

🎯 **关键成果**：
- 确保报告生成有完整数据
- 不依赖 Claude 的确定性执行
- 为 Plugin-only 工作流提供完整数据契约支持
- Shell 脚本语法验证通过，可安全执行
