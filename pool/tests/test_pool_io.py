# -*- coding: utf-8 -*-
import json

import pytest

from pool import pool as P


def test_load_missing_returns_empty(tmp_path):
    p = tmp_path / "pool.json"
    assert P.load_pool(str(p)) == {"version": 1, "pool": []}


def test_save_creates_and_bak(tmp_path):
    p = str(tmp_path / "pool.json")
    d = {"version": 1, "pool": [{"code": "000651"}]}
    P.save_pool(d, p)
    assert P.load_pool(p) == d                      # 无 .bak 时首次写
    d["pool"].append({"code": "600941"})
    P.save_pool(d, p)
    assert json.load(open(p + ".bak"))["pool"] == [{"code": "000651"}]   # 备份的是上一版


def test_load_corrupt_falls_back_to_bak(tmp_path):
    p = tmp_path / "pool.json"
    P.save_pool({"version": 1, "pool": [{"code": "000651"}]}, str(p))
    P.save_pool({"version": 1, "pool": [{"code": "000651"}]}, str(p))   # 第二次写才生成 .bak
    open(p, "w").write("{broken!!")
    assert P.load_pool(str(p))["pool"] == [{"code": "000651"}]


def test_load_both_corrupt_raises(tmp_path):
    p = tmp_path / "pool.json"
    open(p, "w").write("{bad")
    open(str(p) + ".bak", "w").write("{bad too")
    with pytest.raises(P.PoolCorruptError):
        P.load_pool(str(p))
