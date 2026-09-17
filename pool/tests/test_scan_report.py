# -*- coding: utf-8 -*-
import json

import pandas as pd

from pool import cfg as C
from pool import pool as P
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


def test_profile_card_price_is_raw():
    cand = {"sym": "sz000651", "close": 45.6, "chg": 0.012, "amount": 5.4e8, "raw_close": 38.2,
            "bucket": "bull|j<=10|dd>-15",
            "bucket_stat": {"n": 120, "win_rate": 0.58, "payoff": 1.9},
            "feats": {"j_low": 8.3, "dd": -0.12, "age": 23, "shrink": 0.72}}
    card = S.profile_card(cand, name="格力电器")
    assert "38.20" in card                        # 收盘显示现价(raw_close)
    assert "45.60" not in card                    # 后复权价不再进人读卡片


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
    monkeypatch.setattr(C, "LOOKUP_DIR", str(tmp_path / "lookup"))   # 隔离:勿污染真实 lookup 目录
    pool_path = str(tmp_path / "pool.json")
    rc = S.main_scan(parq_dir=str(parq), pool_path=pool_path,
                     report_dir=str(tmp_path / "reports"), now_date=last)
    assert rc == 0
    rep = tmp_path / "reports" / ("%s.md" % last)
    cj = tmp_path / "reports" / ("candidates-%s.json" % last.replace("-", ""))
    assert rep.is_file() and cj.is_file()
    data = json.load(open(cj))
    assert data["date"] == last and data["cfg_version"]
    assert all(c["amount"] >= S.MIN_AMOUNT for c in data["candidates"])   # top 已过硬过滤
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


def test_main_scan_tracking_closes_entry(tmp_path, monkeypatch):
    import kdj.layers as L
    parq = _mk_parq(tmp_path)
    monkeypatch.setattr(C, "LOOKUP_DIR", str(tmp_path / "lookup"))   # 隔离 lookup 目录
    monkeypatch.setattr(L, "signal", lambda df, cfg: pd.Series(False, index=df.index))

    df = pd.read_parquet(parq / "sh600000.parquet")
    added = df.index[-6]                                        # 跟踪窗口=末 5 根K线
    add_close = float(df.at[added, "close"])                    # added 日实际收盘作基准
    d1 = df.index[-5]                                           # 首根跟踪K线:显式改写极值
    df.at[d1, "high"] = add_close * 1.09                        # +9% → hit 触发
    df.at[d1, "low"] = add_close * 0.96                         # −4% → min_dn 更新但不 miss
    write_pq(parq / "sh600000.parquet", df)
    pool = {"version": 1, "pool": []}
    P.add_entry(pool, "600000", "浦发银行", "t", [], {}, add_close, added, [added], "test")
    P.add_entry(pool, "000999", "缺数据", "t", [], {}, 10.0, added, [added], "test")
    pool_path = str(tmp_path / "pool.json")
    P.save_pool(pool, pool_path)

    kw = dict(parq_dir=str(parq), pool_path=pool_path,
              report_dir=str(tmp_path / "reports"), now_date="2026-09-17")
    assert S.main_scan(**kw) == 0
    st = json.load(open(pool_path))
    e = {x["code"]: x for x in st["pool"]}
    assert e["600000"]["outcome"]["type"] == "hit"              # 首根K线即触发 +8%
    assert e["600000"]["days"] == 1 and e["600000"]["last_date"] == df.index[-5]
    assert e["600000"]["max_up"] >= 0.08 and -0.05 < e["600000"]["min_dn"] < 0
    assert e["600000"]["max_up_day"] == 1 and e["600000"]["max_up_date"] == df.index[-5]
    assert e["600000"]["min_dn_day"] == 1 and e["600000"]["min_dn_date"] == df.index[-5]
    assert e["000999"]["status"] == "watching"                  # parquet 缺失 → 记异常不结案
    md = (tmp_path / "reports" / "2026-09-17.md").read_text(encoding="utf-8")
    assert "600000 → **hit**" in md and "parquet 缺失" in md

    assert S.main_scan(**kw) == 0                               # 二次运行幂等
    st2 = json.load(open(pool_path))
    e2 = {x["code"]: x for x in st2["pool"]}["600000"]
    assert e2["days"] == 1 and e2["last_date"] == df.index[-5]  # outcome 冻结,不重复计
    assert e2["outcome"] == e["600000"]["outcome"]


