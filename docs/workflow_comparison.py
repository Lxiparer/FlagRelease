#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""绘制「旧流程（半迁移态 V1/V2）」vs「新流程（Plugin-only V3/V4）」对比图。"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

# ---------- 中文字体 ----------
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
font_manager.fontManager.addfont(FONT)
_prop = font_manager.FontProperties(fname=FONT)
plt.rcParams["font.family"] = _prop.get_name()
plt.rcParams["axes.unicode_minus"] = False

# ---------- 配色 ----------
OLD_FC, OLD_EC = "#fdecea", "#d64545"      # 旧流程：暖红
NEW_FC, NEW_EC = "#e7f4ec", "#2e8b62"      # 新流程：冷绿
COM_FC, COM_EC = "#eef2f7", "#6b7c93"      # 公共步骤：中性灰
MID_FC, MID_EC = "#fff6e5", "#e0a94a"      # 差异徽标：琥珀
INK = "#222831"

fig, ax = plt.subplots(figsize=(16.5, 12.2))
ax.set_xlim(0, 100)
ax.set_ylim(0, 100)
ax.axis("off")

LX, RX, MX = 24, 76, 50          # 左列/右列/中列 x 中心
BW = 36                          # 盒宽
BH = 8.6                         # 盒高


def box(xc, yc, text, fc, ec, w=BW, h=BH, fs=11.2, bold=False, tc=INK):
    p = FancyBboxPatch((xc - w / 2, yc - h / 2), w, h,
                       boxstyle="round,pad=0.15,rounding_size=1.2",
                       linewidth=1.8, facecolor=fc, edgecolor=ec, zorder=2)
    ax.add_patch(p)
    ax.text(xc, yc, text, ha="center", va="center", color=tc,
            fontsize=fs, fontweight="bold" if bold else "normal",
            zorder=3, linespacing=1.45)


def badge(yc, text):
    w, h = 21, 5.4
    p = FancyBboxPatch((MX - w / 2, yc - h / 2), w, h,
                       boxstyle="round,pad=0.1,rounding_size=2.5",
                       linewidth=1.4, facecolor=MID_FC, edgecolor=MID_EC, zorder=2)
    ax.add_patch(p)
    ax.text(MX, yc, text, ha="center", va="center", color="#8a5a00",
            fontsize=10.3, fontweight="bold", zorder=3)


def arrow(xc, y_from, y_to):
    a = FancyArrowPatch((xc, y_from), (xc, y_to),
                        arrowstyle="-|>", mutation_scale=16,
                        linewidth=1.6, color="#8a97a8", zorder=1)
    ax.add_patch(a)


# ---------- 标题 ----------
ax.text(50, 97.5, "FlagOS 迁移流程改造对比", ha="center", va="center",
        fontsize=22, fontweight="bold", color=INK)
ax.text(50, 93.4, "旧流程（半迁移态：V1(native)+V2(flagos) 双轮 + Plugin）  →  "
                  "新流程（Plugin-only：V3 Primary + V4）",
        ha="center", va="center", fontsize=12.5, color="#5a6572")

# ---------- 列头 ----------
box(LX, 88, "旧流程  ·  V1 / V2 双轮", OLD_FC, OLD_EC, h=6.2, fs=14, bold=True, tc="#a3271f")
box(RX, 88, "新流程  ·  Plugin-only", NEW_FC, NEW_EC, h=6.2, fs=14, bold=True, tc="#1e6b48")

# ---------- 行内容 (阶段, 旧, 新, 差异徽标) ----------
rows = [
    ("准入 / 环境检测",
     "① 容器准备 + ② 环境检测\nnative 分支 + baseline 三选状态机\n(v1.1/v1.2/v1.3/none)",
     "① 容器准备 + ② 环境检测\n准入镜像分类；native 直接\nfail-closed 拒绝进入",
     "分支收敛"),
    ("启动服务",
     "③ 启服务\nV1(native) + V2(flagos)\n两个版本分别验证",
     "③ 启服务\nV3(plugin) 单版本",
     "双版本 → 单版本"),
    ("精度评测 + 调优",
     "④⑤ 精度评测 + 算子调优\nV1(native) vs V2(flagos)\n判据：本地 V1 基线",
     "④⑤ 精度评测 + 算子调优\nV3 vs 外部 NV 参考\nrel_drop≤5%，缺基线 fail-closed",
     "本地V1 → 外部NV"),
    ("性能评测 + 调优",
     "⑥⑦ 性能评测 + 算子调优\nV1 vs V2 比值 + step7 强制闸门\n(<80% 补跑；无V1时合成基线×1.05)",
     "⑥ 性能：仅测量、不设 Gate\n(性能不阻断流程，仅记录/报告)",
     "强制闸门 → 仅测量"),
    ("段3 发布",
     "⑧ 段3：发布 V2 Pro（对外）\n→ Harbor flagrelease-public\n(公开仓 + 传权重 + README)",
     "⑧ 段3：不对外发布\n(no-op，发布归并到段4)",
     "对外发布 → 归并段4"),
    ("Plugin 验证 / 交付",
     "⑨–⑬ Plugin 安装 + 评测\n+ V3 发布",
     "⑨–⑬ Plugin V3 交付\n→ Harbor flagrelease-project\n(私有，V3 = 唯一交付)",
     "V3 = 唯一交付"),
    ("V4 减算子",
     "⑬.5 V4 减算子提性能\n基线：V1 / 合成基线",
     "⑬.5 V4 减算子提性能\n基线：V3 自身实测性能\n(目标：超越 V3)",
     "合成基线 → 实测基线"),
]

y_top, y_bot = 80.5, 10.5
n = len(rows)
ys = [y_top - i * (y_top - y_bot) / (n - 1) for i in range(n)]

for i, (stage, old, new, diff) in enumerate(rows):
    yc = ys[i]
    # 阶段标签（最左侧）
    ax.text(2.5, yc, stage, ha="left", va="center", fontsize=10.2,
            color="#5a6572", fontweight="bold", rotation=0)
    box(LX, yc, old, OLD_FC, OLD_EC)
    box(RX, yc, new, NEW_FC, NEW_EC)
    badge(yc, diff)
    if i < n - 1:
        gap_mid = (ys[i] - ys[i + 1])
        arrow(LX, yc - BH / 2 - 0.2, ys[i + 1] + BH / 2 + 0.2)
        arrow(RX, yc - BH / 2 - 0.2, ys[i + 1] + BH / 2 + 0.2)

# ---------- 底部核心说明 ----------
foot = ("核心变化：精度唯一红线 = 与外部 NV 参考的相对退化 ≤5%（fail-closed）；"
        "性能全程不阻断，仅测量记录；V2 已并入 V3，V3 为唯一对外交付版本。\n"
        "同步删除：native 双轮、baseline 三选状态机、合成性能基线(×1.05)、step7 强制闸门、段3 对外发布分支。")
fig.text(0.5, 0.032, foot, ha="center", va="center", fontsize=10.6,
         color="#3a4450", linespacing=1.6,
         bbox=dict(boxstyle="round,pad=0.6", facecolor="#f4f6f9", edgecolor="#ccd4de"))

plt.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.075)
OUT = "/home/lz/workspace/flagos_workflow/FlagRelease/docs/workflow_comparison.png"
plt.savefig(OUT, dpi=170, facecolor="white")
print("saved:", OUT)
