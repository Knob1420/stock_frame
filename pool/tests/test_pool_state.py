# -*- coding: utf-8 -*-
import pytest

from pool import pool as P
from pool.cfg import CLOSE_CFG, cfg_version

D = ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19", "2026-09-22",
     "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-29",
     "2026-09-30", "2026-10-08", "2026-10-09"]


def mk_entry(added=D[0]):
    return {"code": "000651", "name": "", "added": added, "cfg_version": cfg_version(),
            "add_close": 10.0, "reason": "", "reason_tags": [],
            "snapshot": {"env": "牛性", "j_low": 8, "drawdown": -0.18, "age": 23},
            "status": "watching", "max_up": 0.0, "min_dn": 0.0, "days": 0,
            "max_up_day": None, "max_up_date": None,
            "min_dn_day": None, "min_dn_date": None,
            "last_date": None, "last_close": None, "outcome": None, "trade": None}


def bar(d, hi, lo, c):
    return (d, hi, lo, c)


# ---------- 入池 ----------
def test_add_entry_ok_and_rejects():
    pool = {"version": 1, "pool": []}
    e = P.add_entry(pool, "000651", "格力电器", "缩量+J首拐", ["缩量回调", "J首拐"],
                    {"env": "牛性"}, 38.2, D[1], [D[0], D[1]], cfg_version())
    assert e["status"] == "watching" and pool["pool"][-1] is e
    with pytest.raises(ValueError):        # 隔日名单拒绝(快照失真)
        P.add_entry(pool, "600941", "", "", [], {}, 10, D[1], [D[0]], cfg_version())
    with pytest.raises(ValueError):        # watching 重复入池拒绝
        P.add_entry(pool, "000651", "", "", [], {}, 38, D[1], [D[1]], cfg_version())


def test_closed_can_reenter():
    pool = {"version": 1, "pool": []}
    P.add_entry(pool, "000651", "", "", [], {}, 10, D[0], [D[0]], cfg_version())
    pool["pool"][0]["status"] = "closed"
    P.add_entry(pool, "000651", "", "", [], {}, 11, D[1], [D[1]], cfg_version())  # 不抛
    assert len(pool["pool"]) == 2


# ---------- 跟踪与结案 ----------
def test_track_updates_extremes_idempotent():
    e = mk_entry()
    bars = [bar(D[1], 10.4, 9.9, 10.2), bar(D[2], 10.1, 9.5, 9.6)]
    assert P.track(e, bars, n_elapsed=2) is None          # 未触发
    assert e["max_up"] == pytest.approx(0.04) and e["min_dn"] == pytest.approx(-0.05 - 1e-9)
    P.track(e, bars, n_elapsed=2)                          # 同批重跑 → 幂等
    assert e["max_up"] == pytest.approx(0.04) and e["days"] == 2


def test_close_hit():
    e = mk_entry()
    o = P.track(e, [bar(D[1], 10.9, 9.9, 10.8)], n_elapsed=1)
    assert o["type"] == "hit" and e["status"] == "closed"
    assert P.track(e, [bar(D[2], 11.5, 9.0, 9.1)], n_elapsed=2) is None   # 结案后冻结


def test_close_miss():
    e = mk_entry()
    o = P.track(e, [bar(D[1], 10.1, 9.4, 9.5)], n_elapsed=1)
    assert o["type"] == "miss" and e["status"] == "closed"


def test_close_flat_on_window_expiry():
    e = mk_entry()
    bars = [bar(D[i + 1], 10.05, 9.96, 10.0) for i in range(CLOSE_CFG["window"])]
    o = P.track(e, bars, n_elapsed=CLOSE_CFG["window"])
    assert o["type"] == "flat" and o["days"] == CLOSE_CFG["window"]


def test_same_day_double_trigger_is_miss():
    e = mk_entry()
    o = P.track(e, [bar(D[1], 10.9, 9.4, 10.0)], n_elapsed=1)   # 同日 +9% 与 −6%
    assert o["type"] == "miss"


def test_hit_first_day_miss_later_stays_hit():
    e = mk_entry()
    o = P.track(e, [bar(D[1], 10.9, 9.9, 10.8)], n_elapsed=1)   # 第1日 hit → 立即结案
    assert o["type"] == "hit"


# ---------- 标注 ----------
def test_annotate_trade():
    e = mk_entry()
    P.annotate_trade(e, buy_date=D[1], buy_price=39.2)
    P.annotate_trade(e, sell_date=D[5], sell_price=41.0)
    assert e["trade"] == {"buy_date": D[1], "buy_price": 39.2, "sell_date": D[5], "sell_price": 41.0}


# ---------- 极值日期戳 ----------
def test_extreme_day_stamps_follow_refresh():
    e = mk_entry()
    bars = [bar(D[1], 10.2, 9.9, 10.1),      # day1: max_up 2%
            bar(D[2], 10.1, 9.7, 10.0),      # day2: min_dn 3%(不触发)
            bar(D[3], 10.5, 9.95, 10.4)]     # day3: max_up 刷新 5%
    P.track(e, bars, n_elapsed=3)
    assert e["max_up_day"] == 3 and e["max_up_date"] == D[2 + 1]
    assert e["min_dn_day"] == 2 and e["min_dn_date"] == D[1 + 1]
    P.track(e, bars, n_elapsed=3)             # 重跑 → 戳不动
    assert e["max_up_day"] == 3


def test_new_entry_extreme_fields_init_null():
    pool = {"version": 1, "pool": []}
    e = P.add_entry(pool, "000651", "", "", [], {}, 10.0, D[0], [D[0]], cfg_version())
    for k in ("max_up_day", "max_up_date", "min_dn_day", "min_dn_date"):
        assert e[k] is None
