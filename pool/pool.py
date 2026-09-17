# -*- coding: utf-8 -*-
"""pool.py —— pool.json 状态机:IO(备份/恢复)、入池校验、跟踪三判结案、标注(spec §5/§7)。"""
import json
import os
import shutil

from pool.cfg import CLOSE_CFG


class PoolCorruptError(RuntimeError):
    """pool.json 与 .bak 均损坏——停机报错,绝不静默清空案例库。"""


def load_pool(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"version": 1, "pool": []}
    except json.JSONDecodeError:
        bak = path + ".bak"
        if os.path.isfile(bak):
            try:
                with open(bak, encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                raise PoolCorruptError("pool.json 与 .bak 均损坏,无法恢复")
        raise PoolCorruptError("pool.json 损坏且无 .bak 可恢复")


def save_pool(pool, path):
    if os.path.isfile(path):
        shutil.copy2(path, path + ".bak")        # 备份上一版
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)                        # 原子替换


def watching(pool):
    return [e for e in pool["pool"] if e["status"] == "watching"]


# —— 状态机
def add_entry(pool, code, name, reason, reason_tags, snapshot, add_close,
              added, cand_dates, cfg_ver):
    """入池:仅限当日/前一交易日候选;watching 去重(spec §9)。"""
    for e in watching(pool):
        if e["code"] == code:
            raise ValueError("%s 已在 watching( %s 入池),拒绝重复" % (code, e["added"]))
    if added not in cand_dates:
        raise ValueError("%s 不在候选名单日期 %s 中(只认当日/昨日)" % (code, cand_dates))
    entry = {"code": code, "name": name, "added": added, "cfg_version": cfg_ver,
             "add_close": add_close, "reason": reason, "reason_tags": reason_tags,
             "snapshot": snapshot, "status": "watching",
             "max_up": 0.0, "min_dn": 0.0, "days": 0,
             "max_up_day": None, "max_up_date": None,
             "min_dn_day": None, "min_dn_date": None,
             "last_date": None, "last_close": None, "outcome": None, "trade": None}
    pool["pool"].append(entry)
    return entry


def _trigger(entry, cfg):
    """当前极值是否触发(每根 bar 后调用;同日双触发按 miss 保守)。
    阈值判定为"严格越过"并含 1e-9 浮点容差:恰在阈值上不触发
    (测试口径:lo=9.5/base=10 → −5.000000% 未触发;9.5/10−1 浮点为 −0.05000000000000004)。"""
    eps = 1e-9
    if entry["max_up"] >= cfg["hit"] + eps and entry["min_dn"] <= cfg["miss"] - eps:
        return "miss"
    if entry["max_up"] >= cfg["hit"] + eps:
        return "hit"
    if entry["min_dn"] <= cfg["miss"] - eps:
        return "miss"
    return None


def track(entry, bars, n_elapsed, cfg=CLOSE_CFG):
    """增量跟踪:bars 为 >last_date 的新K线(后复权);n_elapsed 为 T+1 起至今的
    市场交易日数(停牌照占,调用方从日历算)。触发即结案并返回 outcome;已结案返回 None。"""
    if entry.get("outcome") is not None:
        return None                                          # 冻结
    base = entry["add_close"]
    for d, hi, lo, c in bars:
        if d <= entry["added"] or (entry["last_date"] and d <= entry["last_date"]):
            continue                                         # 幂等:跳过已处理
        entry["days"] += 1                                   # 先计本日序号,再盖戳
        up, dn = hi / base - 1, lo / base - 1
        if up > entry["max_up"]:                             # 严格超越才刷新+盖戳
            entry["max_up"] = up
            entry["max_up_day"], entry["max_up_date"] = entry["days"], d
        if dn < entry["min_dn"]:
            entry["min_dn"] = dn
            entry["min_dn_day"], entry["min_dn_date"] = entry["days"], d
        entry["last_date"], entry["last_close"] = d, c
        t = _trigger(entry, cfg)
        if t:
            entry["outcome"] = {"type": t, "days": entry["days"],
                                "max_up": entry["max_up"], "min_dn": entry["min_dn"]}
            entry["status"] = "closed"
            return entry["outcome"]
    if n_elapsed >= cfg["window"]:                           # 窗口到期无触发
        entry["outcome"] = {"type": "flat", "days": n_elapsed,
                            "max_up": entry["max_up"], "min_dn": entry["min_dn"]}
        entry["status"] = "closed"
        return entry["outcome"]
    return None


def annotate_trade(entry, **kw):
    """可选买卖标注(现价口径,用户口头;None 值忽略)。"""
    entry["trade"] = {**(entry.get("trade") or {}), **{k: v for k, v in kw.items() if v is not None}}
