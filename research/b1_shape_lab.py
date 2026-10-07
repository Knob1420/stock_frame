# -*- coding: utf-8 -*-
"""Path-shape labels for AMV-allowed B1 signals + T0 feature discrimination.

Labels (frozen, close-path 15d vs next-open entry, costs excluded):
  A1 d1>0 end>0 mae>-2%   A2 d1>0 end>0 mae<=-2%
  B  d1>0 end<=0          C  d1<=0 end>0
  D1 d1<=0 end<=0 mfe<2%  D2 d1<=0 end<=0 mfe>=2%
Outputs: labels parquet, average-path plot, AUC feature contrasts for six
pairwise targets with year-consistency, and per-class feature means.
Diagnostic only; no account claims.
"""
from pathlib import Path
import time

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from b1_amv import load_input, local_calendar, states, digest, write_json
from b1_amv_gate import AMV_PATH, load_frozen

HERE = Path(__file__).resolve().parent
PRED = HERE / "out/selection_lab/h15/b1_predictions.parquet"
CODE = ["b1_amv.py", "b1_amv_gate.py", "b1_shape_lab.py"]
FEATURES = ["dist_ma5", "dist_ma20", "dist_ma60", "dist_ma120", "slope20", "slope60",
            "ret5", "ret20", "ret60", "atr_pct", "volatility20", "range10",
            "dist_high20", "dist_high60", "close_location", "upper_wick", "body",
            "volume_ratio", "volume_dry", "up_down_volume", "log_amount", "j", "rsi",
            "keyk_intact", "keyk_age", "keyk_distance", "above_ma60",
            "idx_slope20", "idx_ret20", "idx_vol20", "amt_ratio", "rotation", "b1plus"]
CONTRASTS = [("A1", "B"), ("A1", "D1"), ("C", "B"), ("C", "D1"), ("A1", "C"), ("B", "D1")]


def build_labels():
    normalized = load_input(AMV_PATH)
    cal = local_calendar()
    normalized = normalized[normalized.index.isin(cal)]
    table = states(normalized, cal)
    data, plans, prices, manifest = load_frozen()
    data = data[data.date >= "2020-01-01"]
    z = data.join(table[["known", "allow_next_open"]], on="date", validate="many_to_one")
    a = z[z.known.eq(True) & z.allow_next_open & z.entry_i.ge(0)].copy()
    close, open_ = prices["close"], prices["open"]
    ei = a.entry_i.to_numpy(int)
    si = a.sym_i.to_numpy(int)
    idx = ei[:, None] + np.arange(15)[None, :]
    cp = np.where(idx < close.shape[0],
                  close[np.clip(idx, 0, close.shape[0] - 1), si[:, None]], np.nan)
    rel = cp / open_[ei, si][:, None] - 1.0
    ok = np.isfinite(rel[:, 14])
    df = pd.DataFrame({
        "event_id": a.event_id.to_numpy()[ok], "sym": a.sym.to_numpy()[ok],
        "date": a.date.to_numpy()[ok], "year": a.date.dt.year.to_numpy()[ok],
        "d1": rel[:, 0][ok], "end": rel[:, 14][ok],
        "mfe": np.nanmax(rel, 1)[ok], "mae": np.nanmin(rel, 1)[ok],
        "peak_day": (np.nanargmax(rel, 1) + 1)[ok], "trough_day": (np.nanargmin(rel, 1) + 1)[ok]})
    df["path"] = np.select(
        [(df.d1 > 0) & (df.end > 0) & (df.mae > -.02), (df.d1 > 0) & (df.end > 0),
         (df.d1 > 0), (df.end > 0), (df.mfe < .02)],
        ["A1", "A2", "B", "C", "D1"], default="D2")
    # keep mean path matrix for the plot
    np.save(HERE / "out/b1_shape_lab_v1_rel.npy", rel[ok]) if (HERE / "out/b1_shape_lab_v1").exists() else None
    return df, rel[ok]


def auc(x, y):
    """P(feat_x > feat_y), 0.5=no separation."""
    r = pd.concat([x, y]).rank()
    nx = len(x)
    return (r.iloc[:nx].sum() - nx * (nx + 1) / 2) / (nx * len(y))


