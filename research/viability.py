# -*- coding: utf-8 -*-
"""viability.py —— R0 可行性报告(离线,确定性表格,无 AI 主观成分)。

回答:现有 KDJ/量价策略历史上扣除基线后有没有正期望?什么条件下成立/失效?
读 dataset parquet,产出 research/viability-<日期>.md。
解读性结论由 AI 事后追加到"AI 初读"节(标注探索性)。
"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from research.dataset import HORIZONS                      # noqa: E402

MIN_CELL_N = 30          # 格子样本门槛:低于只标"样本不足"
NOTE_N = 100             # "值得注意"门槛
EXCESS_ATTN = 0.005      # 超额中位数 |x|≥0.5% 才进入机械结论

FOOTNOTES = [
    "幸存者偏差:parquet 只含当前在市股票,回放胜率系统性偏高(不可修复,只能标注)",
    "ST 股 5% 涨停拦不住(硬过滤 9.7% 代理对 ST 无效)",
    "分桶/条件边界系看过数据后所定(6c 修订),条件格子的超额带选择偏差,只作探索性参考",
    "前瞻收益为仓位制(该股自身 bar 序列,停牌照位)——与池跟踪口径一致",
    "指数数据可能滞后个股一日,滞后日事件的基线/环境为 NaN 并被剔除",
]


def load_dataset(cfg_ver=None):
    d = os.path.join(ROOT, "research")
    if cfg_ver is None:
        cands = [f for f in os.listdir(d) if f.startswith("dataset-") and f.endswith(".parquet")]
        if not cands:
            raise SystemExit("未找到 dataset-*.parquet,先跑 research/dataset.py")
        cands.sort(key=lambda f: os.path.getmtime(os.path.join(d, f)))
        cfg_ver = cands[-1][len("dataset-"):-len(".parquet")]
    ds = pd.read_parquet(os.path.join(d, "dataset-%s.parquet" % cfg_ver))
    meta = json.load(open(os.path.join(d, "dataset-%s.meta.json" % cfg_ver), encoding="utf-8"))
    return ds, meta


def _pct(x, signed=True):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return ("%+.2f%%" if signed else "%.2f%%") % (100 * x)


def _num(x, nd=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return ("%." + str(nd) + "f") % x


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def j_bin(j):
    """J 三档(与 bucket_key 同界,超界钳制);NaN→'?'。"""
    if not (j == j):
        return "?"
    j = min(j, 15.0)
    return "J≤5" if j <= 5 else ("J5~10" if j <= 10 else "J10~15")


def dd_bin(dd):
    if not (dd == dd):
        return "?"
    dd = min(max(dd, -0.25), 0.0)
    return "dd>−10%" if dd > -0.10 else ("dd−10~15%" if dd > -0.15 else "dd≤−15%")


def _bin2(val, cut, hi, lo):
    if not (val == val):
        return "?"
    return hi if val >= cut else lo


def cohort_row(name, sub):
    """一个队列的完整指标(多期限表 + 池口径表数据)。"""
    n = len(sub)
    closed = sub[sub["outcome_type"].isin(["hit", "miss", "flat"])]
    nc = len(closed)
    r = {"name": name, "n": n, "n_closed": nc}
    for h in HORIZONS:
        r["r%d" % h] = sub["r%d" % h].median()
        r["b%d" % h] = sub["b%d" % h].median()
        r["e%d" % h] = (sub["r%d" % h] - sub["b%d" % h]).median()
    if nc:
        hr = float((closed["outcome_type"] == "hit").mean())
        mr = float((closed["outcome_type"] == "miss").mean())
        r.update({"hit": hr, "miss": mr, "flat": 1 - hr - mr,
                  "expect": 0.08 * hr - 0.05 * mr,
                  "mfe": closed["mfe"].median(), "mae": closed["mae"].median(),
                  "ft_up": closed.loc[closed["outcome_type"] == "hit", "ft_up_day"].median(),
                  "ft_dn": closed.loc[closed["outcome_type"] == "miss", "ft_dn_day"].median()})
    else:
        r.update({"hit": np.nan, "miss": np.nan, "flat": np.nan, "expect": np.nan,
                  "mfe": np.nan, "mae": np.nan, "ft_up": np.nan, "ft_dn": np.nan})
    return r


def horizon_table(rows):
    hdr = ["队列", "n"] + sum([["r%d" % h, "基线%d" % h, "超额%d" % h] for h in HORIZONS], [])
    body = []
    for r in rows:
        line = [r["name"], r["n"]]
        for h in HORIZONS:
            line += [_pct(r["r%d" % h]), _pct(r["b%d" % h]), _pct(r["e%d" % h])]
        body.append(line)
    return md_table(hdr, body)


def pool_table(rows):
    hdr = ["队列", "已结n", "hit率", "miss率", "flat率", "理想期望*", "MFE中位", "MAE中位",
           "hit首触日", "miss首触日"]
    body = [[r["name"], r["n_closed"], _pct(r["hit"], False), _pct(r["miss"], False),
             _pct(r["flat"], False), _pct(r["expect"]), _pct(r["mfe"]), _pct(r["mae"]),
             _num(r["ft_up"], 1), _num(r["ft_dn"], 1)] for r in rows]
    return md_table(hdr, body)


def cond_table(sub, by, min_n=MIN_CELL_N):
    """按条件列分组:超额@10D / hit率 / n;按超额降序;子样本不足只标注不下结论。"""
    items = []
    for key, g in sub.groupby(by):
        closed = g[g["outcome_type"].isin(["hit", "miss", "flat"])]
        e10 = float((g["r10"] - g["b10"]).median())
        enough = len(closed) >= min_n
        hr = float((closed["outcome_type"] == "hit").mean()) if (enough and len(closed)) else np.nan
        items.append((str(key), len(g), e10, hr, enough))
    items.sort(key=lambda t: t[2] if t[2] == t[2] else -9e9, reverse=True)
    return md_table(["条件", "n", "超额@10D中位", "hit率", ""],
                    [[k, n, _pct(e), _pct(hr, False) if hr == hr else "样本不足",
                      "" if ok else "⚠n<%d" % min_n] for k, n, e, hr, ok in items])


def build_report(out_path=None):
    ds, meta = load_dataset()
    today = time.strftime("%Y-%m-%d")
    out_path = out_path or os.path.join(ROOT, "research", "viability-%s.md" % today)

    dedup = ds[ds["ep_start"]] if "ep_start" in ds else ds
    L = ["# 策略可行性报告 %s(R0)" % today, ""]
    L.append("> 数据集:%s | 事件 %s | 范围 %s~%s | 基线=同日全白名单横截面中位数(仓位制)" %
             (meta["cfg_version"], format(meta["n_events"], ","),
              meta["date_range"][0], meta["date_range"][1]))
    L.append("> 硬过滤参数:额≥%.0f亿 / 剔涨幅≥%.1f%% | 去重口径:同股信号间隔>%d根bar取首信号" %
             (meta["min_amount"] / 1e8, meta["max_chg"] * 100, meta["dedup_cooldown"]))
    L.append("")

    L += ["## 1. 总体:有没有优势", ""]
    rows = [cohort_row("全体信号", ds), cohort_row("去重后(ep首信号)", dedup)]
    L += [horizon_table(rows), "", pool_table(rows), "",
          "*理想期望=0.08×hit率−0.05×miss率(池止盈止损框架下的单位信号理论上限,未计滑点/买不进)", ""]

    L += ["## 2. 分年稳定性(全体口径)", ""]
    yr = ds.copy()
    yr["year"] = yr["date"].str[:4]
    yrows = [cohort_row(y, g) for y, g in yr.groupby("year")]
    L += [md_table(["年份", "n", "超额@1D", "超额@10D", "超额@20D", "hit率", "miss率", "理想期望"],
                   [[r["name"], r["n"], _pct(r["e1"]), _pct(r["e10"]), _pct(r["e20"]),
                     _pct(r["hit"], False), _pct(r["miss"], False), _pct(r["expect"])]
                    for r in yrows]), ""]

    L += ["## 3. 条件分解(去重口径)", ""]
    dd = dedup.copy()
    dd["_J"] = dd["j_low"].map(j_bin)
    dd["_DD"] = dd["dd"].map(dd_bin)
    dd["_VOL"] = dd["vol_ratio"].map(lambda v: _bin2(v, 1.0, "量比≥1", "量比<1"))
    dd["_SHR"] = dd["shrink"].map(lambda v: _bin2(v, 0.8, "缩量比≥0.8", "缩量比<0.8"))
    dd["_ENV"] = np.where(dd["env_bull"], "牛市", "熊市")
    dd["_PF"] = np.where(dd["pass_filter"], "过硬过滤", "未过硬过滤")
    L += ["### 3.1 单条件", ""]
    for name, col in [("市场环境", "_ENV"), ("J深度", "_J"), ("回撤深度", "_DD"),
                      ("量比(v/vma20)", "_VOL"), ("缩量比", "_SHR"), ("硬过滤前后", "_PF")]:
        L += ["**%s**" % name, "", cond_table(dd[dd[col] != "?"], col), ""]
    dd["_JB"] = dd["_ENV"] + " & " + dd["_J"]
    L += ["### 3.2 环境×J深度", "", cond_table(dd[dd["_J"] != "?"], "_JB"), ""]
    dd3 = dd[(dd["_J"] != "?") & (dd["_DD"] != "?")].copy()
    dd3["_CELL"] = dd3["_ENV"] + " & " + dd3["_J"] + " & " + dd3["_DD"]
    big = dd3.groupby("_CELL")["_CELL"].transform("size") >= MIN_CELL_N
    L += ["### 3.3 环境×J×回撤(n≥%d 才列)" % MIN_CELL_N, "", cond_table(dd3[big], "_CELL"), ""]

    L += ["## 4. 机械结论(规则化提取,解读见 AI 初读)", ""]
    all_e10 = float((ds["r10"] - ds["b10"]).median())
    ded_e10 = float((dedup["r10"] - dedup["b10"]).median())
    L.append("- 全体超额@10D中位 %s;去重后 %s(去重后更接近真实可操作口径)" %
             (_pct(all_e10), _pct(ded_e10)))
    cells = []
    for key, g in dd3.groupby("_CELL"):
        if len(g) >= NOTE_N:
            e = float((g["r10"] - g["b10"]).median())
            if abs(e) >= EXCESS_ATTN:
                cells.append((key, len(g), e))
    cells.sort(key=lambda t: -t[2])
    for key, n, e in cells:
        L.append("- 格子 **%s**:n=%d,超额@10D %s(%s向,探索性)" % (key, n, _pct(e), "正" if e > 0 else "负"))
    if not cells:
        L.append("- 无 n≥%d 且 |超额|≥%.1f%% 的格子" % (NOTE_N, EXCESS_ATTN * 100))
    L.append("")

    L += ["## 5. 已知偏差(读数前必看)", ""]
    L += ["- %s" % f for f in FOOTNOTES]
    L += ["", "## 6. AI 初读(探索性,待时间切分与前向验证)", "",
          ">(本节由 AI 基于上表追加,结论一律探索性,不构成换版依据)", ""]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("报告 → %s" % out_path)
    return out_path


if __name__ == "__main__":
    build_report()
