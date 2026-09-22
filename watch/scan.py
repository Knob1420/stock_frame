# -*- coding: utf-8 -*-
"""scan.py —— 盘后监控引擎:扫描自选股 → 规则触发(状态转换) → 快照归档 → 快报/推送。
数据:stockdata/indicators(每日 daily_update.sh 已刷新);配置:watchlist.yaml。
状态:watch/state.json(每票每规则的昨日条件);归档:watch/archive.parquet(研究管线标注前端)。
推送:企微群机器人 webhook,环境变量 WATCH_WEBHOOK;未设置只打印。
用法: python scan.py [--push]     # 挂在 daily_update.sh 尾部即为每日盘后17:00自动
"""
import argparse
import json
import os
import statistics
import sys
import urllib.parse
import urllib.request

import pandas as pd

def _load_env():
    """极简 .env 加载(KEY=VALUE 行,无引号处理够用);避免引入 python-dotenv。"""
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if os.path.exists(f):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from rules import RULES

IND = os.path.join(os.path.dirname(HERE), "stockdata", "indicators")
STATE_F = os.path.join(HERE, "state.json")
ARCHIVE_F = os.path.join(HERE, "archive.parquet")


def _r(x, nd=2):
    """NaN/缺列安全取整;次新股指标不足期时字段为 None,LLM 侧显示 N/A。"""
    if x is None or pd.isna(x):
        return None
    return round(float(x), nd)


def snapshot(df, params_ma=(120, 250, 360)) -> dict:
    """触发时刻特征快照(研究管线的标注字段,LLM 快报的结构化输入)。"""
    c = df["close"].iloc[-1]
    f = df["factor"].iloc[-1]
    s = {"close": round(float(c / f), 2)}                    # 现价口径(=软件可见价)
    for n in params_ma:
        m = df["close"].rolling(n, min_periods=n).mean().iloc[-1]
        s["dist_ma%d" % n] = None if pd.isna(m) else round(float(c / m - 1), 4)
    lo250 = df["low"].iloc[-250:]
    hi250 = df["high"].iloc[-250:]
    s["hi20"] = round(float(df["high"].iloc[-20:].max() / f), 2)   # 统一现价口径
    s["lo20"] = round(float(df["low"].iloc[-20:].min() / f), 2)
    s["dd_52w"] = round(float(c / hi250.max() - 1), 4)
    s["pos_52w"] = round(float((c - lo250.min()) / (hi250.max() - lo250.min())), 3)
    s["volr"] = round(float(df["volume"].iloc[-1] / df["vma20"].iloc[-1]), 3)
    # —— LLM 增强字段:均线/轨道绝对值(现价口径,免反推)+ 指标 + 近10日量价序列 ——
    for n in (20, 60, 120, 250, 360):                        # 250/360 无预计算列,统一滚动现算
        m = df["close"].rolling(n, min_periods=n).mean().iloc[-1]
        s["mav%d" % n] = _r(m / f)
    s["kdj"] = [_r(df[x].iloc[-1], 1) for x in ("kdj_k", "kdj_d", "kdj_j")]
    s["macd"] = [_r(df[x].iloc[-1] / f, 3) for x in ("macd_dif", "macd_dea", "macd_hist")]
    s["rsi14"] = _r(df["rsi_14"].iloc[-1], 1)
    s["boll"] = [_r(df[x].iloc[-1] / f) for x in ("boll_upper", "boll_mid", "boll_lower")]
    s["atr_pct"] = _r(df["atr14"].iloc[-1] / c * 100, 1)
    s["c10"] = [_r(x / f) for x in df["close"].iloc[-10:]]
    s["vr10"] = [_r(v / vm) if pd.notna(vm) and vm > 0 else None
                 for v, vm in zip(df["volume"].iloc[-10:], df["vma20"].iloc[-10:])]
    return s


_MKT_CACHE = {}


