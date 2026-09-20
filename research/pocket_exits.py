# -*- coding: utf-8 -*-
"""pocket_exits.py —— 口袋支点退出工程(用户 2026-09-20 方案):
规则A 保本:浮盈曾≥b% 后,止损=成本价(跳空按开盘)
规则C 移动:浮盈曾≥b% 后,止损=max(成本, 峰值−t)   [A 为 C 的 floor 特例,合并实现]
规则B 时间:第 dstop 日收盘时 MFE<3% → 当日收盘离场
最长持有 H 日;全部成本分板块(主板0.1/创科0.2/北交0.3 滑点+佣金印花)。
网格:b∈{3,5,8}% × t∈{5,8,10}% × dstop∈{3,4,5} × H∈{10,15,20};选高原不选尖峰。
用法: python pocket_exits.py   # 先建 out/pocket_paths.npz,再网格
"""
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
IND = os.path.join(os.path.dirname(HERE), "stockdata", "indicators")
OUT = os.path.join(HERE, "out")
NDAYS = 20


def build_paths():
    tr = pd.read_csv(os.path.join(OUT, "pocket_trades.csv"))   # 修复口径后的信号
    syms, dates, O, H, L, C, board = [], [], [], [], [], [], []
    for sym, g in tr.groupby("sym"):
        p = os.path.join(IND, "%s.parquet" % sym)
        if not os.path.exists(p):
            continue
        df = pd.read_parquet(p)
        df.index = pd.DatetimeIndex(df.index)
        o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
        for d in pd.to_datetime(g.date):
            if d not in df.index:
                continue
            i = df.index.get_loc(d)
            if i + 2 >= len(df) or np.isnan(o[i + 1]):
                continue
            e = o[i + 1]
            lim = 1.297 if sym.startswith("bj") else (1.197 if sym.startswith(("sz30", "sh68")) else 1.097)
            if e >= c[i] * lim:
                continue                                   # 涨停买不进(分板块)
            sl = slice(i + 1, i + 1 + NDAYS)
            n = len(range(*sl.indices(len(df))))
            row_o = np.full(NDAYS, np.nan); row_o[:n] = o[sl] / e
            row_h = np.full(NDAYS, np.nan); row_h[:n] = h[sl] / e
            row_l = np.full(NDAYS, np.nan); row_l[:n] = l[sl] / e
            row_c = np.full(NDAYS, np.nan); row_c[:n] = c[sl] / e
            syms.append(sym); dates.append(d)
            O.append(row_o); H.append(row_h); L.append(row_l); C.append(row_c)
            board.append(0 if sym.startswith(("sz30", "sh68")) is False and not sym.startswith("bj") else
                         (1 if not sym.startswith("bj") else 2))
    np.savez_compressed(os.path.join(OUT, "pocket_paths.npz"),
                        O=np.array(O), H=np.array(H), L=np.array(L), C=np.array(C),
                        board=np.array(board),
                        meta=np.array([f"{s}|{d.date()}" for s, d in zip(syms, dates)]))
    print("路径 %d × %d 日 → pocket_paths.npz" % (len(syms), NDAYS))


def slip_cost(board):
    s = np.where(board == 2, 0.003, np.where(board == 1, 0.002, 0.001))
    return 0.00025 * 2 + 0.0005 + 2 * s


