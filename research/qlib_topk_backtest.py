# -*- coding: utf-8 -*-
"""qlib_topk_backtest.py —— B1(用户指定的 Qlib TopkDropout 第三方组合层对照)。
信号:B2 复核事件表里 门内 B1+ 事件(b2_trades.csv,tag==B1+ & gate)——独立于自家管道;
打分:事件日起 15 个交易日内 score=1.0(持有窗),过期自然跌出 topk 被卖。
回测:qlib TopkDropoutStrategy,topk=5,账户 50 万,开盘成交,涨跌停/停牌约束由 qlib exchange 处理。
输出:年化/最大回撤/换手 vs 沪深300;对比自家 portfolio_sim(年化 −2.65%/回撤 −73.6%)。
"""
import os
import sys

import pandas as pd

import qlib
from qlib.data import D
from qlib.constant import REG_CN
from qlib.config import C
from qlib.utils import init_instance_by_config
from qlib.backtest import backtest  # noqa: F401  (0.9.x 高层入口)
from qlib.contrib.strategy import TopkDropoutStrategy
from qlib.contrib.evaluate import risk_analysis

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROVIDER = os.path.join(ROOT, "stockdata", "qlib_bin")
TOPK, HOLD_DAYS, CAPITAL = 5, 15, 500_000

qlib.init(provider_uri=PROVIDER, region=REG_CN)

# ---- 信号:门内 B1+ 事件日 → 未来 15 个交易日 score=1 ----
tr = pd.read_csv(os.path.join(HERE, "out", "b2_trades.csv"))
tr = tr[(tr["tag"] == "B1+") & tr["gate"]]
cal = D.calendar(freq="day")
cal = pd.to_datetime(pd.Series(cal))
rows = []
for _, r in tr.iterrows():
    d = pd.Timestamp(r["date"])
    pos = cal.searchsorted(d)
    for k in range(1, HOLD_DAYS + 1):          # 入场次日起可持有
        if pos + k < len(cal):
            rows.append((cal.iloc[pos + k], str(r["sym"]).upper(), 1.0))
sig = pd.DataFrame(rows, columns=["datetime", "instrument", "score"]).drop_duplicates(
    ["datetime", "instrument"]).sort_values(["datetime", "instrument"])
print("信号行 %d,覆盖 %d 个交易日" % (len(sig), sig.datetime.nunique()))

benchmark = "SH000300"
exchange_kwargs = {
    "freq": "day",
    "limit_threshold": 0.095,
    "deal_price": "open",
    "open_cost": 0.001,
    "close_cost": 0.002,
    "min_cost": 5,
}

strategy = TopkDropoutStrategy(signal=sig, topk=TOPK, dropout=TOPK,
                               method_sell="bottom", method_buy="top",
                               hold_thresh=HOLD_DAYS)   # ponytail: hold_thresh 若该版忽略,由 score 15 日过期兜底
executor = {
    "class": "NestedExecutor",
    "module_path": "qlib.backtest.executor",
    "kwargs": {
        "time_per_step": "day",
        "inner_executor": {"class": "SimulatorExecutor",
                           "module_path": "qlib.backtest.executor",
                           "kwargs": {"time_per_step": "day", "generate_portfolio_metrics": True}},
        "inner_strategy": {"class": "EmptyStrategy", "module_path": "qlib.contrib.strategy"},
    },
}
port_er = qlib.backtest.backtest(
    start_time="2010-01-01", end_time="2026-09-01",
    strategy=strategy, executor=init_instance_by_config(executor),
    account=CAPITAL, benchmark=benchmark, exchange_kwargs=exchange_kwargs)
report = port_er.report_df
print("\n== qlib risk_analysis(成本后,vs SH000300) ==")
print(risk_analysis(report["return"] - report["bench"]).to_string())
print("\n== 绝对口径 ==")
print(risk_analysis(report["return"], report_full=False).to_string())
report.to_csv(os.path.join(HERE, "out", "qlib_topk_report.csv"))
