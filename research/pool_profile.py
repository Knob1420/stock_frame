# -*- coding: utf-8 -*-
"""pool_profile.py —— 《事件池标准画像报告》(用户 2026-09-20 六层框架,P1-P3):
第零层 样本框审计:逐年/重复触发/同日聚集/同日相关 ICC/Neff/板块/退市
第一层 联合分布:MFE×MAE 格子 / 右尾贡献分解 / D+1..D+20 全 horizon 矩阵
第二层 时间结构:Time-to-MFE / Time-to-MAE / 先后顺序 / 摸+5%后回踩深度
第三层 条件期望表:E[后续收益|第d天状态] d=1,2,3,5,8
纪律:EDA 段=2018–2023;2024+ 物理隔离(仅报n);2010–2017 二级隔离段(仅报n)。
用法: python pool_profile.py   # → out/pool_profile_report.md
"""
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
EDA = (2018, 2023)          # EDA 段
HOLD1 = (2024, 2099)        # 物理隔离
HOLD2 = (2010, 2017)        # 二级隔离

z = np.load(os.path.join(OUT, "pocket_paths.npz"), allow_pickle=True)
O, Hh, L, C, board = z["O"], z["H"], z["L"], z["C"], z["board"]
meta = pd.Series(z["meta"])
sym = meta.str.split("|").str[0]
dt = pd.to_datetime(meta.str.split("|").str[1])
year = dt.dt.year


def seg(name, mask):
    print(name, mask.sum())


# ============ 第零层 样本框审计(全样本口径——样本框本身不属于发现) ============
rows = []
m = pd.DataFrame({"sym": sym, "date": dt, "year": year, "fwd10": C[:, 9] - 1, "board": board})
m["board_name"] = np.where(board == 0, "主板", np.where(board == 1, "创科", "北交"))
S = []
S.append("# 事件池标准画像报告(口袋支点,路径库 pocket_paths)\n")
S.append("> EDA段 2018–2023;2024+ 物理隔离(只报n);2010–2017 二级隔离(只报n)。\n")
S.append("\n## 第零层 样本框审计\n")
S.append("| 年 | n | 唯一股票 | 日均信号 |")
S.append("|---|---|---|---|")
for y, g in m.groupby("year"):
    S.append("| %d | %d | %d | %.1f |" % (y, len(g), g.sym.nunique(), len(g) / g.date.nunique()))
per = m.groupby("sym").size()
S.append("\n每股触发:均值 %.1f 次 | P50 %d P90 %d 最大 %d;只触发1次的占 %.0f%%" % (
    per.mean(), per.median(), per.quantile(.9), per.max(), 100 * (per == 1).mean()))
sd = m.groupby("date").size()
S.append("同日信号数:均值 %.1f | P50 %d P90 %d 最大 %d;单日≥10个的占 %.0f%% 天" % (
    sd.mean(), sd.median(), sd.quantile(.9), sd.max(), 100 * (sd >= 10).mean()))
S.append("板块占比:" + " / ".join("%s %.0f%%" % (k, 100 * v) for k, v in m.board_name.value_counts(normalize=True).items()))
# 退市股占比(数据末早于 2026-08 的 sym)
import glob
lastd = {}
for p in glob.glob(os.path.join(os.path.dirname(HERE), "stockdata", "indicators", "*.parquet")):
    s = os.path.basename(p)[:-8]
    lastd[s] = pd.Timestamp(pd.read_parquet(p, columns=["close"]).index[-1])
dl = m.sym.map(lastd) < pd.Timestamp("2026-08-01")
S.append("退市/停更股占比:%.1f%%" % (100 * dl.mean()))
# 同日 ICC(单因素随机效应,变量=fwd10)
mm = m.dropna(subset=["fwd10"])
dm = mm.groupby("date")["fwd10"].agg(["mean", "var", "count"])
mbar = dm["count"].mean()
vb = dm["mean"].var() - (dm["var"] * (dm["count"] - 1)).sum() / max((dm["count"] - 1).sum(), 1) / mbar
icc = max(vb, 0) / mm.fwd10.var()
neff_v = len(mm) / (1 + (mbar - 1) * icc)
S.append("**同日 ICC=%.3f,日均 m=%.1f → Neff ≈ %d(名义 %d)**\n" % (icc, mbar, neff_v, len(mm)))

# ============ EDA 段遮罩 ============
e = ((year >= EDA[0]) & (year <= EDA[1])).values
S.append("EDA 段 n=%d | 隔离段 2024+ n=%d | 二级隔离 2010–17 n=%d\n" % (e.sum(), (year >= 2024).sum(), (year <= 2017).sum()))
Ce, He, Le = C[e], Hh[e], L[e]

