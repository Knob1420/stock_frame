#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""indicators_conf.py —— 指标注册表(唯一扩展点)。
build_indicators.FUNCS 按本表驱动:名称 -> (实现函数, 参数, 输出列)。
加新指标:这里加一项 + build_indicators.py 写纯函数并登记进 FUNCS。"""

INDICATORS = {
    "macd": {"params": {"fast": 12, "slow": 26, "signal": 9},
             "columns": ["macd_dif", "macd_dea", "macd_hist"]},
    "rsi":  {"params": {"period": 14},
             "columns": ["rsi_14"]},
    "kdj":  {"params": {"n": 9, "k_p": 3, "d_p": 3},
             "columns": ["kdj_k", "kdj_d", "kdj_j"]},
    "ma":   {"params": {"windows": [5, 10, 20, 60, 120, 240]},
             "columns": ["ma5", "ma10", "ma20", "ma60", "ma120", "ma240"]},
    "vma":  {"params": {"windows": [5, 20]},
             "columns": ["vma5", "vma20"]},
    "boll": {"params": {"n": 20, "p": 2},
             "columns": ["boll_upper", "boll_mid", "boll_lower"]},
    "atr":  {"params": {"n": 14},
             "columns": ["atr14"]},
}