def run_engine(b, t, dstop, H, O, Hh, L, C):
    """向量化:N 个信号并行逐日推进。返回净收益数组。"""
    N = O.shape[0]
    peak = np.ones(N)                    # 峰值(相对 entry 的倍数)
    stop = np.zeros(N)                   # 0=未设
    exit_px = np.full(N, np.nan)
    exit_day = np.full(N, NDAYS)
    done = np.zeros(N, bool)
    for k in range(NDAYS):
        live = ~done & ~np.isnan(O[:, k])
        o, h, l = O[:, k], Hh[:, k], L[:, k]
        if k == 0:                       # 入场日:不卖(T+1),只记峰值
            peak = np.where(live & (h > peak), h, peak)
            continue
        armed = live & (peak >= 1 + b)               # 用昨收前峰值
        ts = live & (k + 1 == dstop) & ((peak - 1) < 0.03)      # B 时间止损(收盘判,含当日)
        stop_lvl = np.where(armed, np.maximum(1.0, peak - t), 0.0)
        gap = live & (stop_lvl > 0) & (o <= stop_lvl) & ~ts
        intr = live & (stop_lvl > 0) & ~gap & (l <= stop_lvl) & ~ts
        fired = gap | intr | ts
        exit_px = np.where(fired, np.where(gap, o, np.where(intr, stop_lvl, C[:, k])), exit_px)
        exit_day = np.where(fired, k + 1, exit_day)
        done = done | fired                                  # 累积,不复活
        peak = np.where(live & ~done & (h > peak), h, peak)    # 出场者不再记峰
        if k + 1 == H:
            rest = live & ~done
            exit_px = np.where(rest, C[:, k], exit_px)
            exit_day = np.where(rest, k + 1, exit_day)
            done = done | rest
    # 数据不足者按最后有效收盘
    lastc = np.array([C[i][~np.isnan(C[i])][-1] if (~np.isnan(C[i])).any() else 1.0 for i in range(N)])
    exit_px = np.where(np.isnan(exit_px), lastc, exit_px)
    return exit_px - 1.0, exit_day


def main():
    f = os.path.join(OUT, "pocket_paths.npz")
    if not os.path.exists(f):
        build_paths()
    z = np.load(f, allow_pickle=True)
    O, Hh, L, C, board = z["O"], z["H"], z["L"], z["C"], z["board"]
    cost = slip_cost(board)
    N = len(O)
    print("N=%d 信号(分板块:主板 %d / 创科 %d / 北交 %d)" % (
        N, (board == 0).sum(), (board == 1).sum(), (board == 2).sum()))
    # 无规则基准
    for H in (10, 15, 20):
        r = C[:, H - 1] - 1.0
        r = r[~np.isnan(r)]
        print("基准 持有%d日(无规则):净 %+.2f%% 胜率 %.1f%%" % (
            H, 100 * (r * (1 - cost[:len(r)] if len(r) == len(cost) else 1)).mean(), 100 * (r > 0).mean()))
    rows = []
    for b in (0.03, 0.05, 0.08):
        for t in (0.05, 0.08, 0.10):
            for ds in (3, 4, 5):
                for H in (10, 15, 20):
                    g, days = run_engine(b, t, ds, H, O, Hh, L, C)
                    net = (1 + g) * (1 - cost) - 1
                    ok = ~np.isnan(g)
                    rows.append({"b%": int(b * 100), "t%": int(t * 100), "dstop": ds, "H": H,
                                 "net%": 100 * net[ok].mean(), "win%": 100 * (net[ok] > 0).mean(),
                                 "hold": np.median(days[ok])})
    G = pd.DataFrame(rows)
    print("\n== 网格结果(按净均值排序,前 15) ==")
    print(G.sort_values("net%", ascending=False).head(15).to_string(index=False, float_format=lambda x: "%.2f" % x))
    print("\n== 高原检验(参数邻域均值) ==")
    G["rank"] = G["net%"].rank(ascending=False)
    top = G.sort_values("net%", ascending=False).head(15)
    plateaus = []
    for _, r in top.iterrows():
        nb = G[(G["b%"] == r["b%"]) & (G["t%"].between(r["t%"] - 3, r["t%"] + 3)) &
               (G["dstop"].between(r["dstop"] - 1, r["dstop"] + 1)) & (G["H"].between(r["H"] - 5, r["H"] + 5))]
        plateaus.append(len(nb[nb["net%"] > 1.0]) / len(nb))
    top = top.copy()
    top["邻域>1%占比"] = plateaus
    print(top[["b%", "t%", "dstop", "H", "net%", "win%", "hold", "邻域>1%占比"]].to_string(
        index=False, float_format=lambda x: "%.2f" % x))
    G.to_csv(os.path.join(OUT, "pocket_exit_grid.csv"), index=False)
    print("\n网格明细 → out/pocket_exit_grid.csv")


if __name__ == "__main__":
    main()
