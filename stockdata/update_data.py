#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""update_data.py —— 每日数据更新的数据面:下载→gzip校验→安全解压→staging校验→原子换新→立即清理。
  python3 update_data.py            # 日常(远程数据不新则退出 1)
  python3 update_data.py --force    # 数据未前进也强制换新(修复现行数据损坏用)
退出码:0=换新完成 1=无新数据 2=下载/校验/换新失败。
磁盘策略:tar.gz 与 staging 处理完即删;.old 换新成功后立即删(数据可重下,不留回退窗口)。
不做远程预检(旧版会把 539MB 读进内存只为读日历末)——下载一次、本地比较。
指标计算/预警不在本文件,由 daily_update.sh 编排(分批独立进程,防 OOM)。
"""
import argparse
import gzip
import logging
import os
import shutil
import sys
import tarfile
import time
import urllib.request
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = "/home/admin/stockdata"
QLIB_BIN = os.path.join(DOWNLOAD_DIR, "qlib_bin")
SAMPLES = [("sh600000", "sh"), ("sz000651", "sz"), ("bj430017", "bj")]
RELEASE_URL = "https://gh-proxy.com/https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz"

log = logging.getLogger("update_data")


def current_end():
    p = os.path.join(QLIB_BIN, "calendars", "day.txt")
    return open(p, encoding="utf-8").read().split()[-1] if os.path.isfile(p) else None


def decide(current, staging, force=False):
    """换新决策:staging 更新才换;force 无视新旧;首跑(无现行数据)必换。"""
    if force or current is None or staging > current:
        return "swap"
    return "noop"


def download_package(url, dest):
    """流式下载到 dest(不进内存),返回路径。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    log.info("下载 %s → %s", url, dest)
    urllib.request.urlretrieve(url, dest)
    log.info("下载完成 %.1f MB", os.path.getsize(dest) / (1 << 20))
    return dest


def verify_gzip(path):
    with gzip.open(path, "rb") as f:
        while f.read(1 << 20):
            pass


def extract(path, staging):
    """解压到 staging;filter=data 拒绝绝对路径/../穿越/特殊文件,非法成员直接抛错。"""
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    with tarfile.open(path) as tf:
        members = tf.getmembers()
        tf.extractall(staging, members=members, filter="data")
    return len(members)


def validate(staging):
    """staging 结构/样本校验。返回 (staging 日历末, 错误信息);err 非 None 即不可换新。"""
    base = os.path.join(staging, "qlib_bin")
    inst = os.path.join(base, "instruments", "all.txt")
    if not os.path.isfile(inst) or len(open(inst, encoding="utf-8").read().splitlines()) == 0:
        return None, "instruments/all.txt 缺失或为空"
    import numpy as np
    for sym, _ in SAMPLES:
        p = os.path.join(base, "features", sym, "close.day.bin")
        if not os.path.isfile(p) or np.fromfile(p, dtype="<f4", count=2).size != 2:
            return None, "样本股 %s bin 不可读" % sym
    end = open(os.path.join(base, "calendars", "day.txt"), encoding="utf-8").read().split()[-1]
    return end, None


def _retry(fn, times=3, wait=1.0):
    for i in range(times):
        try:
            return fn()
        except OSError:
            if i == times - 1:
                raise
            time.sleep(wait)


def swap():
    """staging/qlib_bin → QLIB_BIN 原子换新;失败自动回滚;成功后立即删 .old。"""
    old = QLIB_BIN + ".old"
    new = os.path.join(DOWNLOAD_DIR, "staging", "qlib_bin")
    if os.path.isdir(old):
        shutil.rmtree(old)
    if os.path.isdir(QLIB_BIN):
        _retry(lambda: os.rename(QLIB_BIN, old))
    try:
        _retry(lambda: os.rename(new, QLIB_BIN))
    except OSError:
        if os.path.isdir(old):
            _retry(lambda: os.rename(old, QLIB_BIN))
        raise
    shutil.rmtree(old, ignore_errors=True)      # 换新成功:.old 立即删(磁盘策略)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)

    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(os.path.join(HERE, "logs", "%s.log" % date.today()),
                                      encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])

    pkg_path = os.path.join(DOWNLOAD_DIR, "qlib_bin.tar.gz")
    staging = os.path.join(DOWNLOAD_DIR, "staging")
    try:
        # 1. 下载一次(流式落盘)+ gzip 完整性
        try:
            download_package(RELEASE_URL, pkg_path)
            verify_gzip(pkg_path)
        except Exception as e:
            log.error("下载/校验失败: %s", e)
            return 2

        # 2. 解压 + staging 校验
        try:
            n = extract(pkg_path, staging)
            log.info("解压 %d 成员", n)
        except Exception as e:
            log.error("解压失败(疑似损坏或含非法路径): %s", e)
            return 2
        end, err = validate(staging)
        if err:
            log.error("staging 校验失败: %s", err)
            return 2

        # 3. 决策 + 换新(成功即清理 .old)
        cur = current_end()
        if decide(cur, end, a.force) == "noop":
            log.info("无新数据(现行 %s ≥ 远程 %s),退出", cur, end)
            return 1
        swap()
        log.info("换新完成,现行末 %s", end)
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if os.path.isfile(pkg_path):
            os.remove(pkg_path)
            log.info("已清理临时包 %s", pkg_path)


if __name__ == "__main__":
    sys.exit(main())
