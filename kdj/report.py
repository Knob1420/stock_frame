# -*- coding: utf-8 -*-
"""report.py —— 统计切片、环境日历、消融与 walk-forward 汇总,产 markdown 报告。spec §6。"""
import pandas as pd

# 滚动 walk-forward:训练 6 年 → 测试 2 年,步进 2 年(spec §6 修订②)
FOLDS = [
    ((2010, 2015), (2016, 2017)),
    ((2012, 2017), (2018, 2019)),
    ((2014, 2019), (2020, 2021)),
    ((2016, 2021), (2022, 2023)),
    ((2018, 2023), (2024, 2026)),
]


def stats(trades):
    empty = {"n": 0, "win_rate": float("nan"), "mean_ret": float("nan"),
             "payoff": float("nan"), "big_loss_rate": float("nan"),
             "mfe_med": float("nan"), "mae_med": float("nan"), "censored_share": float("nan")}
    if trades.empty or "ret" not in trades:         # 全池零信号的配置不崩
        return empty
    t = trades.dropna(subset=["ret"])
    if t.empty:
        return empty
    ret = t["ret"]
    win, loss = ret[ret > 0], ret[ret <= 0]
    return {
        "n": len(t),
        "win_rate": (ret > 0).mean(),
        "mean_ret": ret.mean(),
        "payoff": win.mean() / abs(loss.mean()) if len(loss) and len(win) else float("nan"),
        "big_loss_rate": (ret < -0.05).mean(),
        "mfe_med": t["mfe"].median(),
        "mae_med": t["mae"].median(),
        "censored_share": t["censored"].mean() if "censored" in t else 0.0,
    }


def slice_stats(trades, regime):
    """分年 × 分环境(信号日口径)。"""
    if trades.empty or "ret" not in trades:
        return pd.DataFrame()
    t = trades.dropna(subset=["ret"]).copy()
    t["year"] = pd.to_datetime(t["signal_date"]).dt.year
    t["bull"] = pd.to_datetime(t["signal_date"]).map(
        lambda d: bool(regime["bull"].reindex([d], method="ffill").iloc[0])
        if (regime.index <= d).any() else False)
    rows = {}
    for (y, b), g in t.groupby(["year", "bull"]):
        rows[(y, b)] = stats(g)
    return pd.DataFrame(rows).T.sort_index()


def regime_calendar(regime, folds=FOLDS):
    """分段 × 环境天数矩阵(markdown 字符串)。spec §6 修订①:分段环境占比显式可见。"""
    lines = ["| 折 | 测试段 | 牛性天数 | 熊性天数 | 牛性占比 |",
             "|---|---|---|---|---|"]
    for (tr, te) in folds:
        m = (regime.index.year >= te[0]) & (regime.index.year <= te[1])
        seg = regime[m]
        nb = int(seg["bull"].sum())
        label = "%d–%d%s" % (te[0], te[1], "*" if te == (2024, 2026) else "")
        lines.append("| %s | %s | %d | %d | %.0f%% |" % (
            label, label, nb, len(seg) - nb, 100 * nb / max(len(seg), 1)))
    lines.append("")
    lines.append("*第 5 折测试段约 2.7 年（至数据末日 2026-08-21），为表宽例外，spec §6 已披露。")
    return "\n".join(lines)


def signals_per_day(trades):
    """唯一信号(sym×日,含 void)的日均数——每信号 3 行(e1/e2/e3)必须去重。"""
    if trades.empty or "sym" not in trades:
        return 0.0
    uniq = trades.drop_duplicates(subset=["sym", "signal_date"])
    return uniq.groupby(pd.to_datetime(uniq["signal_date"]).dt.date).size().mean()


def buy_fail_rate(trades):
    """买不进率:唯一信号中 void(涨停/停牌)占比。"""
    if trades.empty or "sym" not in trades:
        return 0.0
    uniq = trades.drop_duplicates(subset=["sym", "signal_date"])
    return float((uniq["scheme"] == "void").mean())