def run(out):
    started = time.monotonic()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    labels, rel = build_labels()
    labels.to_parquet(out / "labels.parquet", index=False)

    pred = pd.read_parquet(PRED)
    pred["date"] = pd.to_datetime(pred["date"])
    key = pd.Series(list(zip(labels.sym, pd.to_datetime(labels.date).dt.strftime("%Y-%m-%d"))),
                    index=labels.index)
    pmap = dict(zip(zip(pred.sym, pred.date.dt.strftime("%Y-%m-%d")),
                    zip(*[pred[c] for c in FEATURES])))
    feat = pd.DataFrame([pmap.get(k, (np.nan,) * len(FEATURES)) for k in key], columns=FEATURES)
    df = pd.concat([labels.reset_index(drop=True), feat], axis=1)

    # Average path plot
    fig, ax = plt.subplots(figsize=(8, 5))
    order = ["A1", "A2", "B", "C", "D1", "D2"]
    colors = {"A1": "#1a9850", "A2": "#66bd63", "B": "#f46d43", "C": "#d73027", "D1": "#8c510a", "D2": "#d8b365"}
    for c in order:
        m = df.index[df.path.eq(c)]
        ax.plot(range(1, 16), 100 * rel[m].mean(axis=0), label=f"{c} (n={len(m)})",
                color=colors[c], lw=2 if c in ("A1", "B", "C", "D1") else 1, alpha=.9)
    ax.axhline(0, color="k", lw=.5)
    ax.set_xlabel("持有日"); ax.set_ylabel("相对入场开盘 %"); ax.set_title("六类形态平均路径 (AMV放行, 2020+)")
    ax.legend(fontsize=8); fig.tight_layout(); fig.savefig(out / "avg_paths.png", dpi=150)
    plt.close(fig)

    # AUC contrasts with year consistency
    rows = []
    for f in FEATURES:
        x = df[f]
        if x.notna().sum() < 1000 or x.nunique() < 5:
            continue
        for a_cls, b_cls in CONTRASTS:
            mask = df.path.isin([a_cls, b_cls]) & x.notna()
            sub = df[mask]
            v = x[mask]
            a_all = auc(v[sub.path.eq(a_cls)], v[sub.path.eq(b_cls)])
            yearly = []
            for y, g in sub.groupby("year"):
                va, vb = v[g.index][g.path.eq(a_cls)], v[g.index][g.path.eq(b_cls)]
                if len(va) > 50 and len(vb) > 50:
                    yearly.append(auc(va, vb))
            above = sum(1 for t in yearly if t > .5)
            rows.append(dict(feature=f, contrast=f"{a_cls}vs{b_cls}", auc=round(a_all, 4),
                             n=len(sub), years_above_50pct=f"{above}/{len(yearly)}",
                             year_min=round(min(yearly), 3) if yearly else None,
                             year_max=round(max(yearly), 3) if yearly else None))
    tab = pd.DataFrame(rows)
    tab.to_csv(out / "auc_contrasts.csv", index=False)
    strong = tab[(tab.years_above_50pct.str.split("/").str[0].astype(int) >= 6)
                 & ((tab.auc >= .55) | (tab.auc <= .45))]
    strong.to_csv(out / "auc_strong.csv", index=False)

    means = df.groupby("path")[FEATURES].mean().T.round(3)
    means.to_csv(out / "class_feature_means.csv")

    # D2 internal split: mfe>=10% vs rest
    d2 = df[df.path.eq("D2")]
    d2split = d2.assign(bucket=np.where(d2.mfe >= .10, "mfe>=10%", np.where(d2.mfe >= .05, "5-10%", "<5%"))) \
        .groupby("bucket").agg(n=("end", "size"), mean_end=("end", lambda s: 100 * s.mean()),
                               mean_mfe=("mfe", lambda s: 100 * s.mean()),
                               mean_trough_day=("trough_day", "mean"))
    d2split.to_csv(out / "d2_split.csv")

    design = dict(labels="A1/A2/B/C/D1/D2 frozen path classes, entry=next open, closes only",
                  universe="AMV-allowed B1 events 2020+, 40-day buffer",
                  features=len(FEATURES), contrasts=[f"{a}vs{b}" for a, b in CONTRASTS],
                  auc="Mann-Whitney P(x>y), pooled 2020-2026; year consistency reported",
                  code_hashes={f: digest(HERE / f) for f in CODE},
                  stage="diagnostic_labels_not_trading_rules",
                  limitations=["Ex-post labels; AUC>0.55 pooled AND 6/7 years consistent is the "
                               "promising bar, still needs frozen out-of-sample confirmation.",
                               "Classes are correlated within days; no clustering correction.",
                               "Prior experiments 9/12 failed to convert similar signals into "
                               "account edge; treat as hypothesis generation."])
    write_json(out / "design.json", design)
    print(f"labels={len(df)}; strong AUC rows={len(strong)}; saved to {out}")
    return tab


if __name__ == "__main__":
    run(HERE / "out/b1_shape_lab_v1")
