#!/usr/bin/env python3
"""tests/t046 공용 — 측정 모듈(scripts/t046)을 import 경로에 올린다.

`test_*.py` 가 아니므로 unittest discover 의 수집 대상이 아니다.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
T046 = os.path.join(ROOT, "scripts", "t046")

if T046 not in sys.path:
    sys.path.insert(0, T046)
