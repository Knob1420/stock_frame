# -*- coding: utf-8 -*-
"""portfolio_sim.py —— 组合层模拟(用户参数:50万资金,最多 5 并发仓位)。
规则:门内 B1+ 事件,T+1 开盘买,持有 15 交易日收盘卖,成本 0.35% 双边;
开仓金额 = 当前权益/5;信号多于空位时按 sym 顺序取(无排名,任意确定性口径);
环境门关闭日无新事件(事件已按门过滤),存量持仓持有到期。
产出:逐日权益曲线 → 年化/最大回撤/仓位利用率;out/portfolio_equity.csv
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")

CAPITAL = 500_000
MAX_SLOTS = 5
COST = 0.0035


def load_positions():
    f = pd.read_parquet(os.path.join(OUT, "b1_event_features.parquet"))
    ms = pd.read_parquet(os.path.join(OUT, "market_state.parquet"))
    b1p = f[(f.dist_ma60 > 0.06) & (f.slope_ma20 > 0.02) & (f.keyk == 1) & (f.keyk_brk_low == 0)]
    b1p = b1p.join(ms[["above_ma60", "idx_slope20"]], on=pd.to_datetime(b1p.date))
    gate = b1p[(b1p.above_ma60 > 0.6) & (b1p.idx_slope20 > 0)][["sym", "date"]]
    p = pd.read_parquet(os.path.join(OUT, "b1_event_paths.parquet"))
    m = p.merge(gate, left_on=["sym", "ep_date"], right_on=["sym", "date"], how="inner")
    pos = {}          # (sym, ep) -> {date: ret_close},完整 15 日的才要
    entries = {}      # entry_date(offset1 的 date) -> [(sym, ep)](按 sym 排序)
    for (sym, ep), g in m.groupby(["sym", "ep_date"]):
        g = g.sort_values("offset")
        if int(g.offset.iloc[-1]) != 15:
            continue
        pos[(sym, ep)] = dict(zip(g.date_x, g.ret_close))
        d1 = g.date_x.iloc[0]
        entries.setdefault(d1, []).append((sym, ep))
    for d in entries:
        entries[d].sort()
    return pos, entries


def run():
    pos, entries = load_positions()
    cal = sorted({d for path in pos.values() for d in path})
    cash, equity = CAPITAL, CAPITAL
    open_pos = []     # [(sym, ep, slot, exit_date, path)]
    curve, taken = [], 0
    for d in cal:
        # 到期平仓
        still = []
        for (sym, ep, slot, xd, path) in open_pos:
            if d == xd:
                cash += slot * (1 + path[d]) * (1 - COST)
            else:
                still.append((sym, ep, slot, xd, path))
        open_pos = still
        # 开新仓(信号日为前一交易日,入场日=今日开盘);退出日=入场日起第15个交易日
        for key in entries.get(d, []):
            if len(open_pos) >= MAX_SLOTS:
                break
            slot = equity / MAX_SLOTS
            if cash < slot:
                break
            cash -= slot
            path = pos[key]
            xd = sorted(path)[14]                     # 第 15 个交易日收盘卖
            open_pos.append((key[0], key[1], slot, xd, path))
            taken += 1
        # 盯市
        eq = cash
        for (sym, ep, slot, xd, path) in open_pos:
            if d in path:
                eq += slot * (1 + path[d]) * (1 - COST)
        equity = eq
        curve.append((d, equity, len(open_pos)))
    df = pd.DataFrame(curve, columns=["date", "equity", "slots"]).set_index("date")
    df.to_csv(os.path.join(OUT, "portfolio_equity.csv"))
    return df, taken, len(pos)


def report(df, taken, total):
    e = df.equity
    yrs = (e.index[-1] - e.index[0]).days / 365.25
    cagr = (e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1
    dd = (e / e.cummax() - 1).min()
    print("回测区间 %s ~ %s(%.1f 年)" % (df.index[0].date(), df.index[-1].date(), yrs))
    print("期末权益 %.0f / 期初 %d | 总收益 %+.1f%% | 年化(CAGR) %+.2f%%" % (
        e.iloc[-1], CAPITAL, 100 * (e.iloc[-1] / CAPITAL - 1), 100 * cagr))
    print("最大回撤 %.1f%% | 平均仓位占用 %.2f/5 | 实际开仓 %d / 可用事件 %d(%.1f%%)" % (
        100 * dd, df.slots.mean(), taken, total, 100 * taken / total))
    yearly = e.resample("YE").last()
    y0 = CAPITAL
    print("\n分年权益(年末/年收益%%):")
    for d, v in yearly.items():
        print("  %d: %9.0f (%+.1f%%)" % (d.year, v, 100 * (v / y0 - 1)))
        y0 = v
    mdd_series = e / e.cummax() - 1
    trough = mdd_series.idxmin()
    peak = e[:trough].idxmax()
    print("\n最深回撤段:%s(峰 %.0f)→ %s(谷 %.0f)" % (peak.date(), e[peak], trough.date(), e[trough]))


if __name__ == "__main__":
    report(*run())