def test_pool_dynamic_displays_raw_prices(tmp_path, monkeypatch):
    import kdj.layers as L
    parq = _mk_parq(tmp_path)
    monkeypatch.setattr(C, "LOOKUP_DIR", str(tmp_path / "lookup"))
    monkeypatch.setattr(L, "signal", lambda df, cfg: pd.Series(False, index=df.index))

    df = pd.read_parquet(parq / "sh600000.parquet")
    added, last = df.index[-2], df.index[-1]
    df.at[added, "factor"] = 1.25                 # 模拟入池后除权:factor 抬升→现价骤降
    df.at[last, "factor"] = 2.5
    write_pq(parq / "sh600000.parquet", df)
    base_raw = float(df.at[added, "close"] / 1.25)
    last_raw = float(df.at[last, "close"] / 2.5)

    pool = {"version": 1, "pool": []}
    P.add_entry(pool, "600000", "浦发银行", "t", [], {}, float(df.at[added, "close"]), added,
                [added], "test", add_close_raw=base_raw)
    pool_path = str(tmp_path / "pool.json")
    P.save_pool(pool, pool_path)

    assert S.main_scan(parq_dir=str(parq), pool_path=pool_path,
                       report_dir=str(tmp_path / "reports"), now_date="2026-09-17") == 0
    st = json.load(open(pool_path))
    e = st["pool"][0]
    assert e["status"] == "watching" and e["add_close_raw"] == base_raw   # 未触发,留存观察
    md = (tmp_path / "reports" / "2026-09-17.md").read_text(encoding="utf-8")
    assert "基准 %.2f" % base_raw in md                          # 基准=入池日现价
    assert "基准 %.2f" % float(df.at[added, "close"]) not in md  # 后复权基准不进人读行
    assert "现价 %.2f" % last_raw in md                          # last_close=当日现价
    assert "期间除权" in md                                      # 显示价差与极值背离→注记


def test_main_scan_preserves_ai_section_on_rerun(tmp_path, monkeypatch):
    import kdj.layers as L
    parq = _mk_parq(tmp_path)
    monkeypatch.setattr(C, "LOOKUP_DIR", str(tmp_path / "lookup"))
    kw = dict(parq_dir=str(parq), pool_path=str(tmp_path / "pool.json"),
              report_dir=str(tmp_path / "reports"), now_date="2026-09-17")
    monkeypatch.setattr(L, "signal", lambda df, cfg: pd.Series(False, index=df.index))
    assert S.main_scan(**kw) == 0
    rep = tmp_path / "reports" / "2026-09-17.md"
    assert "信号 0 只" in rep.read_text(encoding="utf-8")
    with open(rep, "a", encoding="utf-8") as f:                  # 模拟 AI 在 §6 追加研究卡
        f.write("- MARKER-AI研究卡-勿删\n")

    def last_only(df, cfg):
        s = pd.Series(False, index=df.index)
        s.iloc[-1] = True
        return s
    monkeypatch.setattr(L, "signal", last_only)                  # 换信号:§1-5 必须刷新
    assert S.main_scan(**kw) == 0
    md = rep.read_text(encoding="utf-8")
    assert "MARKER-AI研究卡-勿删" in md                         # §6 在重跑后保留(reports/不入库)
    assert "信号 2 只" in md and "信号 0 只" not in md           # §1-5 已刷新(0→2)
    assert md.count("## 6.") == 1 and md.count("## 1.") == 1    # 不重复,顺序完整
    assert md.index("## 1.") < md.index("## 6.")


def test_stale_report_preserves_ai_section(tmp_path, monkeypatch):
    import kdj.layers as L
    parq = _mk_parq(tmp_path, last="2026-09-15")                 # 数据停在两天前
    monkeypatch.setattr(L, "signal", lambda df, cfg: pd.Series(False, index=df.index))
    rep = tmp_path / "reports" / "2026-09-17.md"
    rep.parent.mkdir(parents=True)
    rep.write_text("# 观察池日报 2026-09-17\n\n## 6. AI 研究区\n\n- MARKER-旧研究卡\n",
                   encoding="utf-8")
    rc = S.main_scan(parq_dir=str(parq), pool_path=str(tmp_path / "pool.json"),
                     report_dir=str(rep.parent), now_date="2026-09-17")
    assert rc == 1                                               # 滞后退出码不变
    md = rep.read_text(encoding="utf-8")
    assert "数据滞后" in md and "MARKER-旧研究卡" in md         # ⚠ 覆盖 §1-5 区,§6 保留
    assert md.count("## 6.") == 1
