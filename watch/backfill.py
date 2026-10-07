# -*- coding: utf-8 -*-
"""backfill.py —— 触发事件回填:archive 事件 join 最新行情 → 前向结局(研究口径)。
口径:一律现价(close/factor)从 indicators parquet 现算,事件表 close 仅作展示对照;
不读 mav/dist_ma 列,天然免疫新旧均线列名混存。fwd5/10/20 = 触发日后第N交易日收盘/
触发日收盘-1(不足N日 None);MFE/MAE = 触发次日至最新期间 high.max()/low.min() 相对
触发日收盘;elapsed=已过交易日数,恰为5/10/20 时 milestone 标记(HTML高亮"满N日")。
用法: python backfill.py        # 直接打印最近20交易日事件的验证表
"""
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
IND = os.path.join(os.path.dirname(HERE), "stockdata", "indicators")
ARCHIVE_F = os.path.join(HERE, "archive.parquet")
HORIZONS = (5, 10, 20)


def forward_stats(archive_f=ARCHIVE_F, ind_dir=IND, lookback=20, horizons=HORIZONS):
    """archive 最近 lookback 个交易日事件 → 前向结局 DataFrame(空表=无归档/无文件)。"""
    if not os.path.exists(archive_f):
        return pd.DataFrame()
    arc = pd.read_parquet(archive_f)
    if arc.empty:
        return pd.DataFrame()
    arc["date"] = pd.to_datetime(arc["date"])
    keep = sorted(arc["date"].unique())[-lookback:]
    rows = []
    for _, e in arc[arc["date"].isin(keep)].iterrows():
        p = os.path.join(ind_dir, "%s.parquet" % e["code"])
        if not os.path.exists(p):
            continue
        try:
            df = pd.read_parquet(p, columns=["close", "high", "low", "factor"])
            df.index = pd.DatetimeIndex(df.index)
            ti = df.index.get_indexer([e["date"]])[0]
            if ti < 0:                                        # 触发日不在该票行情里(数据残缺)
                continue
            f = df["factor"]
            px, hi, lo = df["close"] / f, df["high"] / f, df["low"] / f   # 现价口径
            base = float(px.iloc[ti])
            n = len(px) - 1 - ti                              # 已过交易日数
            row = {"code": e["code"], "name": e.get("name", ""), "rule": e["rule"],
                   "date": str(e["date"].date()), "trig_close": round(base, 2),
                   "elapsed": n, "ret_now": None, "mfe": None, "mae": None,
                   "milestone": n if n in horizons else None}
            for h in horizons:
                row["fwd%d" % h] = None if n < h else round(float(px.iloc[ti + h] / base - 1), 4)
            if n > 0:
                row["ret_now"] = round(float(px.iloc[-1] / base - 1), 4)
                row["mfe"] = round(float(hi.iloc[ti + 1:].max() / base - 1), 4)
                row["mae"] = round(float(lo.iloc[ti + 1:].min() / base - 1), 4)
            rows.append(row)
        except Exception:
            continue                                          # 单票数据异常不阻断整表
    cols = ["code", "name", "rule", "date", "trig_close", "elapsed",
            "ret_now", "fwd5", "fwd10", "fwd20", "mfe", "mae", "milestone"]
    out = pd.DataFrame(rows, columns=cols)
    for c in ["ret_now", "fwd5", "fwd10", "fwd20", "mfe", "mae", "milestone"]:
        out[c] = out[c].astype(object).where(out[c].notna(), None)  # 混有数值时pandas会凝成float64 NaN,还原None(不足期口径)
    return out.sort_values(["date", "code"]).reset_index(drop=True)


if __name__ == "__main__":
    st = forward_stats()
    if st.empty:
        print("(无归档事件)")
    else:
        with pd.option_context("display.width", 200, "display.max_columns", None):
            print(st.to_string(index=False))
