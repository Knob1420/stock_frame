# -*- coding: utf-8 -*-
"""让 tests/ 能 import stockdata/ 下的模块。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
