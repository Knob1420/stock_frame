# -*- coding: utf-8 -*-
"""case_features.py —— Phase 2a:案例窗口特征提取 + 四象限自动合成。
用户只标 look(漂/普);成败由数据判(观察池口径:T+1 起 10 日内
max_up>=+8%=hit / min_dn<=-5%=miss / 同日双触发按 miss / 其余 flat)。
特征树(用户 2026-09-18 口述):价格/趋势/量能/结构/波动;板块本地无数据暂缺。
用法:
  python case_features.py                     # 读 cases/cases.csv → out/case_features.csv + 摘要
  python case_features.py --selfcheck         # 合成数据自检
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
IND_DIR = os.path.join(os.path.dirname(HERE), "stockdata", "indicators")
CASES_CSV = os.path.join(HERE, "cases", "cases.csv")
OUT_DIR = os.path.join(HERE, "out")

HIT, MISS = 0.08, -0.05


def outcome(df: pd.DataFrame, t: int) -> dict:
    """T+1 开盘入场,10 bar 内先到者定结局;同日双触发按 miss。"""
    if t + 11 > len(df) or np.isnan(df["open"].iloc[t + 1]):
        return {"entry": np.nan, "outcome": "no_data", "fwd10": np.nan}
    entry = float(df["open"].iloc[t + 1])
    res, fwd = "flat", np.nan
    for i in range(t + 1, min(t + 11, len(df))):
        up = df["high"].iloc[i] / entry - 1.0
        dn = df["low"].iloc[i] / entry - 1.0
        if dn <= MISS:
            res = "miss"                      # 同日双触发按 miss:先判 miss
            break
        if up >= HIT:
            res = "hit"
            break
    if t + 10 < len(df):
        fwd = float(df["close"].iloc[t + 10]) / entry - 1.0
    return {"entry": entry, "outcome": res, "fwd10": fwd}


def window_features(df: pd.DataFrame, t: int) -> dict:
    """T-19~T 窗口特征(结构类回看 60 bar)。缺失历史 → NaN,不猜。"""
    w = df.iloc[max(0, t - 19): t + 1]
    c = float(df["close"].iloc[t])
    f = {}

    # 价格
    f["ret20"] = c / df["close"].iloc[t - 20] - 1.0 if t >= 20 else np.nan
    hi60 = df["high"].iloc[max(0, t - 59): t + 1]
    f["dist_high60"] = c / hi60.max() - 1.0 if len(hi60) == 60 else np.nan
    for n in (20, 60, 120, 240):
        f["dist_ma%d" % n] = c / df["ma%d" % n].iloc[t] - 1.0

    # 趋势
    m5, m20, m60 = df["ma5"].iloc[t], df["ma20"].iloc[t], df["ma60"].iloc[t]
    f["ma_align"] = float(m5 > m20 > m60) if not (np.isnan(m60)) else np.nan
    for n in (20, 60):
        a, b = df["ma%d" % n].iloc[t], df["ma%d" % n].iloc[t - 5]
        f["slope_ma%d" % n] = a / b - 1.0 if t >= 5 and not np.isnan(b) else np.nan

    # 量能
    up = (w["close"] > w["open"]).values
    uv, dv = w["volume"].values[up], w["volume"].values[~up]
    f["upvol_ratio"] = uv.mean() / dv.mean() if len(uv) and len(dv) and dv.mean() > 0 else np.nan
    f["vol_ratio_T"] = df["volume"].iloc[t] / df["vma20"].iloc[t]
    m3 = df["volume"].rolling(3, min_periods=3).mean()
    f["shrink_ratio"] = (m3.iloc[max(0, t - 4): t + 1].min() / df["vma20"].iloc[t]
                         if not np.isnan(m3.iloc[t]) else np.nan)
    body = (w["close"] - w["open"]).abs().values
    bu, bd = body[up], body[~up]
    f["body_ratio"] = bu.mean() / bd.mean() if len(bu) and len(bd) and bd.mean() > 0 else np.nan

    # 结构:近 60 bar 最大量为爆量K(关键K)
    if t >= 59:
        v = df["volume"].iloc[t - 59: t + 1]
        bi = int(np.argmax(v.values))                    # 窗口内相对位置
        gi = t - 59 + bi                                  # 全局位置
        f["blast_mult"] = v.iloc[bi] / df["vma20"].iloc[gi]
        f["blast_exists"] = float(f["blast_mult"] >= 3.0)
        f["blast_days_ago"] = t - gi
        f["blast_broken"] = float(df["low"].iloc[gi: t + 1].min() < df["low"].iloc[gi])
        f["days_since_high60"] = t - (t - 59 + int(np.argmax(hi60.values)))
    else:
        for k in ("blast_mult", "blast_exists", "blast_days_ago", "blast_broken",
                  "days_since_high60"):
            f[k] = np.nan

    # 波动
    f["atr_pct"] = df["atr14"].iloc[t] / c
    f["amp_mean20"] = float(((w["high"] - w["low"]) / w["close"].shift(1)).mean())
    a0, a1 = df["atr14"].iloc[t], df["atr14"].iloc[t - 10]
    f["atr_chg"] = a0 / a1 - 1.0 if t >= 10 and not np.isnan(a1) else np.nan
    return f


def quadrant(look: str, outcome: str) -> str:
    """A=漂+hit B=漂+miss C=普+hit D=普+miss;flat/无效 → '?'"""
    if outcome not in ("hit", "miss"):
        return "?"
    return {"漂": "A" if outcome == "hit" else "B",
            "普": "C" if outcome == "hit" else "D"}.get(str(look).strip(), "?")


def run_cases() -> pd.DataFrame:
    cases = pd.read_csv(CASES_CSV, dtype={"sym": str}).dropna(subset=["sym"])
    rows = []
    for _, r in cases.iterrows():
        p = os.path.join(IND_DIR, "%s.parquet" % r["sym"].strip().lower())
        df = pd.read_parquet(p)
        df.index = pd.DatetimeIndex(df.index)
        d = pd.Timestamp(r["date"])
        if d not in df.index:
            print("跳过 %s %s:日期不在该股交易日" % (r["sym"], r["date"]))
            continue
        t = df.index.get_loc(d)
        row = {"sym": r["sym"], "date": d, "look": r["look"],
               "note": r.get("note", "")}
        row.update(outcome(df, t))
        row.update(window_features(df, t))
        row["quad"] = quadrant(row["look"], row["outcome"])
        rows.append(row)
    return pd.DataFrame(rows)


def _selfcheck():
    rng = np.random.default_rng(7)
    n = 300
    close = 10 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    df = pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.005, n)),
        "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": rng.uniform(1e6, 5e6, n)},
        index=pd.bdate_range("2024-01-01", periods=n))
    for n_ in (5, 20, 60, 120, 240):                    # 造 ma/vma/atr 列
        df["ma%d" % n_] = df["close"].rolling(n_, min_periods=1).mean()
    df["vma20"] = df["volume"].rolling(20, min_periods=1).mean()
    df["atr14"] = (df["high"] - df["low"]).rolling(14, min_periods=1).mean()
    f = window_features(df, 299)
    o = outcome(df, 299)
    assert f["dist_ma20"] == f["dist_ma20"] and f["vol_ratio_T"] > 0, f
    assert o["outcome"] in ("hit", "miss", "flat", "no_data"), o
    assert quadrant("漂", "hit") == "A" and quadrant("普", "miss") == "D"
    print("selfcheck ok:", {k: round(v, 4) for k, v in f.items() if isinstance(v, float)})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args(argv)
    if a.selfcheck:
        _selfcheck()
        return
    res = run_cases()
    if res.empty:
        print("cases.csv 无有效案例")
        return
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "case_features.csv")
    res.to_csv(out, index=False)
    print("→ %s(%d 例)\n" % (out, len(res)))
    show = res.set_index("sym")[["date", "look", "outcome", "quad", "fwd10", "ret20",
                                 "dist_high60", "dist_ma60", "upvol_ratio", "shrink_ratio",
                                 "body_ratio", "blast_exists", "blast_broken", "vol_ratio_T"]]
    pd.set_option("display.width", 200)
    print(show.to_string(float_format=lambda x: "%.3f" % x))


if __name__ == "__main__":
    main()
