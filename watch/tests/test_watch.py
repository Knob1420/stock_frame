import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from watch import rules, scan
from kdj.layers import baseline_original


def frame(last_volume=50.0, periods=260):
    index = pd.bdate_range("2025-01-01", periods=periods)
    close = np.linspace(10.0, 20.0, periods)
    volume = np.full(periods, 100.0)
    volume[-1] = last_volume
    return pd.DataFrame(
        {
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": volume,
            "factor": np.ones(periods),
            "kdj_k": np.full(periods, 20.0),
            "kdj_d": np.full(periods, 20.0),
            "kdj_j": np.r_[np.full(periods - 2, 20.0), -5.0, -1.0],
            "atr14": np.full(periods, 0.4),
        },
        index=index,
    )


def write_fixture(tmp_path, stock_count=2, max_push=1):
    indicator_dir = tmp_path / "indicators"
    indicator_dir.mkdir()
    watchlist = []
    for number in range(stock_count):
        code = "sh60000%d" % number
        frame().to_parquet(indicator_dir / (code + ".parquet"))
        watchlist.append({"code": code, "name": "测试%d" % number})
    config = {
        "defaults": {
            "alerting": {
                "min_score": 4,
                "min_categories": 2,
                "max_push_stocks": max_push,
                "cooldown_bars": 10,
            },
            "rules": [
                {
                    "rule": "volume_dry_up",
                    "standalone": True,
                    "weight": 5,
                    "params": {"ratio": 0.75},
                }
            ],
        },
        "watchlist": watchlist,
    }
    config_path = tmp_path / "watchlist.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return config_path, indicator_dir, watchlist


def test_b1_hook_is_low_turn_confirmation():
    data = frame()
    assert rules.rule_b1_hook(data, {"threshold": 15, "require_good": True})
    data.loc[data.index[-1], "kdj_j"] = -10.0
    assert not rules.rule_b1_hook(data, {"threshold": 15, "require_good": True})


def test_default_b1_rule_matches_project_formula():
    data = frame()
    expected = baseline_original(data.assign(j=data["kdj_j"])).iloc[-1]
    assert rules.rule_b1(data, {"no_good": False}) == bool(expected)


def test_same_category_only_contributes_highest_weight():
    active = [
        {"category": "support", "weight": 1},
        {"category": "support", "weight": 3},
        {"category": "volume", "weight": 1},
    ]
    score, categories = scan._score(active)
    assert score == 4
    assert categories == {"support": 3, "volume": 1}


def test_preview_caps_alerts_and_does_not_write_state(tmp_path):
    config_path, indicator_dir, _ = write_fixture(tmp_path)
    state_path = tmp_path / "state.json"
    result = scan.run(
        config_path=config_path,
        state_path=state_path,
        archive_path=tmp_path / "archive.parquet",
        indicator_dir=indicator_dir,
        preview=True,
        persist=False,
    )
    assert len(result["events"]) == 1
    assert result["stats"]["cap_suppressed"] == 1
    assert not state_path.exists()
    report = scan.format_report(result)
    assert "提醒 1 只" in report
    assert "提醒分" in report
    assert "候补观察" in report
    assert "预览模式不写归档" in report
    assert "后续确认" in report
    assert "跟踪中" not in report


def test_first_real_run_builds_silent_baseline(tmp_path):
    config_path, indicator_dir, _ = write_fixture(tmp_path, stock_count=1)
    state_path = tmp_path / "state.json"
    result = scan.run(
        config_path=config_path,
        state_path=state_path,
        archive_path=tmp_path / "archive.parquet",
        indicator_dir=indicator_dir,
    )
    assert result["bootstrap"] is True
    assert result["events"] == []
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2
    assert not (tmp_path / "archive.parquet").exists()


def test_legacy_state_is_migrated_as_a_silent_baseline(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"sh600000|b1|": [True, "2026-09-18"]}),
        encoding="utf-8",
    )
    state, bootstrapping = scan._load_state(path)
    assert bootstrapping is True
    assert state["schema_version"] == 2
    assert state["rules"]["sh600000|b1|"]["active"] is True


