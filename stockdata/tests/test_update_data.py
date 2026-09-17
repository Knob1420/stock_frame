# -*- coding: utf-8 -*-
"""update_data.py 单测:本地校验(不再远程预检)、换新决策、.old 即删、回滚、路径穿越拒绝。"""
import io
import tarfile

import numpy as np
import pytest

import update_data


# ---------- 测试用具:构造假 qlib_bin 树 ----------
def make_tree(root, end="2026-09-16", instruments="sh600000\tX\t1\t2\n"):
    """root/qlib_bin/{calendars,instruments,features×3样本股};返回 qlib_bin 路径。"""
    base = root / "qlib_bin"
    for sub in ("calendars", "instruments"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    for sym in ("sh600000", "sz000651", "bj430017"):
        d = base / "features" / sym
        d.mkdir(parents=True, exist_ok=True)
        (d / "close.day.bin").write_bytes(np.float32([1.0, 2.0]).tobytes())
    (base / "calendars" / "day.txt").write_text("2026-09-15\n%s\n" % end, encoding="utf-8")
    (base / "instruments" / "all.txt").write_text(instruments, encoding="utf-8")
    return base


def patch_paths(monkeypatch, tmp_path):
    """把模块里的正式路径指到 tmp;返回 (qlib_bin, download_dir)。"""
    monkeypatch.setattr(update_data, "QLIB_BIN", str(tmp_path / "qlib_bin"))
    monkeypatch.setattr(update_data, "DOWNLOAD_DIR", str(tmp_path))
    return tmp_path / "qlib_bin", tmp_path


# ---------- validate:返回 (staging_end, err) ----------
def test_validate_ok(tmp_path):
    base = make_tree(tmp_path)
    end, err = update_data.validate(str(tmp_path))
    assert err is None
    assert end == "2026-09-16"


def test_validate_missing_instruments(tmp_path):
    base = make_tree(tmp_path)
    (base / "instruments" / "all.txt").unlink()
    end, err = update_data.validate(str(tmp_path))
    assert err is not None and "instruments" in err


def test_validate_empty_instruments(tmp_path):
    base = make_tree(tmp_path, instruments="")
    end, err = update_data.validate(str(tmp_path))
    assert err is not None and "instruments" in err


def test_validate_broken_sample_bin(tmp_path):
    base = make_tree(tmp_path)
    (base / "features" / "sh600000" / "close.day.bin").write_bytes(b"\x00")
    end, err = update_data.validate(str(tmp_path))
    assert err is not None and "sh600000" in err


# ---------- decide:换新决策(纯函数) ----------
@pytest.mark.parametrize("cur,staging,force,expected", [
    ("2026-09-15", "2026-09-16", False, "swap"),   # 有新数据
    ("2026-09-16", "2026-09-16", False, "noop"),   # 持平
    ("2026-09-17", "2026-09-16", False, "noop"),   # 反而更旧
    ("2026-09-16", "2026-09-16", True, "swap"),    # force 强制换新
    (None, "2026-09-16", False, "swap"),           # 首跑无现行数据
])
def test_decide(cur, staging, force, expected):
    assert update_data.decide(cur, staging, force) == expected


# ---------- swap:成功即删 .old;失败回滚 ----------
def test_swap_deletes_old_immediately(tmp_path, monkeypatch):
    cur = tmp_path / "qlib_bin"
    cur.mkdir()
    (cur / "old.txt").write_text("old", encoding="utf-8")
    new = tmp_path / "staging" / "qlib_bin"
    new.mkdir(parents=True)
    (new / "new.txt").write_text("new", encoding="utf-8")
    patch_paths(monkeypatch, tmp_path)
    update_data.swap()
    assert (cur / "new.txt").exists()          # 新数据就位
    assert not (cur / "old.txt").exists()      # 旧内容不在现行目录
    assert not (tmp_path / "qlib_bin.old").exists()   # .old 不保留


def test_swap_rollback_when_new_missing(tmp_path, monkeypatch):
    cur = tmp_path / "qlib_bin"
    cur.mkdir()
    (cur / "keep.txt").write_text("keep", encoding="utf-8")
    (tmp_path / "staging").mkdir()             # 空 staging:第二步 rename 必失败
    patch_paths(monkeypatch, tmp_path)
    with pytest.raises(OSError):
        update_data.swap()
    assert (cur / "keep.txt").exists()         # 现行数据已回滚
    assert not (tmp_path / "qlib_bin.old").exists()


# ---------- extract:正常包可解,路径穿越成员拒绝 ----------
def _make_tar(path, members):
    """members: [(arcname, bytes)] 构造纯文件 tar。"""
    with tarfile.open(path, "w") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_extract_good_package(tmp_path):
    tar = tmp_path / "pkg.tar"
    _make_tar(tar, [("qlib_bin/calendars/day.txt", b"2026-09-16\n")])
    staging = tmp_path / "out"
    staging.mkdir()
    n = update_data.extract(str(tar), str(staging))
    assert n == 1
    assert (staging / "qlib_bin" / "calendars" / "day.txt").read_bytes() == b"2026-09-16\n"


def test_extract_rejects_traversal(tmp_path):
    tar = tmp_path / "pkg.tar"
    _make_tar(tar, [("../evil.txt", b"evil")])
    staging = tmp_path / "out"
    staging.mkdir()
    with pytest.raises(Exception):             # 穿越成员 → 拒绝并抛错(调用方按失败处理)
        update_data.extract(str(tar), str(staging))
    assert not (tmp_path / "evil.txt").exists()


# ---------- current_end ----------
def test_current_end_reads_calendar(tmp_path, monkeypatch):
    make_tree(tmp_path)
    patch_paths(monkeypatch, tmp_path)
    assert update_data.current_end() == "2026-09-16"


def test_current_end_none_when_absent(tmp_path, monkeypatch):
    patch_paths(monkeypatch, tmp_path)         # 空目录,无 qlib_bin
    assert update_data.current_end() is None
