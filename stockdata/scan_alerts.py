#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan_alerts.py —— 个股条件预警(收盘后扫描):读 watch_rules.json → 逐票评估 → 状态机去重 → 报告 + toast。

  E:/Anaconda/envs/stock/python.exe scan_alerts.py            # 独立跑(规则改完想立刻验证)
  由 update_data.py 在数据换新成功后自动调用(--skip-alerts 可跳过)。
规则语义与状态机见 docs/superpowers/specs/2026-08-23-stock-alerts-design.md。"""
import glob
import json
import os
import subprocess
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
QLIB_URI = os.environ.get("QLIB_URI", "E:/quant/stockdata/qlib_bin")
IND_DIR = os.environ.get("IND_DIR", "E:/quant/stockdata/indicators")
RULES_PATH = os.path.join(HERE, "watch_rules.json")
STATE_PATH = os.path.join(HERE, "alert_state.json")
REPORT_DIR = os.path.join(HERE, "reports")
TOAST_PS1 = r"E:/quant/财报/toast.ps1"

DEFAULT_PARAMS = {"ma_touch": {"n": 240, "pct": 1.5}, "j_hook": {"threshold": 0.0}}
NEAR_MULT = 2.0          # dist ≤ 2×pct 且不在区 → "接近中"


# ---------- 纯函数(单测见 tests/test_scan_alerts.py) ----------
def ma_series(closes, n):
    """尾部简单移动平均;不足 n 根处为 None。"""
    out, s = [], 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= n:
            s -= closes[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def in_zone(close, ma, pct):
    return ma is not None and close is not None and abs(close / ma - 1) * 100 <= pct


def left_zone(close, ma, pct):
    """滞后离开:超过 1.5×pct 才算真正离开(防均线附近抖动)。"""
    return ma is not None and close is not None and abs(close / ma - 1) * 100 > 1.5 * pct


def j_cross_up(j_prev, j_cur, thr):
    return j_prev is not None and j_cur is not None and j_prev < thr <= j_cur


def eval_ma_touch(data, pct, prev, today):
    """data={"closes":[...], "mas":[...]}(后复权)。返回 {status, detail, state, near}。"""
    close, ma = data["closes"][-1], data["mas"][-1]
    if ma is None or close is None:
        return {"status": "skipped", "detail": "历史不足", "state": prev or {}, "near": False}
    dist = (close / ma - 1) * 100
    if in_zone(close, ma, pct):
        if prev and prev.get("in"):
            return {"status": "quiet", "detail": "区内(已报)", "state": prev, "near": False}
        return {"status": "triggered", "detail": "触及均线(距 %+.1f%%)" % dist,
                "state": {"in": True, "since": today, "last_alert": today}, "near": False}
    if prev and prev.get("in") and left_zone(close, ma, pct):
        return {"status": "reset", "detail": "离开区间(距 %+.1f%%)" % dist,
                "state": {"in": False}, "near": False}
    return {"status": "quiet", "detail": "距 %+.1f%%" % dist, "state": prev or {"in": False},
            "near": abs(dist) <= NEAR_MULT * pct}


def eval_j_hook(data, threshold, prev):
    """data={"j_prev","j_cur","date"}(kdj_j 末两根)。同一交叉日只报一次。"""
    jp, jc, d = data.get("j_prev"), data.get("j_cur"), data.get("date")
    if jp is None or jc is None:
        return {"status": "skipped", "detail": "J 数据缺失", "state": prev or {}}
    if j_cross_up(jp, jc, threshold) and (not prev or prev.get("last_alert") != d):
        return {"status": "triggered", "detail": "J 上穿%g(%.1f→%.1f)" % (threshold, jp, jc),
                "state": {"last_alert": d}}
    return {"status": "quiet", "detail": "J=%.1f(未上穿%g)" % (jc, threshold), "state": prev or {}}


# ---------- 主流程 ----------
def _sym(code):
    return ("sh" if code[0] == "6" else "bj" if code[0] in "48" or code.startswith("92") else "sz") + code


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def send_toast(title, body):
    """Windows 通知(复用 财报/toast.ps1);失败不影响扫描结果。"""
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        TOAST_PS1, "-Title", title, "-Body", body[:180]],
                       capture_output=True, timeout=20)
    except Exception as e:
        print("toast 失败(不影响报告):", e)


def _fetch_kline(D, code):
    """返回 (dates, closes 后复权, raw 末根真实价);取不到抛异常。"""
    sym = _sym(code)
    df = D.features([sym], ["$close", "$factor"], start_time="2018-01-01", disk_cache=0)
    s = df.xs(sym, level=0)
    pairs = [(str(t)[:10], float(c), float(f)) for t, c, f in zip(s.index, s["$close"], s["$factor"])
             if c == c and f and f == f]
    if not pairs:
        raise ValueError("无K线")
    return ([p[0] for p in pairs], [p[1] for p in pairs], pairs[-1][1] / pairs[-1][2])


def _write_report(today, rows):
    os.makedirs(REPORT_DIR, exist_ok=True)
    trig = [x for x in rows if x["status"] == "triggered"]
    near = [x for x in rows if x.get("near")]
    lines = ["# 盯盘预警 %s" % today, ""]
    lines.append("## 触发(%d)" % len(trig))
    lines += ["- **%s %s** %s(现价 %.2f)%s" % (x["name"], x["code"], x["detail"], x["raw"],
              " —— " + x["note"] if x.get("note") else "") for x in trig] or ["- 无"]
    lines += ["", "## 接近中(提前预热)"]
    lines += ["- %s %s %s(现价 %.2f)" % (x["name"], x["code"], x["detail"], x["raw"]) for x in near] or ["- 无"]
    lines += ["", "## 其他状态"]
    lines += ["- %s %s [%s] %s" % (x["name"], x["code"],
              {"quiet": "安静", "reset": "已离开", "skipped": "跳过"}.get(x["status"], x["status"]),
              x["detail"]) for x in rows if x["status"] != "triggered"] or ["- 无"]
    p = os.path.join(REPORT_DIR, "alerts_%s.md" % today)
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return p, trig


def scan():
    today = date.today().isoformat()
    if not os.path.isfile(RULES_PATH):
        print("无 watch_rules.json,预警跳过(首次使用请先写规则)")
        return 0
    try:
        rules = _load_json(RULES_PATH, None)
        if not isinstance(rules, list):
            raise ValueError
    except Exception as e:
        print("规则文件损坏(应为 JSON 数组):", e)
        return 2
    state = _load_json(STATE_PATH, {}) or {}
    import pandas as pd
    import qlib
    from qlib.data import D
    qlib.init(provider_uri=QLIB_URI, region="cn")
    rows = []
    for r in rules:
        code, rule = str(r.get("code", "")), r.get("rule", "")
        params = dict(DEFAULT_PARAMS.get(rule, {}))
        params.update(r.get("params") or {})
        for k, v in params.items():                 # 0 与 0.0 归一,防 state key 漂移
            if isinstance(v, float) and v.is_integer():
                params[k] = int(v)
        key = "%s|%s|%s" % (code, rule, ",".join("%s=%s" % kv for kv in sorted(params.items())))
        prev = state.get(key) or {}
        row = {"name": r.get("name") or code, "code": code, "note": r.get("note"), "status": "skipped",
               "detail": "", "state": prev, "raw": 0.0}
        try:
            dates, closes, raw = _fetch_kline(D, code)
            row["raw"] = raw
            if rule == "ma_touch":
                res = eval_ma_touch({"closes": closes, "mas": ma_series(closes, params["n"])},
                                    params["pct"], prev, today)
                res["detail"] = "MA%d %s" % (params["n"], res["detail"])
            elif rule == "j_hook":
                jp = jc = None
                p = os.path.join(IND_DIR, "%s.parquet" % _sym(code))
                if os.path.isfile(p):
                    j = pd.read_parquet(p, columns=["kdj_j"])["kdj_j"]
                    jm = {str(i): v for i, v in zip(j.index, j.values) if v == v}
                    jv = [jm.get(dt) for dt in dates[-2:]]
                    if len(jv) == 2:
                        jp, jc = jv
                res = eval_j_hook({"j_prev": jp, "j_cur": jc, "date": dates[-1]},
                                  params["threshold"], prev)
            else:
                res = {"status": "skipped", "detail": "未知规则 %s" % rule, "state": prev, "near": False}
            row.update(res)
        except Exception as e:
            row.update({"status": "skipped", "detail": "K线不可用(%s)" % e, "near": False})
        row["state"] = row.get("state") or prev
        state[key] = row["state"]
        rows.append(row)
    rep, trig = _write_report(today, rows)
    if trig:
        send_toast("盯盘预警 %d 条" % len(trig),
                   ";".join("%s %s %s" % (x["name"], x["code"], x["detail"]) for x in trig))
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    print("预警扫描:触发 %d / 共 %d 条规则 → %s" % (len(trig), len(rows), rep))
    return 0


if __name__ == "__main__":
    sys.exit(scan())
