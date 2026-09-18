# -*- coding: utf-8 -*-
"""pool/cfg.py —— 池的路径与口径常量(唯一配置点)。"""
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from kdj.layers import DEFAULT_CFG                     # noqa: E402(需先修 sys.path)

PARQ_DIR = "/home/admin/stockdata/indicators"   # 数据与代码分离:数据在仓库外(2026-09-18)
CALENDAR = "/home/admin/stockdata/qlib_bin/calendars/day.txt"
REPORT_DIR = os.path.join(ROOT, "pool", "reports")
POOL_PATH = os.path.join(ROOT, "pool", "pool.json")
LOOKUP_DIR = os.path.join(ROOT, "pool", "lookup_tables")

# 结案三判(spec §7):阈值同回测E3,窗口为池自己的10交易日口径
CLOSE_CFG = {"hit": 0.08, "miss": -0.05, "window": 10}


def cfg_version():
    """信号配置指纹:DEFAULT_CFG 内容 hash——公式换代自动隔离旧案例(spec §5)。"""
    h = hashlib.md5(json.dumps(DEFAULT_CFG, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
    return "kdj-default-" + h
