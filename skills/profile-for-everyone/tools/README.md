# Probe & Compare Dashboard

对两个 Perfetto trace（Gems vs Vendor）自动 probe + compare，生成带内嵌 Perfetto Timeline 的交互式 dashboard。

## 目录结构

```
program/
├── probe_and_compare.py          # 主入口
├── clean_compare_and_generate_dashboard.py  # compare + HTML 生成
├── requirements.txt
├── README.md
└── probe/
    ├── clear_probe.py            # trace 解析核心
    ├── probe_trace.py            # probe 主逻辑
    ├── render_trace_tree.py      # HTML 树渲染
    ├── schema.json               # 语义 schema
    ├── platform.json             # 平台配置
    └── trace_processor_shell     # Perfetto trace_processor 二进制 (arm64 macOS)
```

## 安装依赖

```bash
pip install -r requirements.txt
```

## 运行

```bash
python probe_and_compare.py \
  --gems-trace /path/to/gems.pt.trace.json.gz \
  --vendor-trace /path/to/vendor.pt.trace.json.gz \
  --decode-idx 30 \
  --out-dir ./output
```

### 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--gems-trace` | (必填) | Gems 侧 trace 文件 |
| `--vendor-trace` | (必填) | Vendor 侧 trace 文件 |
| `--schema` | `probe/schema.json` | 语义 schema |
| `--platform` | `probe/platform.json` | 平台配置 |
| `--decode-idx` | 100 | 选取第 N 个 decode iteration |
| `--decode-boundary-mode` | phase | 边界检测模式 (phase/marker) |
| `--cpu-stack-tree-detail` | module_vllm | CPU 栈树详细度 |
| `--trace-processor-shell` | `probe/trace_processor_shell` | trace_processor 二进制路径 |
| `--out-dir` | `./output` | 输出目录 |
| `--clean-output` | false | 运行前清空输出目录 |
| `--override` | (空) | 手动模块名覆盖 JSON |
| `--include-comm-time` | false | 是否包含通信时间 |

## 输出

```
output/
├── compare_dashboard.html    # 主 dashboard（浏览器直接打开）
├── compare_nodes.csv
├── compare_templates.csv
├── compare_hotspots.csv
├── compare_unmatched.json
└── src/
    ├── gems/                 # Gems probe 产物
    └── vendor/               # Vendor probe 产物
```

打开 `compare_dashboard.html`，点击 Vendor/Gems tab 会自动加载 Perfetto Timeline。
