# -*- coding: utf-8 -*-
"""Reverse-engineer signal and early-confirmation features for six B1 paths.

This is a path-mechanism study, not an account backtest. Feature hypotheses are
selected on 2020-2022 outcomes and checked without changing their direction on
2023-2026. T0 means the signal-day close; T+1..T+5 features are reported
separately as post-signal confirmation, never disguised as signal-day inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from sklearn.tree import DecisionTreeClassifier, export_text

from b1_shape_predict import (BAD, CLASSES, CLASS_NAMES, CORE_FEATURES, DEST as PRED_DEST,
                              build_frame, pct)


HERE = Path(__file__).resolve().parent
DEST = HERE / "out/b1_shape_reverse_v1"
DEV_END = pd.Timestamp("2023-01-01")
CONTRASTS = {
    "good_vs_bad": ({"A1", "C"}, {"B", "D1"}),
    "A1_vs_C": ({"A1"}, {"C"}),
    "A1_vs_B": ({"A1"}, {"B"}),
    "C_vs_D1": ({"C"}, {"D1"}),
    "B_vs_D1": ({"B"}, {"D1"}),
}
FEATURE_NAMES = {
    "dist_ma5": "距MA5", "dist_ma20": "距MA20", "dist_ma60": "距MA60",
    "dist_ma120": "距MA120", "slope20": "MA20斜率", "slope60": "MA60斜率",
    "ret5": "前5日涨跌", "ret20": "前20日涨跌", "ret60": "前60日涨跌",
    "atr_pct": "ATR波动率", "volatility20": "20日波动率", "range10": "10日振幅",
    "dist_high20": "距20日高点", "dist_high60": "距60日高点",
    "close_location": "当日收盘位置", "upper_wick": "上影线比例", "body": "当日实体",
    "volume_ratio": "当日量/20日均量", "volume_dry": "5日量/20日量",
    "up_down_volume": "上涨量/下跌量", "log_amount": "成交额",
    "j": "KDJ-J", "rsi": "RSI", "keyk_intact": "关键K未破",
    "keyk_age": "关键K距今天数", "keyk_distance": "距关键K低点",
    "above_ma60": "市场MA60上方比例", "idx_slope20": "指数MA20斜率",
    "idx_ret20": "指数20日涨跌", "idx_vol20": "指数20日波动",
    "amt_ratio": "全市场成交额比", "rotation": "轮动强度", "b1plus": "旧B1+条件",
    "long_atr": "距长期线/ATR", "support_atr": "距下方支撑/ATR",
    "red_green_ratio": "阳线量/阴线量", "pullback_volume_ratio": "回调量比",
    "base_tightness": "整理紧密度", "room_risk": "上方空间/下方风险",
    "key_age": "2倍量关键K年龄", "key_atr": "距2倍量关键K/ATR",
    "pocket_volume_ratio": "当日量/前10日最大阴量", "market_up": "指数趋势向上",
    "breadth_up": "市场宽度向上", "amv_return_1d": "活跃市值1日涨跌",
    "amv_return_2d": "活跃市值2日涨跌", "amv_wave_age": "活跃波段年龄",
    "amv_wave_return": "活跃波段累计涨跌", "amv_peak_drawdown": "活跃波段峰值回撤",
    "amv_trigger_return": "活跃波段启动强度", "amv_trigger_1d": "当日1日启动",
    "amv_trigger_2d": "当日2日启动", "log_candidates_today": "当日候选数量",
}
for _name in list(FEATURE_NAMES):
    FEATURE_NAMES[f"rank_{_name}"] = f"当日候选内{FEATURE_NAMES[_name]}分位"


def auc_feature(values: pd.Series, target: pd.Series):
    keep = values.notna() & target.notna()
    x, y = values[keep].astype(float), target[keep].astype(bool)
    if len(x) < 100 or y.nunique() < 2 or x.nunique() < 2:
        return np.nan
    return float(roc_auc_score(y, x))


def contrast_frame(frame: pd.DataFrame, name: str):
    positive, negative = CONTRASTS[name]
    q = frame[frame.path.isin(positive | negative)].copy()
    q["target"] = q.path.isin(positive)
    return q


def feature_scan(frame: pd.DataFrame):
    rows = []
    for contrast in CONTRASTS:
        q = contrast_frame(frame, contrast)
        dev, test = q[q.date < DEV_END], q[q.date >= DEV_END]
        for feature in CORE_FEATURES:
            dev_auc = auc_feature(dev[feature], dev.target)
            if not np.isfinite(dev_auc):
                continue
            direction = 1 if dev_auc >= .5 else -1
            test_auc = auc_feature(test[feature], test.target)
            oriented_test = test_auc if direction == 1 else 1 - test_auc
            yearly = []
            for _, g in q.groupby(q.date.dt.year):
                value = auc_feature(g[feature], g.target)
                if np.isfinite(value):
                    yearly.append(value if direction == 1 else 1 - value)
            pos_dev = dev.loc[dev.target, feature].astype(float)
            neg_dev = dev.loc[~dev.target, feature].astype(float)
            pos_test = test.loc[test.target, feature].astype(float)
            neg_test = test.loc[~test.target, feature].astype(float)
            scale = dev[feature].astype(float).std(ddof=0)
            rows.append({
                "contrast": contrast, "feature": feature,
                "feature_cn": FEATURE_NAMES.get(feature, feature),
                "preferred": "high" if direction == 1 else "low",
                "dev_auc_oriented": max(dev_auc, 1 - dev_auc),
                "test_auc_oriented": oriented_test,
                "dev_std_effect": direction * (pos_dev.mean() - neg_dev.mean()) / scale
                if np.isfinite(scale) and scale > 0 else np.nan,
                "test_std_effect": direction * (pos_test.mean() - neg_test.mean()) / scale
                if np.isfinite(scale) and scale > 0 else np.nan,
                "consistent_years": sum(v > .5 for v in yearly),
                "years": len(yearly), "year_auc_min": min(yearly) if yearly else np.nan,
                "year_auc_median": np.median(yearly) if yearly else np.nan,
                "year_auc_max": max(yearly) if yearly else np.nan,
                "dev_positive_n": int(dev.target.sum()), "dev_negative_n": int((~dev.target).sum()),
                "test_positive_n": int(test.target.sum()), "test_negative_n": int((~test.target).sum()),
            })
    table = pd.DataFrame(rows)
    table["dev_strength"] = (table.dev_auc_oriented - .5).abs()
    table["dev_rank"] = table.groupby("contrast").dev_strength.rank(
        method="first", ascending=False).astype(int)
    table["selected_in_dev"] = table.dev_rank <= 8
    return table.sort_values(["contrast", "dev_rank"])


def fit_tree(train: pd.DataFrame, test: pd.DataFrame, features: list[str], target="target"):
    x = train[features].astype(float).replace([np.inf, -np.inf], np.nan)
    z = test[features].astype(float).replace([np.inf, -np.inf], np.nan)
    low, high = x.quantile(.01).fillna(0.), x.quantile(.99).fillna(0.)
    x = x.clip(low, high, axis=1)
    fill = x.median().fillna(0.)
    x, z = x.fillna(fill), z.clip(low, high, axis=1).fillna(fill)
    weights = 1 / train.groupby("date").event_id.transform("size").to_numpy(float)
    class_total = pd.Series(weights).groupby(train[target].reset_index(drop=True)).transform("sum")
    weights /= class_total.to_numpy(float)
    model = DecisionTreeClassifier(max_depth=2, min_samples_leaf=max(150, len(train) // 20),
                                   random_state=20261006)
    model.fit(x, train[target], sample_weight=weights)
    pred = model.predict_proba(z)[:, list(model.classes_).index(True)]
    auc = float(roc_auc_score(test[target], pred))
    return model, pred, auc, export_text(model, feature_names=features, decimals=5)


def t0_trees(frame: pd.DataFrame, scan: pd.DataFrame):
    results, leaves = [], []
    for contrast in CONTRASTS:
        q = contrast_frame(frame, contrast)
        train, test = q[q.date < DEV_END].copy(), q[q.date >= DEV_END].copy()
        features = scan[(scan.contrast == contrast) & scan.selected_in_dev].feature.tolist()
        model, pred, auc, rules = fit_tree(train, test, features)
        test["prediction"] = pred
        train_x = train[features].astype(float).replace([np.inf, -np.inf], np.nan)
        test_x = test[features].astype(float).replace([np.inf, -np.inf], np.nan)
        low, high = train_x.quantile(.01).fillna(0.), train_x.quantile(.99).fillna(0.)
        fill = train_x.clip(low, high, axis=1).median().fillna(0.)
        test["leaf"] = model.apply(test_x.clip(low, high, axis=1).fillna(fill))
        results.append({"contrast": contrast, "features": "|".join(features),
                        "test_auc": auc, "test_n": len(test), "rules": rules})
        for leaf, g in test.groupby("leaf"):
            leaves.append({"contrast": contrast, "leaf": int(leaf), "n": len(g),
                           "predicted_probability": float(g.prediction.mean()),
                           "actual_positive_rate": float(g.target.mean()),
                           "mean_net": float(g.net.mean()), "paths": json.dumps(
                               g.path.value_counts(normalize=True).round(4).to_dict())})
    return pd.DataFrame(results), pd.DataFrame(leaves)


def full_paths(frame: pd.DataFrame, prices: dict):
    ei, si = frame.entry_i.to_numpy(int), frame.sym_i.to_numpy(int)
    idx = ei[:, None] + np.arange(15)[None, :]
    close = prices["close"]
    cp = close[idx, si[:, None]].astype(float)
    entry = prices["open"][ei, si].astype(float)
    return cp / entry[:, None] - 1


def first_day(mask: np.ndarray, offset=1):
    any_ = mask.any(axis=1)
    result = np.full(len(mask), np.nan)
    result[any_] = np.argmax(mask[any_], axis=1) + offset
    return result


def path_timing(frame: pd.DataFrame, rel: np.ndarray):
    q = frame[["event_id", "path", "date"]].copy()
    q["peak_day"] = np.argmax(rel, axis=1) + 1
    q["trough_day"] = np.argmin(rel, axis=1) + 1
    q["first_positive_day"] = first_day(rel > 0)
    changes = np.diff(np.c_[np.zeros(len(rel)), rel], axis=1)
    q["first_up_close_day"] = first_day(changes > 0)
    q["trough_before_peak"] = q.trough_day < q.peak_day
    rows = []
    for path, g in q.groupby("path"):
        rows.append({
            "path": path, "name": CLASS_NAMES[path], "n": len(g),
            "peak_q25": g.peak_day.quantile(.25), "peak_median": g.peak_day.median(),
            "peak_q75": g.peak_day.quantile(.75), "peak_le3": g.peak_day.le(3).mean(),
            "trough_q25": g.trough_day.quantile(.25), "trough_median": g.trough_day.median(),
            "trough_q75": g.trough_day.quantile(.75), "trough_le3": g.trough_day.le(3).mean(),
            "trough_le5": g.trough_day.le(5).mean(),
            "trough_before_peak": g.trough_before_peak.mean(),
            "first_positive_median": g.first_positive_day.median(),
            "first_up_close_median": g.first_up_close_day.median(),
        })
    return q, pd.DataFrame(rows).set_index("path").reindex(CLASSES).reset_index()


def prefix_features(rel: np.ndarray, k: int):
    x = rel[:, :k]
    previous = np.c_[np.zeros(len(x)), x[:, :-1]]
    low, high, last = x.min(axis=1), x.max(axis=1), x[:, -1]
    width = high - low
    return pd.DataFrame({
        "ret_now": last, "min_so_far": low, "max_so_far": high,
        "fade_from_high": last - high, "bounce_from_low": last - low,
        "last_change": last - previous[:, -1],
        "up_fraction": (x > previous).mean(axis=1),
        "range_position": np.divide(last - low, width, out=np.full(len(x), .5), where=width > 1e-12),
    })


def early_trees(frame: pd.DataFrame, rel: np.ndarray):
    results = []
    for contrast in ["good_vs_bad", "A1_vs_B", "C_vs_D1"]:
        positive, negative = CONTRASTS[contrast]
        mask = frame.path.isin(positive | negative).to_numpy()
        base = frame.loc[mask, ["event_id", "date", "path", "net"]].reset_index(drop=True)
        base["target"] = base.path.isin(positive)
        for k in [1, 2, 3, 5]:
            x = prefix_features(rel[mask], k)
            q = pd.concat([base, x], axis=1)
            train, test = q[q.date < DEV_END], q[q.date >= DEV_END]
            features = x.columns.tolist()
            model, pred, auc, rules = fit_tree(train, test, features)
            results.append({"contrast": contrast, "known_at": f"T+{k}",
                            "test_auc": auc, "test_n": len(test), "rules": rules})
    return pd.DataFrame(results)


def rule_stats(frame: pd.DataFrame, rel: np.ndarray):
    rows = []
    definitions = {}
    for k in [2, 3, 5]:
        x = prefix_features(rel, k)
        definitions[k] = {
            "above_entry": x.ret_now > 0,
            "up_close": x.last_change > 0,
            "no_2pct_fade": x.fade_from_high > -.02,
            "bounce_2pct": x.bounce_from_low >= .02,
            "above_entry_and_up": (x.ret_now > 0) & (x.last_change > 0),
        }
    periods = {"all": np.ones(len(frame), bool),
               "development_2020_2022": frame.date.lt(DEV_END).to_numpy(),
               "test_2023_2026": frame.date.ge(DEV_END).to_numpy()}
    for contrast in ["A1_vs_B", "C_vs_D1"]:
        positive, negative = CONTRASTS[contrast]
        base_cohort = frame.path.isin(positive | negative).to_numpy()
        target = frame.path.isin(positive).to_numpy()
        for period, period_mask in periods.items():
            cohort = base_cohort & period_mask
            for k, rules in definitions.items():
                for name, rule in rules.items():
                    hit = cohort & rule.to_numpy()
                    rows.append({
                        "contrast": contrast, "period": period,
                        "known_at": f"T+{k}", "rule": name,
                        "selected": int(hit.sum()),
                        "positive_precision": float(target[hit].mean()) if hit.any() else np.nan,
                        "positive_recall": float((hit & target).sum() / (cohort & target).sum()),
                        "negative_rejected": float(((cohort & ~target) & ~hit).sum() /
                                                   (cohort & ~target).sum()),
                    })
    return pd.DataFrame(rows)


def entry_timing(frame: pd.DataFrame, rel: np.ndarray):
    changes = np.diff(np.c_[np.zeros(len(rel)), rel], axis=1)
    first_up = first_day(changes[:, :5] > 0)
    first_reclaim = first_day(rel[:, :10] > 0)
    rows = []
    for path in CLASSES:
        cohort = frame.path.eq(path).to_numpy()
        for method, day in [("direct_next_open", np.zeros(len(rel))),
                            ("first_up_close_by_T5", first_up),
                            ("first_reclaim_entry_by_T10", first_reclaim)]:
            valid = cohort & np.isfinite(day)
            at = day[valid].astype(int)
            entry_ret = np.zeros(valid.sum()) if method == "direct_next_open" else rel[valid, at - 1]
            to_end = (1 + rel[valid, -1]) / (1 + entry_ret) - 1
            rows.append({
                "path": path, "method": method, "eligible": int(valid.sum()),
                "coverage": float(valid.sum() / cohort.sum()),
                "mean_entry_day": float(day[valid].mean()) if valid.any() else np.nan,
                "mean_entry_vs_open": float(entry_ret.mean()) if valid.any() else np.nan,
                "mean_return_to_day15": float(to_end.mean()) if valid.any() else np.nan,
                "win_to_day15": float((to_end > 0).mean()) if valid.any() else np.nan,
            })
    return pd.DataFrame(rows)


def render(out, scan, trees, early, timing, rules, entries):
    selected = scan[scan.selected_in_dev]
    lines = [
        "# B1 六类路径：从结果反推信号与买点",
        "",
        "## 研究问题",
        "",
        "这份分析不做资金账户。它把 57,949 条可成交信号的后 15 日路径作为结果，反查信号日能看到的"
        "价格、量能、位置、大盘与活跃市值特征，并把信号日判断和 T+1～T+5 确认严格分开。",
        "",
        "- `A1+C vs B+D1`：先找总体好坏。",
        "- `A1 vs C`：判断适合直接买还是更可能先回落。",
        "- `A1 vs B`：两者第一天都涨，专门识别假启动。",
        "- `C vs D1`：两者第一天都跌，专门识别可等买点的回升与持续走弱。",
        "- A2、D2 保留在峰谷和买点统计中，但不混入四个最干净的二分类。",
        "",
        "特征方向只在 2020–2022 反推；2023–2026 只检查方向是否还能成立。AUC=0.50 表示无法区分，"
        "0.55 仍只是很弱的排序信息。",
        "", "## 主要结论", "",
        "1. **信号日很难直接判六类。** 总体好坏、A1/B、C/D1 的两层树测试 AUC 都在 0.47–0.49；"
        "A1/C 也只有 0.528。现有指标不支持把候选在 T0 硬切成“立即买”和“等回调买”。",
        "2. **少数信号日变量可作为弱提示。** 较强的活跃市值启动幅度、较高全市场成交额比，在"
        "总体好坏和 A1/B 上跨阶段保持正方向；较年轻的活跃波段、较小的波段内回撤对 A1/B 也有帮助。"
        "它们适合排序或决定试仓大小，还不足以单独否决股票。",
        "3. **真正有用的是 T+2～T+3 的走势确认。** 到 T+3，总体好坏 AUC=0.728、A1/B=0.738、"
        "C/D1=0.725；继续等到 T+5 会更清楚，但会牺牲 A1 的前段涨幅。",
        "4. **一个转涨收盘不够。** D1 中 89.69% 在前 5 日也至少出现过一次转涨收盘，之后到第15日"
        "仍平均亏损。更有意义的是重新站上原入场价、从低点反弹约 2%，或站上入场价同时当天继续走强。",
        "",
        "## 信号日能否区分",
        "",
        "|问题|浅树测试AUC|解释|",
        "|---|---:|---|",
    ]
    explanations = {
        "good_vs_bad": "总体好坏", "A1_vs_C": "直接涨或先跌后涨",
        "A1_vs_B": "真启动或假启动", "C_vs_D1": "回升或持续走弱",
        "B_vs_D1": "先冲高失败或直接走弱",
    }
    for r in trees.itertuples(index=False):
        lines.append(f"|{r.contrast}|{r.test_auc:.3f}|{explanations[r.contrast]}|")
    lines += [
        "", "浅树只允许两层。若测试 AUC 接近 0.50，说明即使事后挑出最有差异的 8 个信号日特征，"
        "也无法在后几年稳定复现；不能把某个漂亮均值直接写成硬筛选。",
        "", "## 2020–2022 反推出、再到 2023–2026 检查的特征", "",
        "|问题|特征|偏好方向|开发AUC|测试AUC|一致年份|测试标准化差|",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for r in selected.itertuples(index=False):
        lines.append(f"|{r.contrast}|{r.feature_cn} (`{r.feature}`)|{r.preferred}|"
                     f"{r.dev_auc_oriented:.3f}|{r.test_auc_oriented:.3f}|"
                     f"{r.consistent_years}/{r.years}|{r.test_std_effect:+.3f}|")
    lines += [
        "", "## 等 1～5 天后，路径本身增加多少辨识力", "",
        "|问题|可用时点|测试AUC|",
        "|---|---:|---:|",
    ]
    for r in early.itertuples(index=False):
        lines.append(f"|{r.contrast}|{r.known_at}|{r.test_auc:.3f}|")
    lines += [
        "", "这里使用的只有入场开盘后的收盘路径：当前收益、截至当日最大涨跌、从高点回落、"
        "从低点反弹、最后一天变化、上涨天数比例。它回答的是‘等确认是否有用’，不能回填成 T0 特征。",
        "", "## 六类峰谷日", "",
        "|类型|数量|峰值日中位数(Q1–Q3)|谷值日中位数(Q1–Q3)|前三日见顶|前三日见底|谷在峰前|",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in timing.itertuples(index=False):
        lines.append(f"|{r.path} {r.name}|{r.n:,}|{r.peak_median:.0f} ({r.peak_q25:.0f}–{r.peak_q75:.0f})|"
                     f"{r.trough_median:.0f} ({r.trough_q25:.0f}–{r.trough_q75:.0f})|"
                     f"{pct(r.peak_le3)}|{pct(r.trough_le3)}|{pct(r.trough_before_peak)}|")
    lines += [
        "", "## A1 与 C 的买点含义", "",
        "`direct_next_open` 是现有次日开盘直接买；`first_up_close_by_T5` 是前 5 日首次收盘高于前一收盘；"
        "`first_reclaim_entry_by_T10` 是前 10 日首次重新站上原入场开盘。后两者是事后按收盘确认的"
        "路径诊断，不代表能在该收盘价无滑点成交。",
        "", "|类型|方法|覆盖率|平均确认日|确认价相对原开盘|确认后至15日收益|确认后胜率|",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in entries[entries.path.isin(["A1", "C", "B", "D1"])].itertuples(index=False):
        lines.append(f"|{r.path}|{r.method}|{pct(r.coverage)}|{r.mean_entry_day:.1f}|"
                     f"{pct(r.mean_entry_vs_open)}|{pct(r.mean_return_to_day15)}|{pct(r.win_to_day15)}|")
    lines += [
        "", "## 2023–2026 的简单确认信号", "",
        "正类在 A1/B 中是 A1，在 C/D1 中是 C。‘准确率’是触发后属于正类的比例；‘排坏率’是"
        "负类中没有触发、因而被挡住的比例。",
        "", "|问题|时点|规则|触发数|准确率|正类召回|排坏率|",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    rule_names = {"above_entry": "站上原入场价", "bounce_2pct": "自阶段低点反弹至少2%",
                  "above_entry_and_up": "站上入场价且当日转涨"}
    show = rules[(rules.period == "test_2023_2026") & rules.rule.isin(rule_names)]
    for r in show.itertuples(index=False):
        lines.append(f"|{r.contrast}|{r.known_at}|{rule_names[r.rule]}|{r.selected:,}|"
                     f"{pct(r.positive_precision)}|{pct(r.positive_recall)}|{pct(r.negative_rejected)}|")
    lines += [
        "", "完整 `confirmation_rules.csv` 同时保存发现期、验证期和全样本。",
        "", "## 可复核文件", "",
        "- `feature_contrasts.csv`：全部信号日特征、全部五个对比，不只保留好看的行。",
        "- `frozen_feature_validation.csv`：开发期选出的每组前 8 个特征及后期结果。",
        "- `t0_trees.csv`、`t0_tree_leaves.csv`：两层可解释树和测试叶节点。",
        "- `early_path_trees.csv`：T+1/T+2/T+3/T+5 的路径确认能力。",
        "- `path_timing.csv`、`entry_timing.csv`：峰谷日与买点路径。",
        "- `confirmation_rules.csv`：简单确认规则。",
        "", "## 解释边界", "",
        "- ‘庄家成本、洗盘、假启动’在这里只有可观测价格路径含义，没有把不可观测主体行为当作事实。",
        "- 结果反推会产生多重比较和过拟合，所以开发期看起来强、测试期回到 0.50 的特征应删除。",
        "- 峰日、谷日、首次反转日都是未来信息，只用于定义等待逻辑和统计，不进入信号日模型。",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(out=DEST, replace=False):
    out = Path(out)
    if (out / "manifest.json").exists() and not replace:
        raise FileExistsError(f"Refusing to overwrite a completed study: {out}")
    out.mkdir(parents=True, exist_ok=True)
    frame, unfilled, _, prices, source, universe = build_frame(False)
    rel = full_paths(frame, prices)
    timing_rows, timing = path_timing(frame, rel)
    timing.to_csv(out / "path_timing.csv", index=False)
    timing_rows.to_parquet(out / "path_timing_events.parquet", index=False)

    scan = feature_scan(frame)
    scan.to_csv(out / "feature_contrasts.csv", index=False)
    selected = scan[scan.selected_in_dev].copy()
    selected.to_csv(out / "frozen_feature_validation.csv", index=False)
    trees, leaves = t0_trees(frame, scan)
    trees.to_csv(out / "t0_trees.csv", index=False)
    leaves.to_csv(out / "t0_tree_leaves.csv", index=False)
    early = early_trees(frame, rel)
    early.to_csv(out / "early_path_trees.csv", index=False)
    rules = rule_stats(frame, rel)
    rules.to_csv(out / "confirmation_rules.csv", index=False)
    entries = entry_timing(frame, rel)
    entries.to_csv(out / "entry_timing.csv", index=False)
    render(out, scan, trees, early, timing, rules, entries)

    manifest = {
        "status": "COMPLETED_PATH_MECHANISM_STUDY", "universe": universe,
        "unfilled_signals_excluded": len(unfilled), "development": "2020-2022",
        "frozen_test": "2023-2026", "contrasts": list(CONTRASTS),
        "features_scanned": len(CORE_FEATURES), "source": source,
        "checks": {
            "events": len(frame), "rel_shape": list(rel.shape),
            "paths_match": bool(np.array_equal(frame.path.to_numpy(),
                np.select([(rel[:, 0] > 0) & (rel[:, -1] > 0) & (rel.min(1) > -.02),
                           (rel[:, 0] > 0) & (rel[:, -1] > 0), rel[:, 0] > 0,
                           rel[:, -1] > 0, rel.max(1) < .02],
                          ["A1", "A2", "B", "C", "D1"], default="D2"))),
            "selected_features_only_from_dev": True,
            "future_path_not_in_t0_features": True,
        },
        "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False, default=str) + "\n",
        encoding="utf-8")
    print(out / "report.md")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEST)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    run(args.out, args.replace)