def _market_scan(ind_dir=IND, bench="sh000300"):
    """全市场宽度统计 + 基准指数 → dict(模块级缓存,一轮一份;LLM 文本与大盘图共用)。

    自建指标库全量扫描(约5500只,列裁剪读取约60s):涨跌家数、站上年线比例、
    中位量比 + 沪深300 近期涨跌/距年线。数据日以基准指数日历末为准,末行不同日
    的(北交所/退市残留,bj 段数据源不更新)不计入。任何一步失败都降级而非中断。
    """
    if _MKT_CACHE:
        return _MKT_CACHE
    try:                                                       # 基准指数先行:定数据日
        b = pd.read_parquet(os.path.join(ind_dir, "%s.parquet" % bench),
                            columns=["close"])
        d = str(b.index[-1])[:10]
    except Exception:
        b, d = None, None
    adv = dec = flat = above = total = 0
    vrs = []
    for fn in sorted(os.listdir(ind_dir)):
        if not fn.endswith(".parquet") or fn[:-8].startswith(("sh000", "sz39")):
            continue                                           # 指数/板块系列不计入个股统计
        try:
            df = pd.read_parquet(os.path.join(ind_dir, fn),
                                 columns=["close", "volume", "vma20"])
        except Exception:
            continue
        if len(df) < 2 or (d and str(df.index[-1])[:10] != d):
            continue
        last, prev = df["close"].iloc[-1], df["close"].iloc[-2]
        if last > prev:
            adv += 1
        elif last < prev:
            dec += 1
        else:
            flat += 1
        m = df["close"].rolling(250, min_periods=250).mean().iloc[-1]   # 年线=MA250,滚动现算
        if pd.notna(m):
            total += 1
            if last > m:
                above += 1
        vm = df["vma20"].iloc[-1]
        if pd.notna(vm) and vm > 0:
            vrs.append(float(df["volume"].iloc[-1] / vm))
    st = {"date": d or "?", "adv": adv, "dec": dec, "flat": flat,
          "above": above, "total": total,
          "med_vr": round(statistics.median(vrs), 2) if vrs else None,
          "bench_df": b}
    if b is not None and len(b) > 21:
        c = b["close"].iloc[-1]
        st["bench_d5"] = float(c / b["close"].iloc[-6] - 1)
        st["bench_d20"] = float(c / b["close"].iloc[-21] - 1)
        m = b["close"].rolling(250, min_periods=250).mean().iloc[-1]
        if pd.notna(m):
            st["bench_dist250"] = float(c / m - 1)
    _MKT_CACHE.update(st)
    return st


def market_context(ind_dir=IND):
    """大盘环境一行文本(LLM prompt 用),来自 _market_scan。"""
    st = _market_scan(ind_dir)
    parts = []
    if st["total"]:
        parts.append("涨%d/跌%d/平%d" % (st["adv"], st["dec"], st["flat"]))
        parts.append("站上年线(MA250) %d/%d 只(%.0f%%)"
                     % (st["above"], st["total"], 100.0 * st["above"] / st["total"]))
    if st["med_vr"] is not None:
        parts.append("中位量比%.2f" % st["med_vr"])
    if "bench_d5" in st:
        txt = "沪深300 近5日%+.1f%% 近20日%+.1f%%" % (100 * st["bench_d5"], 100 * st["bench_d20"])
        if "bench_dist250" in st:
            txt += " 距年线%+.1f%%" % (100 * st["bench_dist250"])
        parts.append(txt)
    if not parts:
        return "大盘环境:统计不可用"
    return "大盘环境(数据日%s,全市场%d只): %s" % (
        st["date"], st["adv"] + st["dec"] + st["flat"], "; ".join(parts))


