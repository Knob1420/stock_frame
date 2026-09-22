# -*- coding: utf-8 -*-
"""chart.py —— 盘后快报配图:个股 K线+量+KDJ / 大盘总览。matplotlib 静态 PNG。

配色取自已验证的参考调色板(dataviz):surface #fcfcfb / ink #0b0b0b / muted #898781 /
grid #e1e0d9;均线分类色 slot1-3 蓝/橙/紫。K 线遵循 A 股惯例红涨绿跌,
并以"涨空心/跌实心"的形状通道兜底红绿色弱。所有价格按现价口径(整列/factor末值)。
"""
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import font_manager
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter

# 中文字体(Noto Sans CJK SC,apt fonts-noto-cjk;缺失时退回默认并容忍方块)
for f in font_manager.fontManager.ttflist:
    if f.name == "Noto Sans CJK SC":
        plt.rcParams["font.family"] = ["Noto Sans CJK SC"]
        break
plt.rcParams["axes.unicode_minus"] = False

SURFACE, INK, MUTED, GRID, BASE = "#fcfcfb", "#0b0b0b", "#898781", "#e1e0d9", "#c3c2b7"
UP, DOWN = "#d03b3b", "#008300"          # A 股惯例:红涨(空心)/绿跌(实心)
MA_C = {20: "#2a78d6", 120: "#eb6834", 250: "#4a3aa7"}  # 蓝/橙/紫,固定顺序
BOLL_FILL = "#cde2fb"
PAD_R = 9                                # 右缘直标预留区(K线数)


def _style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(BASE)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=0.6, alpha=0.8)
    ax.set_axisbelow(True)


