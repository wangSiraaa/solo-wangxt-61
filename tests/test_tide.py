"""潮位窗口纯函数测试：跨午夜窗口、上下限、插值。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.tide import feasible_tide_windows, intersect_windows, required_tide_lower

D0 = datetime(2026, 9, 15, 0, 0)


def test_required_tide():
    # 吃水 8.4 + UKC 0.5 - 基准水深 6.0 = 2.9
    assert required_tide_lower(8.4, 0.5, 6.0, 0.0) == pytest.approx(2.9)
    # 航道最低潮位下限优先
    assert required_tide_lower(5.5, 0.5, 6.0, 1.0) == pytest.approx(1.0)


def test_window_crosses_midnight():
    # 高潮在 00:30，阈值 2.9：可行窗口应同时覆盖午夜两侧
    samples = []
    for i in range(-12 * 2, 12 * 2 + 1):
        t = D0 + timedelta(minutes=30 * i)
        # 三角峰，30 分钟下降 0.05m（保证约 20 分钟可行窗）
        dh = abs(i)
        samples.append((t, 3.0 - 0.05 * dh))
    windows = feasible_tide_windows(samples, 2.9, None,
                                    D0 - timedelta(hours=3), D0 + timedelta(hours=3))
    assert len(windows) == 1
    s, e = windows[0]
    assert s < D0 < e, f"窗口 {s}~{e} 未跨越午夜"
    assert (e - s) >= timedelta(minutes=3)


def test_upper_tide_limit_excludes_high_water():
    samples = [(D0 + timedelta(minutes=i), 3.0) for i in range(0, 61)]
    # 上限 2.5：高潮位（强流速）期间不可通航
    windows = feasible_tide_windows(samples, 0.0, 2.5, D0, D0 + timedelta(hours=1))
    assert windows == []
    windows = feasible_tide_windows(samples, 0.0, 3.0, D0, D0 + timedelta(hours=1))
    assert len(windows) == 1


def test_intersect():
    a = [(D0, D0 + timedelta(hours=2))]
    b = [(D0 + timedelta(hours=1), D0 + timedelta(hours=3))]
    assert intersect_windows(a, b) == [(D0 + timedelta(hours=1), D0 + timedelta(hours=2))]
