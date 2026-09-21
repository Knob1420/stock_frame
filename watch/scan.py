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


def snapshot(df, params_ma=(60, 120, 240)) -> dict:
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
    return s


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
    tracking = [{"code": k.split("|")[0], "rule": k.split("|")[1:] and "|".join(k.split("|")[1:]),
                 "days": (pd.Timestamp.today() - pd.Timestamp(v[1])).days}
                for k, v in state.items() if v[0] and v[1] and
                (pd.Timestamp.today() - pd.Timestamp(v[1])).days > 0]
    if events:
        arc = pd.DataFrame(events)
        old = pd.read_parquet(ARCHIVE_F) if os.path.exists(ARCHIVE_F) else arc.iloc[:0]
        pd.concat([old, arc], ignore_index=True).to_parquet(ARCHIVE_F)
    report_txt = format_report(events, tracking)
    print(report_txt)
    return report_txt


def format_report(events, tracking=None) -> str:
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
                      for n in (60, 120, 240) if e.get("dist_ma%d" % n) is not None)
        thesis = "\n> thesis:%s" % e["thesis"] if e.get("thesis") else ""
        lines.append("\n**%s %s** 触发[%s]\n> 现价 %.2f | %s | 距52周高 %.1f%% 位置%.0f%% | 量比%.2f%s" % (
            e["code"], e["name"], "+".join(rules_by[e["code"]]), e["close"], ma,
            100 * e["dd_52w"], 100 * e["pos_52w"], e["volr"], thesis))
    if tracking:
        event_rules = {(e["code"], e["rule"]) for e in events}
        by = {}                                     # 同票合并一行;rstrip 去空 hint 的尾部 |
        for t in tracking:
            r = t["rule"].rstrip("|")
            norm = (r.replace("|", "(") + ")") if "|" in r else r   # near_ma|MA120 → near_ma(MA120)
            if (t["code"], norm) in event_rules:
                continue                            # 今日事件里已列过的不再重复
            d = by.setdefault(t["code"], {"days": t["days"], "rules": []})
            d["rules"].append(r)
        if by:
            lines.append("\n**跟踪中(钝化/持续状态,不推送):**")
            for code, d in sorted(by.items(), key=lambda kv: -kv[1]["days"]):
                lines.append("- %s [%s] 已持续 %d 天" % (code, ",".join(d["rules"]), d["days"]))
    return "\n".join(lines)


def _brief_once(facts):
    """单次 GLM 调用:一只票的事实文本 → 六段快报文本。"""
    import json as _j
    key = os.environ.get("GLM_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    prompt = ("你是盯盘助手。以下为该股今日触发的结构化事实(唯一信息来源,禁止编造数字)。"
              "输出六段:①支撑位列表(来源:均线/近20日低点/箱体配置) ②压力位列表(来源同上) "
              "③量价状态 ④当前位置评估 ⑤与thesis对照(当前是否符合低吸/关注的前置状态) ⑥风险提示。每段一行,简洁。\n\n" + facts)
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
    """每票一次调用;同票多规则合并;单票失败不影响其他票。"""
    key = os.environ.get("GLM_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if not key:
        return "(未设置 GLM_API_KEY(.env),跳过 LLM 快报)"
    seen, uniq = set(), []
    for e in events:                              # 同票多规则合并
        k = e["code"]
        if k not in seen:
            seen.add(k)
            uniq.append(e)
        else:
            for u in uniq:
                if u["code"] == k:
                    u["rule"] += "+" + e["rule"]
    outs = []
    for e in uniq:
        facts = "- %s %s 触发[%s] 现价%.2f 距MA60 %+.1f%% MA120 %+.1f%% MA240 %+.1f%% 距52周高 %.1f%% 52周位置%.0f%% 量比%.2f 近20日区间[%.2f,%.2f] thesis:%s" % (
            e["code"], e["name"], e["rule"], e["close"],
            100 * e["dist_ma60"], 100 * e["dist_ma120"], 100 * e["dist_ma240"],
            100 * e["dd_52w"], 100 * e["pos_52w"], e["volr"], e["lo20"], e["hi20"], e["thesis"])
        try:
            outs.append("**%s %s** 触发[%s]\n%s" % (e["code"], e["name"], e["rule"], _brief_once(facts)))
        except Exception as ex:
            outs.append("**%s %s** (该票分析失败: %s)" % (e["code"], e["name"], ex))
    return "\n\n".join(outs)


if __name__ == "__main__":
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--llm", action="store_true", help="触发事件追加大模型六段快报")
    a = ap.parse_args()
    report_txt = run(a.push)
    if a.llm and os.path.exists(ARCHIVE_F):
        arc = pd.read_parquet(ARCHIVE_F)
        todays = arc[arc["date"] == arc["date"].max()]
        if len(todays):
            rd = os.path.join(HERE, "reports")            # 本地报告先落盘,LLM 失败不丢底稿
            os.makedirs(rd, exist_ok=True)
            rp = os.path.join(rd, todays["date"].max() + ".md")
            with open(rp, "w", encoding="utf-8") as f:
                f.write(report_txt)
            try:
                brief = llm_brief(todays.to_dict("records"))
            except Exception as e:
                brief = "(LLM 快报生成失败: %s)" % e
            print("\n===== LLM 快报 =====\n" + brief)
            with open(rp, "a", encoding="utf-8") as f:
                f.write("\n\n===== LLM 快报 =====\n" + brief)
            if a.push:                                            # 推送=规则报告+LLM快报(失败也推,带标注)
                full = report_txt + "\n\n===== LLM 快报 =====\n" + brief
                hook = os.environ.get("WATCH_WEBHOOK", "")
                sckey = os.environ.get("SERVERCHAN_KEY", "")
                if hook:
                    chunks, buf = [], ""               # 按空行(票块)切分条,单票不拆两条消息
                    for blk in full.split("\n\n"):
                        if len(buf) + len(blk) + 2 > 3800:
                            chunks.append(buf)
                            buf = blk
                        else:
                            buf = ("%s\n\n%s" % (buf, blk)) if buf else blk
                    chunks.append(buf)
                    for c in chunks:                   # 企微 markdown 上限 4096 字节
                        data = json.dumps({"msgtype": "markdown", "markdown": {"content": c}}).encode()
                        urllib.request.urlopen(urllib.request.Request(
                            hook, data=data, headers={"Content-Type": "application/json"}), timeout=10)
                    print("已推送企微 %d 条(含LLM快报)" % len(chunks))
                elif sckey:
                    body = urllib.parse.urlencode({
                        "title": "盘后监控:%d 触发 + LLM快报(%s)" % (len(todays), todays["date"].max()),
                        "desp": full[:30000]}).encode()
                    urllib.request.urlopen(urllib.request.Request(
                        "https://sctapi.ftqq.com/%s.send" % sckey, data=body), timeout=15)
                    print("已推送 Server酱(含LLM快报)")
        elif a.push and os.environ.get("SERVERCHAN_KEY", ""):
            pass  # 无 LLM/无事件时不推(规则报告已在每日运行时打印+落盘)
