# -*- coding: utf-8 -*-
"""pool_profile_b1.py —— 《事件池标准画像报告》B1 版(39.3万事件,15日路径)。
与口袋支点版同口径:第零层样本框/第一层联合分布+右尾+horizon/第二层时间结构/第三层条件期望。
EDA=2018–2023;2024+ 物理隔离(只报n);2010–17 二级隔离(只报n)。
用法: python pool_profile_b1.py   # → out/pool_profile_b1.md
"""
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
D = 15

p = pd.read_parquet(os.path.join(OUT, "b1_event_paths.parquet"))
# 透视:每事件 offset×(hi, lo, ret_close)
pv = (p.set_index(["sym", "ep_date", "offset"])[["hi", "lo", "ret_close", "board"]]
      .unstack("offset"))
hi = pv["hi"].values[:, :D]                     # 相对入场价
lo = pv["lo"].values[:, :D]
rc = pv["ret_close"].values[:, :D]
board = pv[("board", 1)].values
sym = pv.index.get_level_values(0)
dt = pd.to_datetime(pv.index.get_level_values(1))
year = dt.year.values
full = ~np.isnan(rc[:, D - 1])
hi, lo, rc, board, sym, year = hi[full], lo[full], rc[full], board[full], sym[full], year[full]

S = ["# 事件池标准画像报告(B1,39.3万事件,15日路径)\n",
    "> EDA段 2018–2023;2024+ 物理隔离(只报n);2010–2017 二级隔离(只报n)。\n"]

# ========== 第零层 ==========
S.append("\n## 第零层 样本框审计(全样本)\n")
S.append("| 年 | n | 唯一股票 | 日均信号 |")
S.append("|---|---|---|---|")
m = pd.DataFrame({"sym": sym, "date": dt[full].values, "year": year, "fwd10": rc[:, 9]})
for y, g in m.groupby("year"):
    S.append("| %d | %d | %d | %.1f |" % (y, len(g), g.sym.nunique(), len(g) / g.date.nunique()))
per = m.groupby("sym").size()
S.append("\n每股触发:均值 %.1f | P50 %d P90 %d;只1次占 %.0f%%" % (
    per.mean(), per.median(), per.quantile(.9), 100 * (per == 1).mean()))
sd = m.groupby("date").size()
S.append("同日信号:均值 %.0f | P50 %d P90 %d 最大 %d" % (sd.mean(), sd.median(), sd.quantile(.9), sd.max()))
S.append("板块:" + " / ".join("%s %.0f%%" % (k, 100 * v) for k, v in
                            pd.Series(board).value_counts(normalize=True).items()))
mm = m.dropna(subset=["fwd10"])
dm = mm.groupby("date")["fwd10"].agg(["mean", "var", "count"])
mbar = dm["count"].mean()
vb = dm["mean"].var() - (dm["var"] * (dm["count"] - 1)).sum() / max((dm["count"] - 1).sum(), 1) / mbar
icc = max(vb, 0) / mm.fwd10.var()
S.append("**同日 ICC=%.3f,日均 m=%.0f → Neff ≈ %d(名义 %d)**\n" % (
    icc, mbar, len(mm) / (1 + (mbar - 1) * icc), len(mm)))

# ========== EDA 段 ==========
e = (year >= 2018) & (year <= 2023)
S.append("EDA 段 n=%d | 2024+ n=%d | 2010–17 n=%d\n" % (e.sum(), (year >= 2024).sum(), (year <= 2017).sum()))
He, Le, Ce = hi[e], lo[e], rc[e]

# ========== 第一层 ==========
mfe = np.nanmax(He, axis=1)          # 本表 hi/lo/ret_close 即收益率口径,勿再减1
mae = np.nanmin(Le, axis=1)
f15 = Ce[:, D - 1]
S.append("\n## 第一层 联合分布(15日窗,EDA段)\n")
S.append("| MFE\\MAE | ≥−5% | (−10,−5) | ≤−10 |")
S.append("|---|---|---|---|")
for lab, msk in [("MFE≥10%", mfe >= .10), ("MFE 3~10%", (mfe >= .03) & (mfe < .10)), ("MFE<3%", mfe < .03)]:
    cells = ["%.1f%%" % (100 * (msk & a).mean()) for a in (mae >= -.05, (mae > -.10) & (mae < -.05), mae <= -.10)]
    S.append("| %s | %s |" % (lab, " | ".join(cells)))
