# -*- coding: utf-8 -*-
"""盘后 watchlist：规则扫描、降噪、结构化解释与推送。

流程：规则产生状态；同股信号合并评分；经过冷却和数量上限后提醒；
大模型只解释入选提醒，不参与打分和定价。
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from copy import deepcopy
from pathlib import Path

import pandas as pd


def _load_env():
    """加载项目 .env，只填补尚未存在的环境变量。"""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for path in (HERE, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:  # 兼容脚本运行和模块导入
    from .rules import RULE_META, RULES
except ImportError:
    from rules import RULE_META, RULES

IND = ROOT / "stockdata" / "indicators"
CONFIG_F = HERE / "watchlist.yaml"
STATE_F = HERE / "state.json"
ARCHIVE_F = HERE / "archive.parquet"
STATE_SCHEMA = 2

DEFAULT_ALERTING = {
    "min_score": 4,
    "min_categories": 2,
    "cooldown_bars": 10,
    "cooldown_override_score": 2,
    "max_push_stocks": 5,
    "llm_max_stocks": 5,
}
PRIORITY_BONUS = {"high": 1, "normal": 0, "low": -1}
PRIORITY_ORDER = {"high": 2, "normal": 1, "low": 0}
LLM_STATES = {"仅进入观察", "等待确认", "确认增强", "风险优先"}


def _number(value, digits=4):
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def snapshot(df, params_ma=(5, 20, 60, 120, 240)) -> dict:
    """生成统一为行情软件可见价的事实快照。"""
    close = float(df["close"].iloc[-1])
    factor = float(df["factor"].iloc[-1])
    previous = float(df["close"].iloc[-2]) if len(df) > 1 else close
    result = {
        "close": round(close / factor, 2),
        "pct1": _number(close / previous - 1, 4),
        "ret5": _number(close / df["close"].iloc[-6] - 1, 4) if len(df) > 5 else None,
        "ret20": _number(close / df["close"].iloc[-21] - 1, 4) if len(df) > 20 else None,
        "kdj_j": _number(df["kdj_j"].iloc[-1], 2) if "kdj_j" in df else None,
    }
    for period in params_ma:
        ma = df["close"].rolling(period, min_periods=period).mean()
        current = ma.iloc[-1]
        result["ma%d" % period] = _number(current / factor, 2)
        result["dist_ma%d" % period] = _number(close / current - 1, 4)
        result["slope_ma%d_5" % period] = (
            _number(current / ma.iloc[-6] - 1, 4)
            if len(ma) > 5 and pd.notna(current) and pd.notna(ma.iloc[-6]) else None
        )
    prior = df.iloc[:-1] if len(df) > 1 else df
    low250 = prior["low"].iloc[-250:]
    high250 = prior["high"].iloc[-250:]
    price_range = high250.max() - low250.min()
    result.update({
        "prior_hi20": _number(prior["high"].iloc[-20:].max() / factor, 2),
        "prior_lo20": _number(prior["low"].iloc[-20:].min() / factor, 2),
        "prior_hi60": _number(prior["high"].iloc[-60:].max() / factor, 2),
        "dd_52w": _number(close / high250.max() - 1, 4),
        "pos_52w": _number((close - low250.min()) / price_range, 3)
        if price_range else None,
    })
    vma20 = df["volume"].rolling(20, min_periods=20).mean().iloc[-1]
    result["volr"] = _number(df["volume"].iloc[-1] / vma20, 3)
    result["atr_pct"] = (
        _number(df["atr14"].iloc[-1] / close, 4) if "atr14" in df else None
    )
    return result


def _rule_hint(name, params):
    if name in {"near_ma", "ma_reclaim"}:
        return "MA%s" % params.get("period", 20)
    if name in {"box_touch_lower", "box_breakdown"}:
        return "下沿%s" % params.get("lower")
    if name == "breakout":
        return "%s日" % params.get("period", 20)
    return ""


def _rule_descriptor(spec):
    name = spec["rule"]
    meta = {**RULE_META.get(name, {})}
    for key in ("label", "category", "weight", "standalone", "kind"):
        if key in spec:
            meta[key] = spec[key]
    params = spec.get("params", {}) or {}
    hint = spec.get("id") or _rule_hint(name, params)
    label = meta.get("label", name) + (("(%s)" % hint) if hint else "")
    return {
        "rule": name,
        "hint": hint,
        "label": label,
        "category": meta.get("category", name),
        "weight": int(meta.get("weight", 1)),
        "standalone": bool(meta.get("standalone", False)),
        "kind": meta.get("kind", "watch"),
        "notify": bool(spec.get("notify", True)),
        "params": params,
    }


def _state_key(code, descriptor):
    return "%s|%s|%s" % (code, descriptor["rule"], descriptor["hint"])


def _load_state(path):
    """读取 v2 状态；旧版 key/list 状态会自动迁移并静默建基线。"""
    path = Path(path)
    if not path.exists():
        return {"schema_version": STATE_SCHEMA, "rules": {}, "stocks": {}}, True
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("schema_version") == STATE_SCHEMA:
        raw.setdefault("rules", {})
        raw.setdefault("stocks", {})
        return raw, False
    migrated = {"schema_version": STATE_SCHEMA, "rules": {}, "stocks": {}}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, list) and len(value) >= 2:
                migrated["rules"][key] = {
                    "active": bool(value[0]),
                    "since": value[1],
                    "last_transition": value[1],
                }
    return migrated, True


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def _score(active_rules, priority="normal"):
    """同类别只取最高权重，防止多条均线重复加分。"""
    categories = {}
    for rule in active_rules:
        categories[rule["category"]] = max(
            categories.get(rule["category"], 0), rule["weight"]
        )
    return sum(categories.values()) + PRIORITY_BONUS.get(priority, 0), categories


def _score_components(active_rules, priority="normal"):
    """返回真正计入总分的规则，供消息解释；被同类高分覆盖的规则不展示。"""
    best = {}
    for rule in active_rules:
        current = best.get(rule["category"])
        if current is None or rule["weight"] > current["weight"]:
            best[rule["category"]] = rule
    components = [
        {"label": rule.get("label", category), "weight": rule["weight"]}
        for category, rule in best.items()
    ]
    bonus = PRIORITY_BONUS.get(priority, 0)
    if bonus:
        components.append({
            "label": "个股高优先级" if bonus > 0 else "个股低优先级",
            "weight": bonus,
        })
    return components


def _trading_bars_since(df, date):
    if not date:
        return 10 ** 9
    return int(
        (pd.DatetimeIndex(df.index).normalize() > pd.Timestamp(date).normalize()).sum()
    )


def _merge_levels(levels):
    merged = []
    valid = (x for x in levels if x.get("value") is not None)
    for level in sorted(valid, key=lambda x: x["value"]):
        if merged and abs(level["value"] / merged[-1]["value"] - 1) <= 0.005:
            merged[-1]["source"] += "/" + level["source"]
        else:
            merged.append(dict(level))
    return merged


def _key_levels(snap, rule_specs):
    close = snap["close"]
    levels = []
    for period in (5, 20, 60, 120, 240):
        value = snap.get("ma%d" % period)
        if value is not None:
            levels.append({"source": "MA%d" % period, "value": value})
    for source, field in (
        ("前20日低点", "prior_lo20"),
        ("前20日高点", "prior_hi20"),
        ("前60日高点", "prior_hi60"),
    ):
        if snap.get(field) is not None:
            levels.append({"source": source, "value": snap[field]})
    for spec in rule_specs:
        params = spec.get("params", {}) or {}
        if "lower" in params:
            levels.append({"source": "人工箱体下沿", "value": float(params["lower"])})
        if "upper" in params:
            levels.append({"source": "人工箱体上沿", "value": float(params["upper"])})
    levels = _merge_levels(levels)
    supports = sorted(
        (x for x in levels if close * 0.85 <= x["value"] <= close),
        key=lambda x: abs(x["value"] / close - 1),
    )[:2]
    resistance = sorted(
        (x for x in levels if close < x["value"] <= close * 1.30),
        key=lambda x: abs(x["value"] / close - 1),
    )[:2]
    return {"support": supports, "resistance": resistance}


def _plan_for(event):
    names = {x["rule"] for x in event["new_rules"]}
    if event["kind"] == "risk":
        status = "风险条件已经触发，先检查原关注逻辑是否仍成立。"
        watch_for = "等待收盘重新站回失守价位，再重新评估；当前不把下跌本身当成低吸理由。"
    elif "b1_hook" in names:
        status = "B1 已从低位出现首次拐头，进入确认阶段。"
        watch_for = "观察后续收盘能否守住参考支撑并站稳 MA5，避免单日拐头后继续走弱。"
    elif "breakout" in names:
        status = "价格与成交量同时越过前高，进入突破后的确认阶段。"
        watch_for = "观察突破位能否守住；放量冲高后又收回前高下方，则不追。"
    elif "box_touch_lower" in names:
        status = "股价回到人工箱体下沿附近，已经到达原计划观察区。"
        watch_for = "观察下沿能否持续守住，并等待收盘止跌或重新转强。"
    else:
        status = "多个观察条件同时成立，但还不是自动买入指令。"
        watch_for = "等待 J 值低位拐头，且收盘站稳 MA5；确认前只跟踪，不因超跌直接买入。"
    if event.get("thesis"):
        status += " 原关注逻辑：%s。" % event["thesis"]
    custom_lower = [
        float(x["params"]["lower"]) for x in event["rule_specs"]
        if "lower" in (x.get("params", {}) or {})
    ]
    if custom_lower:
        invalidation = "收盘跌破人工箱体下沿 %.2f 时，原箱体观察逻辑失效。" % custom_lower[0]
    elif event.get("prior_lo20") is not None:
        invalidation = "收盘跌破前20日低点 %.2f 时，本次止跌观察逻辑失效。" % event["prior_lo20"]
    else:
        invalidation = "当前缺少可复核的失效位，继续等待，不据此行动。"
    return {
        "status": status,
        "watch_for": watch_for,
        "invalidation": invalidation,
        "support": event["levels"]["support"],
        "resistance": event["levels"]["resistance"],
    }


def _tier(event):
    if event["kind"] == "risk":
        return "风险提醒"
    if any(x["standalone"] for x in event["new_rules"]) or event["score"] >= 6:
        return "重点观察"
    return "组合观察"


def _archive(events, path):
    if not events:
        return
    rows = []
    for event in events:
        row = {
            key: value for key, value in event.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        row.update({
            "rule": "+".join(x["label"] for x in event["new_rules"]),
            "active_rule": "+".join(x["label"] for x in event["active_rules"]),
            "signature": "+".join(
                sorted(x["rule"] + ":" + x["hint"] for x in event["new_rules"])
            ),
        })
        rows.append(row)
    frame = pd.DataFrame(rows)
    path = Path(path)
    if path.exists():
        old = pd.read_parquet(path)
        if all(column in old.columns for column in ("date", "code", "signature")):
            existing = {
                (str(row.date), str(row.code), str(row.signature))
                for row in old[["date", "code", "signature"]].itertuples(index=False)
                if pd.notna(row.signature)
            }
            keep = [
                (str(row.date), str(row.code), str(row.signature)) not in existing
                for row in frame[["date", "code", "signature"]].itertuples(index=False)
            ]
            frame = frame.loc[keep]
        frame = pd.concat([old, frame], ignore_index=True, sort=False)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(temporary, index=False)
    os.replace(temporary, path)


def run(
    *,
    config_path=CONFIG_F,
    state_path=STATE_F,
    archive_path=ARCHIVE_F,
    indicator_dir=IND,
    persist=True,
    preview=False,
    bootstrap_alerts=False,
):
    """扫描一次；preview 模式把当前活跃条件视为新变化，但不写任何文件。"""
    import yaml

    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    defaults = config.get("defaults", {}) or {}
    alerting = {**DEFAULT_ALERTING, **(defaults.get("alerting", {}) or {})}
    state, bootstrapping = _load_state(state_path)
    working = deepcopy(state)
    if preview:
        working = {"schema_version": STATE_SCHEMA, "rules": {}, "stocks": {}}
        bootstrapping = False

    candidates = []
    threshold_suppressed = []
    cooldown_suppressed = []
    errors = []
    evaluated = active_conditions = transitions = 0
    latest_dates = []
    seen_codes = set()

    for item in config.get("watchlist", []):
        if item.get("enabled", True) is False:
            continue
        code = item["code"]
        if code in seen_codes:
            errors.append("%s 在 watchlist 中重复，已忽略后一个" % code)
            continue
        seen_codes.add(code)
        data_path = Path(indicator_dir) / (code + ".parquet")
        if not data_path.exists():
            errors.append("%s 无指标数据" % code)
            continue
        try:
            df = pd.read_parquet(data_path)
            df.index = pd.DatetimeIndex(df.index)
            if df.empty:
                raise ValueError("数据为空")
        except Exception as exc:
            errors.append("%s 读取失败：%s" % (code, exc))
            continue
        evaluated += 1
        today = str(df.index[-1].date())
        latest_dates.append(today)
        rules = (
            [] if item.get("inherit_defaults", True) is False
            else list(defaults.get("rules", []))
        )
        rules += list(item.get("rules", []))
        active_rules, new_rules = [], []

        for spec in rules:
            name = spec.get("rule")
            fn = RULES.get(name)
            if fn is None:
                errors.append("%s 使用未知规则 %s" % (code, name))
                continue
            descriptor = _rule_descriptor(spec)
            key = _state_key(code, descriptor)
            previous = working["rules"].get(key, {})
            try:
                active = bool(fn(df, descriptor["params"]))
            except Exception as exc:
                errors.append("%s/%s 求值失败：%s" % (code, name, exc))
                continue
            was_active = bool(previous.get("active", False))
            since = previous.get("since") if active and was_active else (
                today if active else None
            )
            transition = active != was_active
            working["rules"][key] = {
                "active": active,
                "since": since,
                "last_transition": (
                    today if transition else previous.get("last_transition")
                ),
            }
            if active:
                active_conditions += 1
                active_rules.append(descriptor)
            if active and not was_active and descriptor["notify"]:
                transitions += 1
                new_rules.append(descriptor)

        if not new_rules:
            continue
        priority = item.get("priority", "normal")
        score, categories = _score(active_rules, priority)
        standalone = any(x["standalone"] for x in new_rules)
        item_alerting = item.get("alerting", {}) or {}
        minimum = int(item_alerting.get("min_score", alerting["min_score"]))
        min_categories = int(
            item_alerting.get("min_categories", alerting["min_categories"])
        )
        if not standalone and (
            score < minimum or len(categories) < min_categories
        ):
            threshold_suppressed.append({"code": code, "score": score})
            continue

        snap = snapshot(df)
        event = {
            "code": code,
            "name": item.get("name", ""),
            "date": today,
            "priority": priority,
            "thesis": item.get("thesis", ""),
            "score": score,
            "categories": sorted(categories),
            "score_components": _score_components(active_rules, priority),
            "new_rules": new_rules,
            "active_rules": active_rules,
            "rule_specs": rules,
            "kind": (
                "risk" if any(x["kind"] == "risk" for x in new_rules) else "watch"
            ),
            **snap,
        }
        event["levels"] = _key_levels(snap, rules)
        event["tier"] = _tier(event)
        event["plan"] = _plan_for(event)

        stock_state = working["stocks"].get(code, {})
        cooldown = int(
            item_alerting.get("cooldown_bars", alerting["cooldown_bars"])
        )
        bars_since = _trading_bars_since(df, stock_state.get("last_alert_date"))
        score_jump = score - int(stock_state.get("last_alert_score", -99))
        override = int(alerting["cooldown_override_score"])
        if (
            not preview
            and bars_since < cooldown
            and event["kind"] != "risk"
            and score_jump < override
        ):
            event["cooldown_remaining"] = cooldown - bars_since
            cooldown_suppressed.append(event)
            continue
        candidates.append(event)

    freshest = max(latest_dates) if latest_dates else None
    stale = [event for event in candidates if event["date"] != freshest]
    candidates = [event for event in candidates if event["date"] == freshest]
    stale_cooldown = [
        event for event in cooldown_suppressed if event["date"] != freshest
    ]
    cooldown_suppressed = [
        event for event in cooldown_suppressed if event["date"] == freshest
    ]
    errors += [
        "%s 数据日 %s 落后于 %s，未提醒" % (x["code"], x["date"], freshest)
        for x in stale + stale_cooldown
    ]
    candidates.sort(
        key=lambda x: (
            x["kind"] == "risk",
            PRIORITY_ORDER.get(x["priority"], 1),
            x["score"],
            -abs(x.get("dist_ma20") or 0),
            x["code"],
        ),
        reverse=True,
    )
    max_push = int(alerting["max_push_stocks"])
    selected, cap_suppressed = candidates[:max_push], candidates[max_push:]

    silent_bootstrap = bootstrapping and not bootstrap_alerts and not preview
    if silent_bootstrap:
        cap_suppressed = selected + cap_suppressed
        selected = []

    if not silent_bootstrap:
        for event in selected:
            event["delivery_status"] = "selected"
        for event in cap_suppressed:
            event["delivery_status"] = "cap_suppressed"
        for event in cooldown_suppressed:
            event["delivery_status"] = "cooldown_suppressed"

    for event in selected:
        working["stocks"][event["code"]] = {
            "last_alert_date": event["date"],
            "last_alert_score": event["score"],
            "last_tier": event["tier"],
        }
    working["last_data_date"] = freshest
    if persist and not preview:
        _atomic_json(state_path, working)
        if not silent_bootstrap:
            _archive(selected + cap_suppressed + cooldown_suppressed, archive_path)

    return {
        "date": freshest,
        "events": selected,
        "qualified": candidates,
        "suppressed": {
            "threshold": threshold_suppressed,
            "cooldown": cooldown_suppressed,
            "cap": cap_suppressed,
        },
        "stats": {
            "watchlist": len(config.get("watchlist", [])),
            "evaluated": evaluated,
            "active_conditions": active_conditions,
            "new_transitions": transitions,
            "qualified": len(candidates),
            "selected": len(selected),
            "threshold_suppressed": len(threshold_suppressed),
            "cooldown_suppressed": len(cooldown_suppressed),
            "cap_suppressed": len(cap_suppressed),
        },
        "bootstrap": bool(silent_bootstrap),
        "preview": preview,
        "errors": errors,
        "alerting": alerting,
    }


def _format_level_list(levels):
    if not levels:
        return "暂无近距离可复核价位"
    return "；".join(
        "%s %.2f" % (x["source"], x["value"]) for x in levels
    )


def format_report(result, briefs=None) -> str:
    """生成适合手机阅读的 Markdown，不展开全部持续状态。"""
    briefs = briefs or {}
    stats = result["stats"]
    date = result.get("date") or str(pd.Timestamp.today().date())
    if not result["events"]:
        if result.get("bootstrap"):
            return (
                "✅ watchlist 已建立新提醒基线（数据日 %s），本次不推送旧状态。"
                "后续只提醒新变化。" % date
            )
        return (
            "✅ 盘后 watchlist：无须提醒（数据日 %s）\n"
            "活跃条件 %d｜新变化 %d｜门槛过滤 %d｜冷却过滤 %d"
            % (
                date,
                stats["active_conditions"],
                stats["new_transitions"],
                stats["threshold_suppressed"],
                stats["cooldown_suppressed"],
            )
        )

    lines = [
        "# 盘后观察 · %s" % date,
        "提醒 %d 只｜组合门槛过滤 %d｜冷却过滤 %d｜数量上限略过 %d"
        % (
            stats["selected"],
            stats["threshold_suppressed"],
            stats["cooldown_suppressed"],
            stats["cap_suppressed"],
        ),
    ]
    icons = {"风险提醒": "🔴", "重点观察": "🟠", "组合观察": "🟡"}
    for index, event in enumerate(result["events"], 1):
        plan = event["plan"]
        lines += [
            "",
            "## %s %d. %s %s｜%s"
            % (
                icons.get(event["tier"], "🟡"),
                index,
                event["name"],
                event["code"],
                event["tier"],
            ),
            "**触发：** %s"
            % " + ".join(x["label"] for x in event["new_rules"]),
            "**快照：** 现价 %.2f｜日涨跌 %+.2f%%｜J %s｜量比 %s｜提醒分 %d"
            % (
                event["close"],
                100 * (event.get("pct1") or 0),
                "--"
                if event.get("kdj_j") is None
                else "%.1f" % event["kdj_j"],
                "--"
                if event.get("volr") is None
                else "%.2f" % event["volr"],
                event["score"],
            ),
            "**分数组成：** %s"
            % " + ".join(
                "%s %s%d" % (
                    part["label"],
                    "+" if part["weight"] >= 0 else "",
                    part["weight"],
                )
                for part in event["score_components"]
            ),
            "**当前状态：** %s" % plan["status"],
            "**支撑参考：** %s" % _format_level_list(plan["support"]),
            "**压力参考：** %s" % _format_level_list(plan["resistance"]),
            "**后续确认：** %s" % plan["watch_for"],
            "**逻辑失效：** %s" % plan["invalidation"],
        ]
        brief = briefs.get(event["code"])
        if brief:
            lines.append(
                "**模型判断：** %s｜%s" % (brief["state"], brief["summary"])
            )
            if brief.get("supporting_evidence"):
                lines.append(
                    "**支持证据：** %s"
                    % "；".join(brief["supporting_evidence"])
                )
            if brief.get("contrary_evidence"):
                lines.append(
                    "**反面证据：** %s"
                    % "；".join(brief["contrary_evidence"])
                )
            if brief.get("thesis_check"):
                lines.append("**原逻辑对照：** %s" % brief["thesis_check"])
            if brief.get("watch_for"):
                lines.append(
                    "**模型建议继续看：** %s"
                    % "；".join(brief["watch_for"])
                )
            if brief.get("risk"):
                lines.append("**模型提示：** %s" % brief["risk"])
    cap = result["suppressed"].get("cap", [])
    if cap:
        shown = cap[:5]
        summary = "、".join(
            "%s(%d分)" % (event["name"], event["score"]) for event in shown
        )
        if len(cap) > len(shown):
            summary += "，另有%d只" % (len(cap) - len(shown))
        lines += [
            "",
            "**候补观察：** %s" % summary,
            (
                "> 预览模式不写归档；正式扫描会归档候补。"
                if result.get("preview")
                else "> 候补已归档但不展开；原条件持续不会补发，出现更强的新信号时会重新参与排序。"
            ),
        ]
    lines += [
        "",
        "> 价位来自均线、前期高低点或 watchlist 人工箱体；"
        "模型不参与筛选和定价。这些是观察条件，不代表已验证买点。",
    ]
    return "\n".join(lines)


def _analysis_payload(event):
    return {
        "code": event["code"],
        "name": event["name"],
        "thesis": event.get("thesis") or "未填写",
        "reminder_tier": event["tier"],
        "reminder_score_components": event["score_components"],
        "trigger": [x["label"] for x in event["new_rules"]],
        "active_context": [x["label"] for x in event["active_rules"]],
        "facts": {
            key: event.get(key)
            for key in (
                "pct1",
                "ret5",
                "ret20",
                "kdj_j",
                "volr",
                "atr_pct",
                "dist_ma20",
                "dist_ma60",
                "dist_ma120",
                "dist_ma240",
                "dd_52w",
                "pos_52w",
            )
        },
        "program_plan": {
            "status": event["plan"]["status"],
            "watch_for": event["plan"]["watch_for"],
            "invalidation": event["plan"]["invalidation"],
        },
    }


def _parse_briefs(text, expected_codes):
    text = text.strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        text = text.removeprefix(fence + "json").removeprefix(fence)
        text = text.removesuffix(fence).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("模型未返回 JSON")
    raw = json.loads(text[start : end + 1])
    output = {}
    for item in raw.get("items", []):
        code = item.get("code")
        if code not in expected_codes:
            continue
        state = str(item.get("state", "")).strip()
        if state not in LLM_STATES:
            raise ValueError("模型返回了未知观察状态")
        cleaned = {
            "state": state,
            "summary": str(item.get("summary", "")).strip()[:180],
            "supporting_evidence": [
                str(x).strip()[:140]
                for x in item.get("supporting_evidence", [])[:2]
            ],
            "contrary_evidence": [
                str(x).strip()[:140]
                for x in item.get("contrary_evidence", [])[:2]
            ],
            "watch_for": [
                str(x).strip()[:140] for x in item.get("watch_for", [])[:2]
            ],
            "thesis_check": str(item.get("thesis_check", "")).strip()[:180],
            "risk": str(item.get("risk", "")).strip()[:180],
        }
        if re.search(r"\d", json.dumps(cleaned, ensure_ascii=False)):
            raise ValueError("模型输出了程序之外的数字")
        if not cleaned["contrary_evidence"]:
            raise ValueError("模型没有给出反面证据或信息缺口")
        if cleaned["summary"]:
            output[code] = cleaned
    return output


def _build_llm_prompt(payload):
    """模型只做证据翻译和反证检查，不接管筛选、评分或价位。"""
    schema = {
        "items": [{
            "code": "原样返回输入代码",
            "state": "仅进入观察|等待确认|确认增强|风险优先",
            "summary": "一句话说明当前所处阶段",
            "supporting_evidence": ["一至两条支持当前观察的事实"],
            "contrary_evidence": ["至少一条反面事实、冲突或信息缺口"],
            "thesis_check": "原关注逻辑是否符合、部分符合或无法判断，并说明原因",
            "watch_for": ["一至两条下一交易日可由收盘数据验证的事件"],
            "risk": "最关键的失败路径",
        }]
    }
    return (
        "你是A股盘后观察助手。程序已经完成候选筛选、提醒排序、关键价位和失效条件计算；"
        "你的职责是解释证据、主动寻找反证，并把下一步观察写成可验证事件。\n"
        "输入中的提醒分只用于消息排序，不是胜率、上涨概率或预期收益，不能据此做概率推断。\n"
        "信息边界：只能使用输入中的日线量价、技术位置、触发规则、程序观察计划和用户填写的thesis。"
        "不得补充新闻、行业热度、基本面、资金流、盘口、筹码或所谓庄家行为。\n"
        "字段口径：pct和ret是小数收益率；dist_ma为收盘相对均线距离，正值在线上；"
        "volr是当日量相对二十日均量；atr_pct是日线波动比例；dd_52w是距阶段高点回撤；"
        "pos_52w越接近低端表示位置越低。只解释方向和相互关系，不在输出中复述数值。\n"
        "分析顺序：先区分只是进入观察、仍待确认、确认增强还是风险优先；再给支持证据；"
        "必须给至少一条反面证据或信息缺口；然后对照thesis；最后列出下一交易日收盘后能够判断真假的观察事件。\n"
        "写作要求：避免‘关注后续走势’‘注意风险’等空话；不预测一定上涨；不直接给买卖指令；"
        "不要重复程序已经给出的具体价位。除code原样返回外，所有文本字段禁止出现阿拉伯数字、价格和百分比。\n"
        "只输出JSON，不输出Markdown或额外说明。必须严格使用以下结构："
        + json.dumps(schema, ensure_ascii=False)
        + "\n输入："
        + json.dumps(payload, ensure_ascii=False)
    )


def llm_brief(events, max_items=5):
    """用最多一次模型请求解释全部入选股票。"""
    key = os.environ.get("GLM_API_KEY") or os.environ.get(
        "ANTHROPIC_AUTH_TOKEN", ""
    )
    if not key or not events:
        return {}
    selected = events[:max_items]
    payload = [_analysis_payload(event) for event in selected]
    prompt = _build_llm_prompt(payload)
    body = json.dumps({
        "model": os.environ.get("LLM_MODEL", "glm-5.3"),
        "max_tokens": 2500,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    base = os.environ.get(
        "LLM_BASE_URL", "https://open.bigmodel.cn/api/anthropic"
    )
    request = urllib.request.Request(
        base.rstrip("/") + "/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        response = json.loads(
            urllib.request.urlopen(request, timeout=120).read()
        )
        answer = "".join(
            block.get("text", "")
            for block in response.get("content", [])
            if block.get("type") == "text"
        )
        return _parse_briefs(
            answer, {event["code"] for event in selected}
        )
    except Exception as exc:
        print(
            "⚠ 大模型分析失败，已使用规则版观察计划：%s" % exc,
            file=sys.stderr,
        )
        return {}


def _split_markdown(text, max_bytes=3500):
    """按股票块切分，并按 UTF-8 字节数限制消息。"""
    chunks, current = [], ""
    for block in text.split("\n\n"):
        candidate = block if not current else current + "\n\n" + block
        if len(candidate.encode("utf-8")) <= max_bytes:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = block
        if len(current.encode("utf-8")) > max_bytes:
            raw = current.encode("utf-8")
            while len(raw) > max_bytes:
                cut = raw[:max_bytes]
                while True:
                    try:
                        chunks.append(cut.decode("utf-8"))
                        break
                    except UnicodeDecodeError:
                        cut = cut[:-1]
                raw = raw[len(cut) :]
            current = raw.decode("utf-8")
    if current:
        chunks.append(current)
    return chunks


def push_report(report, result):
    hook = os.environ.get("WATCH_WEBHOOK", "")
    serverchan = os.environ.get("SERVERCHAN_KEY", "")
    if hook:
        chunks = _split_markdown(report)
        for chunk in chunks:
            data = json.dumps({
                "msgtype": "markdown",
                "markdown": {"content": chunk},
            }).encode()
            urllib.request.urlopen(
                urllib.request.Request(
                    hook,
                    data=data,
                    headers={"Content-Type": "application/json"},
                ),
                timeout=10,
            )
        print(
            "已推送企微 %d 条（%d 只股票）"
            % (len(chunks), len(result["events"]))
        )
    elif serverchan:
        body = urllib.parse.urlencode({
            "title": "盘后观察：%d只（%s）"
            % (len(result["events"]), result["date"]),
            "desp": report[:30000],
        }).encode()
        urllib.request.urlopen(
            urllib.request.Request(
                "https://sctapi.ftqq.com/%s.send" % serverchan,
                data=body,
            ),
            timeout=15,
        )
        print("已推送 Server酱（%d 只股票）" % len(result["events"]))
    else:
        print("未配置 WATCH_WEBHOOK 或 SERVERCHAN_KEY，仅生成本地报告")


def main(argv=None):
    _load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--push", action="store_true", help="有入选提醒时推送")
    parser.add_argument(
        "--llm", action="store_true", help="用一次模型请求解释全部入选提醒"
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="预览当前活跃条件，不读写提醒状态",
    )
    parser.add_argument(
        "--bootstrap-alerts",
        action="store_true",
        help="首次建状态时也提醒；默认静默建基线",
    )
    args = parser.parse_args(argv)
    result = run(
        preview=args.preview,
        persist=not args.preview,
        bootstrap_alerts=args.bootstrap_alerts,
    )
    max_llm = int(result["alerting"]["llm_max_stocks"])
    briefs = (
        llm_brief(result["events"], max_llm) if args.llm else {}
    )
    report = format_report(result, briefs)
    print(report)
    for error in result["errors"]:
        print("⚠ %s" % error, file=sys.stderr)

    if result["events"] and not args.preview:
        reports = HERE / "reports"
        reports.mkdir(exist_ok=True)
        (reports / (result["date"] + ".md")).write_text(
            report, encoding="utf-8"
        )
    if args.push and result["events"] and not args.preview:
        push_report(report, result)
    elif args.push and args.preview:
        print("preview 模式不会推送")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