def _xlabels(ax, dates, n=6):
    step = max(1, len(dates) // n)
    ax.set_xticks(range(0, len(dates), step))
    ax.set_xticklabels([str(dates[i])[5:10] for i in range(0, len(dates), step)])


def _edge_labels(ax, items, x_right, min_px=12):
    """右缘直标(值+颜色),按像素间距防重叠(均线粘合时依次推开)。"""
    items = sorted([it for it in items if it[0] is not None and not pd.isna(it[0])])
    if not items:
        return
    px = [ax.transData.transform((0, y))[1] for y, _, _ in items]
    for i in range(1, len(px)):
        if px[i] - px[i - 1] < min_px:
            px[i] = px[i - 1] + min_px
    inv = ax.transData.inverted()
    for (y, label, col), p in zip(items, px):
        ax.text(x_right, inv.transform((0, p))[1], label, fontsize=8, color=col,
                va="center", ha="left")


def stock_chart(df, code, name, rules, close, out_path, days=120):
    """个股:K线+MA+BOLL / 量 / KDJ 三联图 → PNG(约120交易日,现价口径)。"""
    df = df.assign(ma250=df["close"].rolling(250, min_periods=250).mean())  # 年线滚动现算
    d = df.iloc[-days:].copy()
    f = df["factor"].iloc[-1]
    for c in ("open", "high", "low", "close", "ma20", "ma120", "ma250",
              "boll_upper", "boll_lower"):
        d[c] = d[c] / f
    up = d["close"] >= d["open"]
    x = range(len(d))
    dates = list(d.index)

    fig, (a1, a2, a3) = plt.subplots(
        3, 1, figsize=(9.5, 7.2), dpi=120, sharex=True,
        gridspec_kw={"height_ratios": [3.2, 1, 1.1], "hspace": 0.08},
        facecolor=SURFACE)
    _style(a1), _style(a2), _style(a3)

    # ── 主图:BOLL 通道 + K线 + 均线 ──
    a1.fill_between(x, d["boll_lower"], d["boll_upper"], color=BOLL_FILL, alpha=0.35,
                    linewidth=0, label="BOLL(20,2)")
    for i in x:                                       # 涨空心红/跌实心绿(形状兜底色弱)
        c = UP if up.iloc[i] else DOWN
        a1.plot([i, i], [d["low"].iloc[i], d["high"].iloc[i]], color=c, linewidth=0.8)
        body_low = min(d["open"].iloc[i], d["close"].iloc[i])
        a1.add_patch(Rectangle((i - 0.32, body_low), 0.64,
                               max(abs(d["close"].iloc[i] - d["open"].iloc[i]), 0.001),
                               facecolor="none" if up.iloc[i] else DOWN,
                               edgecolor=c, linewidth=0.9))
    for n, col in MA_C.items():
        a1.plot(x, d["ma%d" % n], color=col, linewidth=1.6, label="MA%d" % n)
    hi = max(d["high"].max(), d["boll_upper"].max())
    lo = min(d["low"].min(), d["boll_lower"].min())
    a1.set_ylim(lo * 0.985, hi * 1.015)               # BOLL 高点不裁边
    a1.set_xlim(-1, len(d) + PAD_R)                   # 右侧留白给均线直标
    a1.axvline(len(d) - 1, color=BASE, linewidth=0.8, linestyle="--")
    a1.text(len(d) - 1, a1.get_ylim()[1], " 数据日", fontsize=7, color=MUTED,
            va="top", ha="left")
    _edge_labels(a1, [(d["ma%d" % n].iloc[-1], "MA%d %.2f" % (n, d["ma%d" % n].iloc[-1]),
                       col) for n, col in MA_C.items()], len(d) - 0.6)
    a1.legend(loc="upper left", fontsize=8, frameon=False, ncol=4)
    a1.set_title("%s %s  现价 %.2f  触发[%s]" % (code, name, close, rules),
                 fontsize=12, color=INK, loc="left", pad=10)

    # ── 副图:成交量 + 20日量能均线 ──
    a2.bar(x, d["volume"], width=0.64,
           color=[UP if u else DOWN for u in up], alpha=0.65, linewidth=0)
    a2.plot(x, d["vma20"], color="#52514e", linewidth=1.3, label="VMA20")
    a2.set_xlim(-1, len(d) + PAD_R)
    _edge_labels(a2, [(d["vma20"].iloc[-1], "VMA20", "#52514e")], len(d) - 0.6)
    a2.yaxis.set_major_formatter(FuncFormatter(
        lambda v, _: ("%g亿" % (v / 1e8)) if abs(v) >= 1e8 else
        ("%g万" % (v / 1e4)) if abs(v) >= 1e4 else "%g" % v))

    # ── 副图:KDJ ──
    for k, col, lab in zip(("kdj_k", "kdj_d", "kdj_j"),
                           ("#2a78d6", "#eb6834", "#1baf7a"), ("K", "D", "J")):
        a3.plot(x, d[k], color=col, linewidth=1.4, label=lab)
    a3.set_xlim(-1, len(d) + PAD_R)
    _edge_labels(a3, [(d[k].iloc[-1], "%s %.1f" % (lab, d[k].iloc[-1]), col)
                      for k, col, lab in zip(("kdj_k", "kdj_d", "kdj_j"),
                                             ("#2a78d6", "#eb6834", "#1baf7a"),
                                             ("K", "D", "J"))], len(d) - 0.6)
    for y in (80, 20):
        a3.axhline(y, color=GRID, linewidth=0.8)
    a3.set_ylim(-15, 115)
    a3.legend(loc="upper left", fontsize=8, frameon=False, ncol=3)
    _xlabels(a3, dates)

    fig.savefig(out_path, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return out_path


def market_chart(bench_df, stats, out_path, days=250):
    """大盘总览:沪深300 走势+年线 / 当日宽度两条百分比堆叠条 → PNG。"""
    d = bench_df.assign(
        ma250=bench_df["close"].rolling(250, min_periods=250).mean()).iloc[-days:]
    x = range(len(d))
    dates = list(d.index)
    c, m = d["close"], d["ma250"]

    fig, (a1, a2) = plt.subplots(
        2, 1, figsize=(9.5, 5.6), dpi=120,
        gridspec_kw={"height_ratios": [2.6, 1], "hspace": 0.42}, facecolor=SURFACE)
    _style(a1), _style(a2)

    a1.plot(x, c, color="#2a78d6", linewidth=1.8, label="沪深300")
    a1.plot(x, m, color="#eb6834", linewidth=1.4, label="MA250(年线)")
    a1.fill_between(x, c, m, where=(c >= m), color=UP, alpha=0.08, linewidth=0)
    a1.fill_between(x, c, m, where=(c < m), color=DOWN, alpha=0.08, linewidth=0)
    a1.legend(loc="upper left", fontsize=9, frameon=False)
    a1.set_title("大盘总览  数据日 %s" % stats["date"], fontsize=12, color=INK,
                 loc="left", pad=10)
    sub = "沪深300 近5日%+.1f%% 近20日%+.1f%%" % (100 * stats["bench_d5"],
                                                 100 * stats["bench_d20"])
    if "bench_dist250" in stats:                      # 指数为归一化值,只展示相对涨跌
        sub += " 距年线%+.1f%%" % (100 * stats["bench_dist250"])
    a1.text(0, 1.02, sub, transform=a1.transAxes, fontsize=9, color="#52514e")
    _xlabels(a1, dates)

    # 当日宽度:两条 100% 堆叠条(2px 表面缝=白描边),直标数值;窄段不内嵌文字
    rows = [
        ("当日涨跌", stats["adv"], stats["flat"], stats["dec"]),
        ("站上年线", stats["above"], 0, stats["total"] - stats["above"]),
    ]
    a2.set_xlim(0, 1)
    a2.set_yticks(range(len(rows)))
    a2.set_yticklabels([r[0] for r in rows], fontsize=9, color=INK)
    a2.set_xticks([])
    a2.grid(False)
    for s in a2.spines.values():
        s.set_visible(False)
    for i, (label, v1, v2, v3) in enumerate(rows):
        tot = v1 + v2 + v3
        segs = [(v1, UP, "涨 %d" % v1 if i == 0 else "年线上 %d(%.0f%%)"
                 % (v1, 100.0 * v1 / tot)),
                (v2, MUTED, "平 %d" % v2 if i == 0 else None),
                (v3, DOWN, "跌 %d" % v3 if i == 0 else "年线下 %d(%.0f%%)"
                 % (v3, 100.0 * v3 / tot))]
        left = 0.0
        for v, col, lab in segs:
            if not v:
                continue
            w = v / tot
            a2.barh(i, w, left=left, height=0.52, color=col, alpha=0.75,
                    edgecolor=SURFACE, linewidth=2)
            if lab and w >= 0.10:                     # 过窄的段(如"平")不内嵌,避免溢出
                a2.text(left + w / 2, i, lab, ha="center", va="center",
                        fontsize=8.5, color=INK)
            left += w
    a2.invert_yaxis()

    fig.savefig(out_path, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return out_path