r = pd.Series(f15)
srt = r.sort_values()
n1, n5 = len(srt) // 100, len(srt) // 20
S.append("\n右尾分解(fwd15):全均值 %+.2f%% | 去Top1%% %+.2f%% | 去Top5%% %+.2f%% | >30%%单笔 %.1f%% | >50%% %.1f%%" % (
    100 * r.mean(), 100 * srt.iloc[:-n1].mean(), 100 * srt.iloc[:-n5].mean(),
    100 * (r > .30).mean(), 100 * (r > .50).mean()))
S.append("\n| 日 | 均值 | 中位 | 胜率 |")
S.append("|---|---|---|---|")
for d in range(D):
    x = Ce[:, d]
    S.append("| D+%d | %+.2f%% | %+.2f%% | %.0f%% |" % (d + 1, 100 * np.nanmean(x), 100 * np.nanmedian(x), 100 * np.nanmean(x > 0)))

# ========== 第二层 ==========
S.append("\n## 第二层 时间结构\n")


def first_day(mat, thr):
    hit = mat >= thr
    return np.where(hit.any(axis=1), hit.argmax(axis=1) + 1, np.nan), hit.any(axis=1)


for thr in (0.03, 0.05, 0.10):
    f_, any_ = first_day(He, thr)
    S.append("首次摸+%d%%:%.0f%% 摸到;中位第 %.0f 天(P25 %d P75 %d)" % (
        round(thr * 100), 100 * any_.mean(), np.nanmedian(f_),
        np.nanpercentile(f_[any_], 25), np.nanpercentile(f_[any_], 75)))
amd = np.nanargmin(Le, axis=1) + 1
S.append("最深回撤日:中位第 %d 天(P25 %d P75 %d)" % (np.median(amd), np.percentile(amd, 25), np.percentile(amd, 75)))
f5, t5 = first_day(He, 0.05)
win = f15 > 0
S.append("先后(摸+5%%日 vs 最深回撤日):赢家先摸高 %.0f%% | 输家先摸高 %.0f%%" % (
    100 * np.nanmean(f5[win] < amd[win]), 100 * np.nanmean(f5[~win] < amd[~win])))
pb = np.full(len(Ce), np.nan)
for i in np.nonzero(t5)[0]:
    d0 = int(f5[i]) - 1
    seg_ = 1 + He[i, d0:]
    pk = np.maximum.accumulate(seg_)
    pb[i] = (seg_ / pk - 1).min()
S.append("摸+5%%后(n=%d):赢家随后回撤中位 %+.1f%% | 输家 %+.1f%%;赢家回撤≥−8%%占 %.0f%%" % (
    t5.sum(), 100 * np.nanmedian(pb[t5 & win]), 100 * np.nanmedian(pb[t5 & ~win]),
    100 * np.nanmean(pb[t5 & win] <= -0.08)))

# ========== 第三层 ==========
S.append("\n## 第三层 条件期望表(状态=第d日收盘浮盈;后续=当日收盘→D+15)\n")
for d in (1, 2, 3, 5, 8):
    fut = (1 + Ce[:, D - 1]) / (1 + Ce[:, d - 1]) - 1
    st = pd.cut(Ce[:, d - 1], [-9, -0.05, 0, 0.05, 9], labels=["<−5%", "−5~0", "0~+5%", ">+5%"])
    g = (pd.DataFrame({"st": st, "fut": fut}).groupby("st", observed=True)
         .agg(n=("fut", "size"), 后续均值=("fut", lambda x: 100 * x.mean()),
              后续中位=("fut", lambda x: 100 * x.median()), 后续胜率=("fut", lambda x: 100 * (x > 0).mean())))
    S.append("\n**第 %d 日**\n\n%s" % (d, g.to_string()))

with open(os.path.join(OUT, "pool_profile_b1.md"), "w", encoding="utf-8") as f:
    f.write("\n".join(S))
print("\n".join(S))
print("\n→ out/pool_profile_b1.md")