ABLATION_MENU = [
    # 基线两变体不在本菜单——由 CLI --mode baseline 专跑(spec §6 验证流程第 1 步)
    {"name": "l1_good_only", "cfg": {"l1": ["good"]}},
    {"name": "l1_good_stable", "cfg": {"l1": ["good", "stable"]}},
    {"name": "l1_full_age5_80", "cfg": {}},                          # 默认即全量
    {"name": "no_low", "cfg": {"l2": ["shrink", "depth"]}},
    {"name": "no_shrink", "cfg": {"l2": ["low", "depth"]}},
    {"name": "no_depth", "cfg": {"l2": ["low", "shrink"]}},
    {"name": "plus_ma240", "cfg": {"ma240": True}},
    {"name": "l3_hook_first", "cfg": {"l3": ["first"]}},
    {"name": "l3_hook_bull", "cfg": {"l3": ["hook", "bull"]}},
    {"name": "l3_hook_ma5", "cfg": {"l3": ["hook", "ma5"]}},
    {"name": "l3_hook_vol", "cfg": {"l3": ["hook", "vol"]}},
    {"name": "l3_hook_pct", "cfg": {"l3": ["hook", "pct"]}},         # 对比项 d:原 |PCT| 复刻
    {"name": "w=3", "cfg": {"w": 3}},
    {"name": "w=8", "cfg": {"w": 8}},
    {"name": "shrink=0.7", "cfg": {"shrink": 0.7}},
    {"name": "shrink=0.9", "cfg": {"shrink": 0.9}},
    {"name": "depth=-0.20", "cfg": {"depth": -0.20}},
    {"name": "depth=-0.30", "cfg": {"depth": -0.30}},
]


def walk_forward_years(folds=FOLDS):
    return [te for _, te in folds]


def render_cfg_table(df):
    """配置统计表 → markdown。tabulate 会对短单元格做列宽填充(name 列被 pad 成
    "| a      |"),压缩多余空白 → "| a |":markdown 渲染不变,行内容可直接断言。"""
    import re
    cols = ["name", "n", "win_rate", "mean_ret", "payoff", "big_loss_rate", "mfe_med", "mae_med",
            "sig_per_day", "buy_fail_rate"]
    md = df[cols].to_markdown(index=False, floatfmt=".4f")
    return re.sub(r" {2,}", " ", md)


def run_ablation(grouped, cal, regime, scheme="e1", outdir="reports"):
    """跑消融菜单:每配置全池信号→模拟→全样本统计 + 分年×分环境切片(spec §6:所有指标必须切片)。
    prep_groups 物化一次,18 个配置复用。"""
    import os
    import backtest
    os.makedirs(outdir, exist_ok=True)
    prepped = backtest.prep_groups(grouped)
    rows, details = [], []
    for item in ABLATION_MENU:
        trades = backtest.run_signals(prepped, cal, item.get("cfg", {}), prepped=True)
        t = trades[trades["scheme"] == scheme] if not trades.empty else trades
        s = stats(t)
        s["name"] = item["name"]
        s["sig_per_day"] = signals_per_day(trades)
        s["buy_fail_rate"] = buy_fail_rate(trades)
        rows.append(s)
        details.append("\n## %s\n\n%s\n" % (
            item["name"], slice_stats(t, regime).to_markdown(floatfmt=".4f")))
    df = pd.DataFrame(rows)
    with open(os.path.join(outdir, "ablation_%s.md" % scheme), "w", encoding="utf-8") as f:
        f.write("# 消融(%s)\n\n> 注:日均信号量/买不进率为全样本口径(未按年×环境切片)。\n\n"
                "%s\n\n# 分年×分环境切片\n%s"
                % (scheme, render_cfg_table(df), "\n".join(details)))
    return df


if __name__ == "__main__":
    import argparse
    import backtest
    import layers
    from data import load_market, load_index

    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline", "ablation"], required=True)   # wf 在 Task 12 加
    ap.add_argument("--scheme", default="e1", choices=["e1", "e2", "e3"])
    a = ap.parse_args()
    if a.mode == "baseline":
        for pool in ("new", "old"):
            grouped, cal = load_market(variant=pool)
            prepped = backtest.prep_groups(grouped)
            trades = backtest.run_signals_fn(prepped, cal, layers.baseline_original, prepped=True)
            t = trades[trades["scheme"] == a.scheme] if not trades.empty else trades
            print(pool, stats(t), "sig/day=%.1f" % signals_per_day(trades),
                  "buyfail=%.1f%%" % (100 * buy_fail_rate(trades)))
            del grouped, prepped, trades
    elif a.mode == "ablation":
        regime = load_index()
        grouped, cal = load_market()
        df = run_ablation(grouped, cal, regime, scheme=a.scheme)
        print(render_cfg_table(df))