def run(push=False):
    import yaml
    cfg = yaml.safe_load(open(os.path.join(HERE, "watchlist.yaml"), encoding="utf-8"))
    state = json.load(open(STATE_F, encoding="utf-8")) if os.path.exists(STATE_F) else {}
    events = []
    for it in cfg["watchlist"]:
        code = it["code"]
        p = os.path.join(IND, "%s.parquet" % code)
        if not os.path.exists(p):
            print("⚠ %s 无数据,跳过" % code)
            continue
        df = pd.read_parquet(p)
        df.index = pd.DatetimeIndex(df.index)
        for r in (cfg.get("defaults", {}).get("rules", []) + it.get("rules", [])):
            name, params = r["rule"], r.get("params", {})
            fn = RULES.get(name)
            if fn is None:
                print("⚠ 未知规则 %s" % name)
                continue
            try:
                active = bool(fn(df, params))
            except Exception as e:
                print("⚠ %s/%s 求值失败: %s" % (code, name, e))
                continue
            hint = ("MA%s" % params["period"]) if "period" in params else (
                "下沿%s" % params["lower"]) if "lower" in params else ""
            key = "%s|%s|%s" % (code, name, hint)
            prev = state.get(key)
            today = str(df.index[-1].date())
            if active:
                since = prev[1] if prev and prev[0] else today
                state[key] = [True, since]
            else:
                state[key] = [False, None]
            if active and not (prev and prev[0]):         # 状态转换才报警/推送
                events.append({"code": code, "name": it.get("name", ""), "rule": name + ("(%s)" % hint if hint else ""),
                               "date": str(df.index[-1].date()), "thesis": it.get("thesis", ""),
                               **snapshot(df)})
    json.dump(state, open(STATE_F, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    names = {it["code"]: it.get("name", "") for it in cfg["watchlist"]}
    tracking = [{"code": k.split("|")[0], "name": names.get(k.split("|")[0], ""),
                 "rule": k.split("|")[1:] and "|".join(k.split("|")[1:]),
                 "days": (pd.Timestamp.today() - pd.Timestamp(v[1])).days}
                for k, v in state.items() if v[0] and v[1] and
                (pd.Timestamp.today() - pd.Timestamp(v[1])).days > 0]
    if events:
        arc = pd.DataFrame(events)
        old = pd.read_parquet(ARCHIVE_F) if os.path.exists(ARCHIVE_F) else arc.iloc[:0]
        pd.concat([old, arc], ignore_index=True).to_parquet(ARCHIVE_F)
    return events, tracking


def format_report(events, tracking=None, briefs=None) -> str:
    if not events and not tracking:
        return "✅ 盘后扫描:无触发(%s)" % pd.Timestamp.today().date()
    lines = ["**盘后监控快报 %s**" % pd.Timestamp.today().date()]
    rules_by, order = {}, []                       # 同票多规则合并:快照只出一遍
    for e in events:
        if e["code"] not in rules_by:
            rules_by[e["code"]] = []
            order.append(e)
        rules_by[e["code"]].append(e["rule"])
    if events:
        lines.append("数据日 %s" % events[0]["date"])
    for e in order:
        ma = " ".join("MA%d %+.1f%%" % (n, 100 * e["dist_ma%d" % n])
                      for n in (120, 250, 360) if e.get("dist_ma%d" % n) is not None)
        thesis = "\n> thesis:%s" % e["thesis"] if e.get("thesis") else ""
        lines.append("\n**%s %s** 触发[%s]\n> 现价 %.2f | %s | 距52周高 %.1f%% 位置%.0f%% | 量比%.2f%s" % (
            e["code"], e["name"], "+".join(rules_by[e["code"]]), e["close"], ma,
            100 * e["dd_52w"], 100 * e["pos_52w"], e["volr"], thesis))
        b = (briefs or {}).get(e["code"])                 # LLM 解读紧跟该票事实,同块输出
        if b:
            lines[-1] += "\n" + "\n".join(
                seg.strip() for seg in b.split("\n\n") if seg.strip())
    if tracking:
        event_rules = {(e["code"], e["rule"]) for e in events}
        by = {}                                     # 同票合并一行;rstrip 去空 hint 的尾部 |
        for t in tracking:
            r = t["rule"].rstrip("|")
            norm = (r.replace("|", "(") + ")") if "|" in r else r   # near_ma|MA120 → near_ma(MA120)
            if (t["code"], norm) in event_rules:
                continue                            # 今日事件里已列过的不再重复
            d = by.setdefault(t["code"], {"days": t["days"], "name": t.get("name", ""), "rules": []})
            d["rules"].append(r)
        if by:
            lines.append("\n**跟踪中(钝化/持续状态,不推送):**")
            for code, d in sorted(by.items(), key=lambda kv: -kv[1]["days"]):
                lines.append("- %s %s [%s] 已持续 %d 天" % (code, d["name"], ",".join(d["rules"]), d["days"]))
    return "\n".join(lines)


def _f(v):
    """数值格式化;None(次新/数据不足)显示 N/A。"""
    return "N/A" if v is None else "%g" % v


def _stock_facts(e, rules_str):
    """单票 → 多行结构化事实块(现价口径绝对值,LLM 直接引用免反推)。"""
    ma = " ".join("MA%d=%s(%+.1f%%)" % (n, _f(e.get("mav%d" % n)),
                                        100 * (e["close"] / e["mav%d" % n] - 1))
                  for n in (20, 60, 120, 250, 360) if e.get("mav%d" % n))
    k, d, j = e.get("kdj") or (None, None, None)
    dif, dea, hist = e.get("macd") or (None, None, None)
    up, mid, low = e.get("boll") or (None, None, None)
    c10 = "[%s]" % ",".join(_f(x) for x in e.get("c10") or [])
    vr10 = "[%s]" % ",".join(_f(x) for x in e.get("vr10") or [])
    return ("- %s %s 触发[%s]\n"
            "  现价%.2f | %s | 距52周高 %+.1f%% 52周位置%.0f%% | 量比%.2f | 近20日区间[%.2f,%.2f]\n"
            "  指标: KDJ K=%s D=%s J=%s | RSI14=%s | MACD DIF=%s DEA=%s 柱=%s | BOLL 上%s/中%s/下%s | ATR=%s%%现价\n"
            "  近10日收盘%s 量比%s\n"
            "  thesis:%s") % (
        e["code"], e["name"], rules_str, e["close"], ma,
        100 * e["dd_52w"], 100 * e["pos_52w"], e["volr"], e["lo20"], e["hi20"],
        _f(k), _f(d), _f(j), _f(e.get("rsi14")), _f(dif), _f(dea), _f(hist),
        _f(up), _f(mid), _f(low), _f(e.get("atr_pct")), c10, vr10, e["thesis"])


def _brief_once(facts, market):
    """单次 GLM 调用:大盘环境 + 一只票的事实文本 → 六段快报文本。"""
    import json as _j
    key = os.environ.get("GLM_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    prompt = ("你是盯盘助手。以下是当日大盘环境与该股今日触发的结构化事实(唯一信息来源,禁止编造数字;"
              "MA/BOLL/MACD 均为现价口径绝对值,直接引用无需反推,N/A 表示数据不足)。\n"
              + market + "\n\n" + facts +
              "\n\n输出六段:①支撑位列表(来源:MA数值/BOLL下轨/近20日低点) ②压力位列表(来源:MA数值/BOLL上轨/近20日高点) "
              "③量价状态(结合KDJ/RSI/MACD与近10日量价序列) ④当前位置评估(必须结合大盘环境判断顺逆势) "
              "⑤与thesis对照(当前是否符合低吸/关注的前置状态) ⑥风险提示(大盘环境走弱时须点明)。每段一行,简洁。")
    body = _j.dumps({"model": os.environ.get("LLM_MODEL", "glm-5.3"),
                     "max_tokens": 2000,
                     "thinking": {"type": "disabled"},   # 快报是数字翻译非推理,关思考 7 倍提速
                     "messages": [{"role": "user", "content": prompt}]}).encode()
    base = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/anthropic")
    req = urllib.request.Request(base.rstrip("/") + "/v1/messages", data=body, headers={
        "Content-Type": "application/json",
        "x-api-key": key, "anthropic-version": "2023-06-01"})
    r = _j.loads(urllib.request.urlopen(req, timeout=120).read())
    return "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text").strip()


def llm_brief(events):
    """每票一次调用;返回 {code: 六段解读};单票失败不影响其他票。"""
    key = os.environ.get("GLM_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if not key:
        return {}
    merged = {}                                        # code -> 合并后的规则串
    for e in events:
        merged[e["code"]] = (merged[e["code"]] + "+" + e["rule"]) if e["code"] in merged else e["rule"]
    outs = {}
    first = {}                                         # code -> 该票首条事件(快照字段相同)
    for e in events:
        first.setdefault(e["code"], e)
    market = market_context()                          # 全市场统计一轮一份,各票共用
    for code, e in first.items():
        try:
            outs[code] = _brief_once(_stock_facts(e, merged[code]), market)
        except Exception as ex:
            outs[code] = "(该票分析失败: %s)" % ex
    return outs


BRIEFS_DIR = os.path.join(HERE, "reports", "briefs")


def save_briefs(date, briefs, events, out_dir=None):
    """LLM 六段快报结构化落盘(层4命中率的物理前提);重复运行同日覆盖(幂等)。"""
    if not briefs:
        return None
    d = out_dir or BRIEFS_DIR
    os.makedirs(d, exist_ok=True)
    first, merged = {}, {}
    for e in events:
        first.setdefault(e["code"], e)
        merged[e["code"]] = (merged[e["code"]] + "+" + e["rule"]) if e["code"] in merged else e["rule"]
    payload = {"date": date, "briefs": {
        code: {"rules": merged.get(code, ""), "close": first[code]["close"],
               "thesis": first[code].get("thesis", ""), "brief": txt}
        for code, txt in briefs.items()}}
    p = os.path.join(d, "%s.json" % date)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return p


if __name__ == "__main__":
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--llm", action="store_true", help="每票事实后直接追加 LLM 六段解读")
    a = ap.parse_args()
    events, tracking = run(a.push)
    briefs = llm_brief(events) if (a.llm and events) else {}
    report_txt = format_report(events, tracking, briefs)
    print(report_txt)
    if a.llm and events:                                # 落盘一份完整版(事实+解读)
        rd = os.path.join(HERE, "reports")
        os.makedirs(rd, exist_ok=True)
        with open(os.path.join(rd, events[0]["date"] + ".md"), "w", encoding="utf-8") as f:
            f.write(report_txt)
        save_briefs(events[0]["date"], briefs, events)
    # 配图:大盘总览一张 + 每票一张(有事件才生成;单图失败不阻断)
    charts = {}
    if events:
        try:
            import chart as _chart
            cdir = os.path.join(HERE, "reports", "charts", events[0]["date"])
            os.makedirs(cdir, exist_ok=True)
            st = _market_scan()
            if st["bench_df"] is not None:
                charts["market"] = _chart.market_chart(
                    st["bench_df"], st, os.path.join(cdir, "market.png"))
            merged, first = {}, {}
            for e in events:
                merged[e["code"]] = (merged[e["code"]] + "+" + e["rule"]) if e["code"] in merged else e["rule"]
                first.setdefault(e["code"], e)
            for code, e in first.items():
                try:
                    df = pd.read_parquet(os.path.join(IND, "%s.parquet" % code))
                    df.index = pd.DatetimeIndex(df.index)
                    charts[code] = _chart.stock_chart(
                        df, code, e["name"], merged[code], e["close"],
                        os.path.join(cdir, "%s.png" % code))
                except Exception as ex:
                    print("⚠ %s 配图失败: %s" % (code, ex))
        except Exception as ex:
            print("⚠ 大盘图失败: %s" % ex)

    if a.push and events:
        hook = os.environ.get("WATCH_WEBHOOK", "")
        sckey = os.environ.get("SERVERCHAN_KEY", "")
        if hook:
            import base64
            import hashlib
            import re
            import time as _t

            def _send(payload):
                urllib.request.urlopen(urllib.request.Request(
                    hook, data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"}), timeout=10)
                _t.sleep(3.1)                          # 企微机器人限 20 条/分钟

            def _img(p):
                b = open(p, "rb").read()
                return {"msgtype": "image",
                        "image": {"base64": base64.b64encode(b).decode(),
                                  "md5": hashlib.md5(b).hexdigest()}}

            n_img = 0
            blocks = [b.strip() for b in report_txt.split("\n\n") if b.strip()]
            for i, blk in enumerate(blocks):            # 企微 markdown 上限 4096 字节
                for j in range(0, len(blk), 3800):
                    _send({"msgtype": "markdown", "markdown": {"content": blk[j:j + 3800]}})
                if i == 0 and "market" in charts:      # 头部块后跟大盘总览图
                    _send(_img(charts["market"]))
                    n_img += 1
                m = re.match(r"\*\*(sh\d{6}|sz\d{6}|bj\d{6})", blk)
                if m and m.group(1) in charts:          # 各票文字块后跟该票图
                    _send(_img(charts[m.group(1)]))
                    n_img += 1
            print("已推送企微: %d 个文本块 + %d 张图" % (len(blocks), n_img))
        elif sckey:
            body = urllib.parse.urlencode({
                "title": "盘后监控:%d 触发(%s)" % (len(briefs), events[0]["date"]),
                "desp": report_txt[:30000]}).encode()
            urllib.request.urlopen(urllib.request.Request(
                "https://sctapi.ftqq.com/%s.send" % sckey, data=body), timeout=15)
            print("已推送 Server酱(含LLM解读)")