# ============ 第一层 联合分布(EDA 段) ============
mfe = np.nanmax(He[:, :10], axis=1) - 1
mae = np.nanmin(Le[:, :10], axis=1) - 1
f10 = Ce[:, 9] - 1
S.append("\n## 第一层 联合分布(10日窗,EDA段)\n")
S.append("| MFE\\MAE | ≥−5% | (−10,−5) | ≤−10 |")
S.append("|---|---|---|---|")
for mlab, mmsk in [("MFE≥10%", mfe >= .10), ("MFE 3~10%", (mfe >= .03) & (mfe < .10)), ("MFE<3%", mfe < .03)]:
    cells = ["%.1f%%" % (100 * (mmsk & a).mean()) for a in (mae >= -.05, (mae > -.10) & (mae < -.05), mae <= -.10)]
    S.append("| %s | %s |" % (mlab, " | ".join(cells)))
r = pd.Series(f10)
srt = r.sort_values()
top1 = srt.iloc[-len(srt) // 100:]
top5 = srt.iloc[-len(srt) // 20:]
S.append("\n右尾分解:全均值 %+.2f%% | 去Top1%% %+.2f%% | 去Top5%% %+.2f%% | Top1%%贡献 %+.2fpp | >30%%单笔 %d 笔 | >50%% %d 笔" % (
    100 * r.mean(), 100 * srt.iloc[:-len(top1)].mean(), 100 * srt.iloc[:-len(top5)].mean(),
    100 * (top1.mean() - r.mean()) * len(top1) / len(r), (r > .30).sum(), (r > .50).sum()))
S.append("\n| 日 | 均值 | 中位 | 胜率 | P(>成本) |")
S.append("|---|---|---|---|---|")
for d in range(20):
    x = Ce[:, d] - 1
    S.append("| D+%d | %+.2f%% | %+.2f%% | %.0f%% | %.0f%% |" % (
        d + 1, 100 * np.nanmean(x), 100 * np.nanmedian(x), 100 * np.nanmean(x > 0), 100 * np.nanmean(x > 0)))

# ============ 第二层 时间结构(EDA 段) ============
S.append("\n## 第二层 时间结构\n")


def first_day(mat, thr):
    hit = mat >= thr
    any_ = hit.any(axis=1)
    first = np.where(any_, hit.argmax(axis=1) + 1, np.nan)
    return first, any_


for thr in (1.03, 1.05, 1.10):
    f_, any_ = first_day(He[:, :10], thr)
    S.append("首次摸+%d%%:%.0f%% 的信号10日内摸到;摸到者中位第 %.0f 天(P25 %d P75 %d)" % (
        round((thr - 1) * 100), 100 * any_.mean(), np.nanmedian(f_), np.nanpercentile(f_[any_], 25), np.nanpercentile(f_[any_], 75)))
argmae = np.nanargmin(Le[:, :10], axis=1) + 1
S.append("最深回撤发生日:中位第 %d 天(P25 %d P75 %d)" % (np.median(argmae), np.percentile(argmae, 25), np.percentile(argmae, 75)))
f5, t5 = first_day(He[:, :10], 1.05)
mae_day = argmae
win = f10 > 0
S.append("先后顺序(摸+5%%日 vs 最深回撤日):赢家先摸高 %.0f%% | 输家先摸高 %.0f%%" % (
    100 * np.nanmean(f5[win] < mae_day[win]), 100 * np.nanmean(f5[~win] < mae_day[~win])))
# 摸+5%后的最大回撤(从摸到日起的峰值回撤)
tm = t5
pb = np.full(len(Ce), np.nan)
for i in np.nonzero(tm)[0]:
    d0 = int(f5[i]) - 1
    seg_ = He[i, d0:10]
    peaks = np.maximum.accumulate(seg_)
    pb[i] = (seg_ / peaks - 1).min()
S.append("摸到+5%%后(n=%d):赢家随后最大回撤中位 %+.1f%% | 输家 %+.1f%%;赢家中回撤≥−8%%的占 %.0f%%" % (
    tm.sum(), 100 * np.nanmedian(pb[tm & win]), 100 * np.nanmedian(pb[tm & ~win]),
    100 * np.nanmean(pb[tm & win] <= -0.08)))

# ============ 第三层 条件期望表(EDA 段) ============
S.append("\n## 第三层 条件期望表(状态=第d日收盘浮盈亏;后续=当日收盘→D+15收盘)\n")
for d in (1, 2, 3, 5, 8):
    base = Ce[:, d - 1]
    fut = Ce[:, 14] / base - 1
    st = pd.cut(base - 1, [-9, -0.05, 0, 0.05, 9], labels=["<−5%", "−5~0", "0~+5%", ">+5%"])
    t = pd.DataFrame({"st": st, "fut": fut})
    g = t.groupby("st", observed=True).agg(n=("fut", "size"), 后续均值=("fut", lambda x: 100 * x.mean()),
                                           后续中位=("fut", lambda x: 100 * x.median()),
                                           后续胜率=("fut", lambda x: 100 * (x > 0).mean()))
    S.append("\n**第 %d 日:**\n\n%s" % (d, g.to_markdown() if hasattr(g, "to_markdown") else g.to_string()))

with open(os.path.join(OUT, "pool_profile_report.md"), "w", encoding="utf-8") as f:
    f.write("\n".join(S))
print("\n".join(S))
print("\n→ out/pool_profile_report.md")
