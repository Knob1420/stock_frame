# -*- coding: utf-8 -*-
import pandas as pd
import pytest

from pool import scan as S
from pool.tests.conftest import make_df, write_pq


def test_assemble_maps_j():
    df = make_df(n=30)
    g = S.assemble(df)
    assert list(g.columns) == ["open", "high", "low", "close", "volume", "j"]
    assert (g["j"] == df["kdj_j"]).all()


def test_collect_uses_injected_signal_and_tail(tmp_path, monkeypatch):
    import kdj.layers as L
    parq = tmp_path / "ind"; parq.mkdir()
    write_pq(parq / "sh600001.parquet", make_df(seed=1))
    write_pq(parq / "sz000002.parquet", make_df(seed=2))
    write_pq(parq / "sz300999.parquet", make_df(seed=3))        # 创业板:白名单外
    write_pq(parq / "sh000300.parquet", make_df(seed=4))

    def last_row_only(df, cfg):
        s = pd.Series(False, index=df.index)
        s.iloc[-1] = True
        return s
    cands, anomalies = S.collect_candidates(str(parq), expected_date=None, signal_fn=last_row_only)
    syms = [c["sym"] for c in cands]
    assert syms == ["sh600001", "sz000002"]                     # 白名单+信号,指数/创业板排除
    c = cands[0]
    assert {"j_low", "dd", "age", "shrink"} <= set(c["feats"])
    assert c["amount"] > 0


def test_screen_hard_filter_and_rank():
    tbl = {"bull|j<=10|dd>-15": {"n": 100, "win_rate": 0.6, "payoff": 2.0, "types": {}},
           "bear|10<j<=15|dd<=-15": {"n": 10, "win_rate": 0.9, "payoff": None, "types": {}}}
    lookup_data = {"cfg_version": "x", "buckets": tbl}
    mk = lambda sym, amount, chg, bucket: {"sym": sym, "amount": amount, "chg": chg,
                                           "bucket": bucket, "feats": {}, "close": 10, "date": "d"}
    cands = [mk("a", 3e8, 0.01, "bull|j<=10|dd>-15"),      # n=100桶 胜率0.6
             mk("b", 5e8, 0.005, "bull|j<=10|dd>-15"),      # 同桶,额大 → 排前
             mk("c", 9e8, 0.20, "bull|j<=10|dd>-15"),       # 涨停代理≥9.7% → 剔除
             mk("d", 1e7, 0.01, "bull|j<=10|dd>-15"),       # 成交额<2亿 → 剔除
             mk("e", 4e8, 0.01, "bear|10<j<=15|dd<=-15")]   # n=10<30 → 中位(0.6)
    top, nrej = S.screen(cands, lookup_data, top=3)
    assert nrej == 2 and [t["sym"] for t in top] == ["b", "a", "e"]
    assert top[2]["score"] == pytest.approx(0.6)             # e 拿中位分


def test_screen_short_list_kept():
    lookup_data = {"cfg_version": "x", "buckets": {}}
    cands = [{"sym": "a", "amount": 3e8, "chg": 0.0, "bucket": "bull|j<=10|dd>-15",
              "feats": {}, "close": 10, "date": "d"}]
    top, nrej = S.screen(cands, lookup_data, top=30)
    assert len(top) == 1 and nrej == 0                       # 不足30全保留,如实列数