def test_alert_is_archived_once_and_continuous_state_does_not_repeat(tmp_path):
    config_path, indicator_dir, _ = write_fixture(tmp_path, stock_count=1)
    state_path = tmp_path / "state.json"
    archive_path = tmp_path / "archive.parquet"
    first = scan.run(
        config_path=config_path,
        state_path=state_path,
        archive_path=archive_path,
        indicator_dir=indicator_dir,
        bootstrap_alerts=True,
    )
    second = scan.run(
        config_path=config_path,
        state_path=state_path,
        archive_path=archive_path,
        indicator_dir=indicator_dir,
    )
    assert len(first["events"]) == 1
    assert second["events"] == []
    assert len(pd.read_parquet(archive_path)) == 1


def test_cap_suppressed_candidates_are_also_archived(tmp_path):
    config_path, indicator_dir, _ = write_fixture(tmp_path, stock_count=2, max_push=1)
    archive_path = tmp_path / "archive.parquet"
    result = scan.run(
        config_path=config_path,
        state_path=tmp_path / "state.json",
        archive_path=archive_path,
        indicator_dir=indicator_dir,
        bootstrap_alerts=True,
    )
    archived = pd.read_parquet(archive_path)
    assert len(result["events"]) == 1
    assert len(archived) == 2
    assert set(archived.delivery_status) == {"selected", "cap_suppressed"}


def test_appending_new_archive_rows_preserves_legacy_rule_rows(tmp_path):
    config_path, indicator_dir, watchlist = write_fixture(tmp_path, stock_count=1)
    archive_path = tmp_path / "archive.parquet"
    pd.DataFrame(
        [
            {"date": "2025-01-01", "code": watchlist[0]["code"], "rule": "b1"},
            {"date": "2025-01-01", "code": watchlist[0]["code"], "rule": "near_ma"},
        ]
    ).to_parquet(archive_path, index=False)
    scan.run(
        config_path=config_path,
        state_path=tmp_path / "state.json",
        archive_path=archive_path,
        indicator_dir=indicator_dir,
        bootstrap_alerts=True,
    )
    assert len(pd.read_parquet(archive_path)) == 3


def test_recent_stock_alert_is_suppressed_by_cooldown(tmp_path):
    config_path, indicator_dir, watchlist = write_fixture(tmp_path, stock_count=1)
    data = pd.read_parquet(indicator_dir / (watchlist[0]["code"] + ".parquet"))
    today = str(data.index[-1].date())
    state = {
        "schema_version": 2,
        "rules": {
            watchlist[0]["code"] + "|volume_dry_up|": {
                "active": False,
                "since": None,
                "last_transition": None,
            }
        },
        "stocks": {
            watchlist[0]["code"]: {
                "last_alert_date": today,
                "last_alert_score": 5,
                "last_tier": "重点观察",
            }
        },
    }
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    result = scan.run(
        config_path=config_path,
        state_path=state_path,
        archive_path=tmp_path / "archive.parquet",
        indicator_dir=indicator_dir,
        persist=False,
    )
    assert result["events"] == []
    assert result["stats"]["cooldown_suppressed"] == 1


def test_llm_output_must_be_structured_and_cannot_add_numbers():
    good = json.dumps(
        {
            "items": [
                {
                    "code": "sh600000",
                    "state": "等待确认",
                    "summary": "缩量回到支撑附近，仍需确认。",
                    "supporting_evidence": ["趋势尚未转坏"],
                    "contrary_evidence": ["尚未出现持续转强证据"],
                    "thesis_check": "未提供原关注逻辑，无法对照",
                    "watch_for": ["观察能否站稳短期均线"],
                    "risk": "继续走弱会破坏止跌逻辑",
                }
            ]
        },
        ensure_ascii=False,
    )
    assert scan._parse_briefs(good, {"sh600000"})["sh600000"]["summary"]
    bad = good.replace("趋势尚未转坏", "还有百分之十空间")
    # 中文数词允许；程序禁止的是模型另造机器可误认成价位的阿拉伯数字。
    assert scan._parse_briefs(bad, {"sh600000"})
    with pytest.raises(ValueError):
        scan._parse_briefs(good.replace("趋势尚未转坏", "压力位21.5"), {"sh600000"})


def test_llm_prompt_requires_counterevidence_and_respects_data_boundary():
    prompt = scan._build_llm_prompt([{"code": "sh600000"}])
    assert "必须给至少一条反面证据" in prompt
    assert "不得补充新闻" in prompt
    assert "提醒分只用于消息排序" in prompt


def test_markdown_chunks_respect_utf8_byte_limit():
    chunks = scan._split_markdown(("中文内容" * 100) + "\n\n" + ("下一只" * 100), 180)
    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-8")) <= 180 for chunk in chunks)
