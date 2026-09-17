# -*- coding: utf-8 -*-
"""pool.py —— pool.json 状态机:IO(备份/恢复)、入池校验、跟踪三判结案、标注(spec §5/§7)。"""
import json
import os
import shutil


class PoolCorruptError(RuntimeError):
    """pool.json 与 .bak 均损坏——停机报错,绝不静默清空案例库。"""


def load_pool(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"version": 1, "pool": []}
    except json.JSONDecodeError:
        bak = path + ".bak"
        if os.path.isfile(bak):
            try:
                with open(bak, encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                raise PoolCorruptError("pool.json 与 .bak 均损坏,无法恢复")
        raise PoolCorruptError("pool.json 损坏且无 .bak 可恢复")


def save_pool(pool, path):
    if os.path.isfile(path):
        shutil.copy2(path, path + ".bak")        # 备份上一版
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)                        # 原子替换


def watching(pool):
    return [e for e in pool["pool"] if e["status"] == "watching"]
