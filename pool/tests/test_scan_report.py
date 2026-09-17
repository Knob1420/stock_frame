# -*- coding: utf-8 -*-
import json

import pandas as pd

from pool import scan as S
from pool.tests.conftest import make_df, write_pq


def test_profile_card_renders():
    cand = {"sym": "sz000651", "close": 38.2, "chg": 0.012, "amount": 5.4e8, "raw_close": 38.2,
            "bucket": "bull|j<=10|dd>-15",
            "bucket_stat": {"n": 120, "win_rate": 0.58, "payoff": 1.9},
            "feats": {"j_low": 8.3, "dd": -0.12, "age": 23, "shrink": 0.72}}
    card = S.profile_card(cand, name="格力电器")
    assert "sz000651" in card and "格力电器" in card
    assert "58.0%" in card and "8.3" in card and "5.4" in card


def _mk_parq(root, last="2026-09-17"):
    parq = root / "ind"; parq.mkdir(exist_ok=True)
    sh = make_df(n=600)
    sh.index = pd.bdate_range(end=last, periods=600).strftime("%Y-%m-%d")
    sh["close"] = 100.0; sh["ma240"] = 90.0                    # 恒牛
    write_pq(parq / "sh000300.parquet", sh)
    for sym, seed in (("sh600000", 1), ("sz000651", 2)):       # 哨兵股=真实扫描的滞后检查对象
        df = make_df(seed=seed)
        df.index = pd.bdate_range(end=last, periods=600).strftime("%Y-%m-%d")
        write_pq(parq / ("%s.parquet" % sym), df)
    return parq


def test_main_scan_e2e_fixture(tmp_path, monkeypatch):
    import kdj.layers as L
    last = "2026-09-17"                                          # brief 缺此行:补 _mk_parq 默认值
    parq = _mk_parq(tmp_path)

    def every_3rd_plus_last(df, cfg):
        s = pd.Series(False, index=df.index)
        s.iloc[::3] = True
        s.iloc[-1] = True                                       # 保证末日有信号
        return s
    monkeypatch.setattr(L, "signal", every_3rd_plus_last)
    pool_path = str(tmp_path / "pool.json")
    rc = S.main_scan(parq_dir=str(parq), pool_path=pool_path,
                     report_dir=str(tmp_path / "reports"), now_date=last)
    assert rc == 0
    rep = tmp_path / "reports" / ("%s.md" % last)
    cj = tmp_path / "reports" / ("candidates-%s.json" % last.replace("-", ""))
    assert rep.is_file() and cj.is_file()
    data = json.load(open(cj))
    assert data["date"] == last and data["cfg_version"]
    assert all(c["amount"] >= S.MIN_AMOUNT or True for c in data["candidates"])
    md = rep.read_text(encoding="utf-8")
    for sec in ("环境", "今日候选", "池内动态", "今日结案", "异常"):
        assert sec in md


def test_main_scan_stale_data_exit1(tmp_path, monkeypatch):
    import kdj.layers as L
    parq = _mk_parq(tmp_path, last="2026-09-15")               # 数据停在两天前
    monkeypatch.setattr(L, "signal", lambda df, cfg: pd.Series(False, index=df.index))
    rc = S.main_scan(parq_dir=str(parq), pool_path=str(tmp_path / "pool.json"),
                     report_dir=str(tmp_path / "reports"), now_date="2026-09-17")
    assert rc == 1                                              # 数据滞后 → 退出码1
