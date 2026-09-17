# -*- coding: utf-8 -*-
"""backtest.py —— 信号→交易模拟。诚实口径:收盘出信号、次日开盘入场、
涨停/停牌买不进、成本双向、T+1 制度。spec §6。"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

LIMIT_UP = 1.097          # 开盘涨幅 ≥ 9.7% 视为涨停买不进(主板 10%)


@dataclass
class Costs:
    commission: float = 0.00025   # 佣金万 2.5(单边)
    stamp: float = 0.001          # 印花税千 1(卖出;2023-08 后实际万 5,保守口径,可配)
    slippage: float = 0.001       # 滑点(单边)

    def buy_mult(self):
        return 1 + self.commission + self.slippage

    def sell_mult(self):
        return 1 - self.commission - self.stamp - self.slippage


def enter(g, sig_pos, cal):
    """入场判定。g:单股帧(index=该股有效交易日);sig_pos:信号日在 g 中的位置;
    cal:市场日历(用于"次日"判定)。返回 dict(buyable/entry_date/entry/reason)。"""
    sig_date = g.index[sig_pos]
    p = cal.get_loc(sig_date) if sig_date in cal else None
    if p is None or p + 1 >= len(cal):
        return {"buyable": False, "reason": "no_calendar", "entry_date": None, "entry": None}
    next_day = cal[p + 1]
    later = g.loc[g.index > sig_date]
    if later.empty:
        return {"buyable": False, "reason": "data_end", "entry_date": None, "entry": None}
    ent = later.iloc[0]
    if later.index[0] != next_day or ent.isna().any():   # 次日无 K 线或整行 NaN(停牌)
        return {"buyable": False, "reason": "suspended", "entry_date": None, "entry": None}
    prev_close = g["close"].iloc[sig_pos]
    if ent["open"] >= prev_close * LIMIT_UP:
        return {"buyable": False, "reason": "limit_up", "entry_date": None, "entry": None}
    return {"buyable": True, "reason": "", "entry_date": later.index[0], "entry": float(ent["open"])}


LIMIT_DOWN = 0.903         # 收盘 ≤ 昨收×0.903 视为跌停封板,卖出顺延(spec §6 卖出侧摩擦)


def _defer_limit_down(v, i):
    """卖出日跌停封板 → 顺延下一可交易日;顺延后到数据末才 censored。返回 (新位置, censored)。"""
    i0 = i
    while i < len(v) - 1 and v["close"].iloc[i] <= v["close"].iloc[i - 1] * LIMIT_DOWN:
        i += 1
    return i, i != i0 and i >= len(v) - 1      # 未发生顺延时不打截尾标


def _sell_close(v, i, cen):
    """统一出场:先跌停顺延,再按顺延日收盘成交。"""
    i2, cen2 = _defer_limit_down(v, i)
    return v.index[i2], float(v["close"].iloc[i2]), bool(cen or cen2)


def _exit_e1(v, i_ent, entry):
    """E1:入场后第 4 根收盘卖(T+1 买,T+5 卖)。不足则末日收盘,censored=True。"""
    cen = i_ent + 4 > len(v) - 1                    # 夹紧前判定:恰好落末根不算截尾
    return _sell_close(v, min(i_ent + 4, len(v) - 1), cen)


def _exit_e2(v, i_ent, entry):
    """E2:J 下拐或 J>90 当日收盘卖;T+1 → 最早入场次日;末日兜底。"""
    j = v["j"]
    for i in range(i_ent + 1, len(v)):
        if j.iloc[i] > 90 or (j.iloc[i] < j.iloc[i - 1]):
            return _sell_close(v, i, False)
    return _sell_close(v, len(v) - 1, True)


def _exit_e3(v, i_ent, entry, tp=0.08, sl=-0.05):
    """E3:止盈/止损触价;跳空穿越按开盘价(若当日封死跌停则顺延);
    T+1 → 最早入场次日;到期未触发 → E1 时限收盘。"""
    hi_t, lo_t = entry * (1 + tp), entry * (1 + sl)
    for i in range(i_ent + 1, len(v)):
        o = v["open"].iloc[i]
        if o <= lo_t or o >= hi_t:
            sealed = o <= lo_t and v["close"].iloc[i] <= v["close"].iloc[i - 1] * LIMIT_DOWN
            if sealed:
                return _sell_close(v, i, False)          # 开盘穿止损且封死 → 顺延
            return v.index[i], float(o), False           # 跳空穿越按开盘价
        if v["low"].iloc[i] <= lo_t:
            if v["close"].iloc[i] <= v["close"].iloc[i - 1] * LIMIT_DOWN:
                return _sell_close(v, i, False)           # 触止损且封死跌停 → 顺延
            return v.index[i], float(lo_t), False         # 盘中触价 → 按触发价成交(与止盈对称)
        if v["high"].iloc[i] >= hi_t:
            return v.index[i], float(hi_t), False
    cen = i_ent + 4 > len(v) - 1
    return _sell_close(v, min(i_ent + 4, len(v) - 1), cen)


def simulate(g, sig_positions, cal, costs):
    """g:单股帧(有效交易日,DatetimeIndex);sig_positions:信号日**整数位置**列表。
    返回交易记录列表(每个信号 × e1/e2/e3 三行;买不进则一行 void 记录)。"""
    v = g.dropna(subset=["close"])
    rows = []
    for sp in sig_positions:
        base = {"sym": None, "signal_date": g.index[sp]}
        r = enter(g, sp, cal)
        if not r["buyable"]:
            rows.append({**base, "scheme": "void", "reason": r["reason"],
                         "ret": None, "mfe": None, "mae": None, "censored": False})
            continue
        i_ent = v.index.get_loc(r["entry_date"])
        entry = r["entry"]
        bm, sm = costs.buy_mult(), costs.sell_mult()
        for scheme, fn in (("e1", _exit_e1), ("e2", _exit_e2), ("e3", _exit_e3)):
            xd, xp, cen = fn(v, i_ent, entry)
            end = v.index.get_loc(xd)
            seg = v.iloc[i_ent: end + 1]
            rows.append({**base, "scheme": scheme, "reason": "",
                         "entry_date": r["entry_date"], "entry": entry,
                         "exit_date": xd, "exit": xp,
                         "ret": xp * sm / (entry * bm) - 1,
                         "mfe": float(seg["high"].max() / entry - 1),
                         "mae": float(seg["low"].min() / entry - 1),
                         "censored": bool(cen)})
    return rows


def prep_groups(grouped, min_bars=120):
    """一次性物化驱动输入:剥层级/剔次新/剔停牌/DatetimeIndex/附加 j。
    返回 (sym, df) 列表——消融/walk-forward 多次 run_signals_fn 时复用,
    避免逐配置逐股票重复算 j(Task 8 质量评审 Important #2 的驱动层修复)。"""
    import data as _d
    groups = []
    for sym, g in grouped:
        g = g.droplevel(0)
        if len(g) < min_bars:                       # 剔次新(spec §5)
            continue
        g = g.dropna(subset=["$close"] if "$close" in g.columns else ["close"])
        g.index = pd.DatetimeIndex(g.index)
        if "j" not in g.columns:
            g = _d.attach_indicators(g if "$close" in g.columns
                                     else g.rename(columns={c: "$" + c for c in g.columns}))
        groups.append((sym, g))
    return groups


def run_signals_fn(grouped, cal, sig_fn, cfg=None, costs=None, min_bars=120, prepped=False):
    """全池驱动。sig_fn(g)(cfg 可选)→布尔列;返回记录 DataFrame。
    prepped=True 时 grouped 需为 prep_groups() 的产物,跳过预处理直通。"""
    costs = costs or Costs()
    rows = []
    for sym, g in (grouped if prepped else prep_groups(grouped, min_bars)):
        sig = sig_fn(g, cfg) if cfg is not None else sig_fn(g)
        pos = [sig.index.get_loc(t) for t in sig.index[sig]]   # Timestamp→整数位置
        for row in simulate(g, pos, cal, costs):
            row["sym"] = sym
            rows.append(row)
    return pd.DataFrame(rows)


def run_signals(grouped, cal, cfg, costs=None, prepped=False):
    import layers
    return run_signals_fn(grouped, cal, layers.signal, cfg, costs, prepped=prepped)
