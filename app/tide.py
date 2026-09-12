"""潮位通航窗口计算（离线虚构数据 / 示例吃水规则，不用于真实航行决策）。

示例规则：
    可用水深 = 海图基准水深 + 潮位
    要求    可用水深 >= 船舶吃水 + 富余水深(UKC)
    => 所需潮位下限 = 吃水 + UKC - 海图基准水深
    另有航道级潮位上/下限（max_tide 用于模拟“流速过大禁止通航”）。

离散潮位样本之间按线性插值处理；以 1 分钟为栅格判断可行性，
再把连续可行分钟合并为半开时间区间，天然支持跨午夜窗口。
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta

ONE_MINUTE = timedelta(minutes=1)


def required_tide_lower(draft_m: float, ukc_m: float, chart_depth_m: float, min_tide_m: float) -> float:
    return max(draft_m + ukc_m - chart_depth_m, min_tide_m)


def _interpolate(samples: list[tuple[datetime, float]], t: datetime) -> float | None:
    """samples 已按时间升序；返回 t 处线性插值潮位，样本覆盖不到返回 None。"""
    if not samples or t < samples[0][0] or t > samples[-1][0]:
        return None
    keys = [s[0] for s in samples]
    i = bisect_right(keys, t) - 1
    if i >= len(samples) - 1:
        return samples[-1][1]
    (a, ha), (b, hb) = samples[i], samples[i + 1]
    if b == a:
        return ha
    frac = (t - a).total_seconds() / (b - a).total_seconds()
    return ha + (hb - ha) * frac


def feasible_tide_windows(
    samples: list[tuple[datetime, float]],
    lower_m: float,
    upper_m: float | None,
    horizon_start: datetime,
    horizon_end: datetime,
) -> list[tuple[datetime, datetime]]:
    """返回半开区间列表 [start, end)，可跨越午夜。"""
    samples = sorted(samples, key=lambda s: s[0])
    windows: list[tuple[datetime, datetime]] = []
    run_start: datetime | None = None
    t = horizon_start
    while t < horizon_end:
        h = _interpolate(samples, t)
        ok = h is not None and h >= lower_m and (upper_m is None or h <= upper_m)
        if ok and run_start is None:
            run_start = t
        elif not ok and run_start is not None:
            windows.append((run_start, t))
            run_start = None
        t += ONE_MINUTE
    if run_start is not None:
        windows.append((run_start, horizon_end))
    return windows


def intersect_windows(
    windows: list[tuple[datetime, datetime]],
    bounds: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """两组半开区间求交（bounds 为班期/期望窗口等）。"""
    out: list[tuple[datetime, datetime]] = []
    for a_start, a_end in windows:
        for b_start, b_end in bounds:
            s, e = max(a_start, b_start), min(a_end, b_end)
            if s < e:
                out.append((s, e))
    return out


def merge_windows(windows: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """合并相邻/重叠的半开区间（相隔 1 分钟即视为相邻）。"""
    merged: list[tuple[datetime, datetime]] = []
    for s, e in sorted(windows):
        if merged and s <= merged[-1][1] + ONE_MINUTE:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged
