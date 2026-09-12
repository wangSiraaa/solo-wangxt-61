"""离线虚构种子数据（仅用于演示/测试，不代表真实港口或潮位）。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import select

from .database import Base, SessionLocal, engine
from . import models

# 基准：2026-09-15（虚构）
D0 = datetime(2026, 9, 15, 0, 0)


def tide_series(fairway: str, high_waters: list[datetime], amp: float = 1.5,
                mean: float = 2.0, step_minutes: int = 10):
    """生成尖峰形态的虚构潮位：height = mean + amp*cos^4(2π·Δ/T)，T≈12h25m。

    cos^4 使高潮附近窗口较窄、低潮附近较平坦，便于演示跨午夜窄窗口。
    每个栅格时刻只按时间上最近的高潮计算；高潮峰间存在合理低潮谷。
    """
    period = 12 * 60 + 25
    out = []
    t = high_waters[0] - timedelta(hours=7)
    end = high_waters[-1] + timedelta(hours=7)
    seen = set()
    while t <= end:
        if t not in seen:
            delta = min(abs((t - hw).total_seconds()) / 60 for hw in high_waters)
            c = math.cos(math.pi * delta / period)
            h = mean + amp * c ** 4
            out.append(models.TideSample(fairway_code=fairway, ts=t, height_m=round(h, 3)))
            seen.add(t)
        t += timedelta(minutes=step_minutes)
    return out


def seed(db=None):
    own_session = db is None
    if own_session:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
    try:
        if db.scalar(select(models.Fairway).limit(1)):
            return  # 已有数据，幂等

        # 航道：海图基准水深 6.0m，UKC 0.5m
        fw = models.Fairway(code="CH01", name="虚构主航道", chart_datum_depth_m=6.0,
                            safety_ukc_m=0.5, min_tide_m=0.0, max_tide_m=None)
        db.add(fw)

        vessels = [
            models.Vessel(imo="V-SMALL", name="虚构小货轮", draft_m=5.5, loa_m=120,
                          required_qualification="GENERAL", fairway_code="CH01"),
            models.Vessel(imo="V-DEEP", name="虚构深吃水货轮", draft_m=8.4, loa_m=230,
                          required_qualification="GENERAL", fairway_code="CH01"),
            models.Vessel(imo="V-TANKER", name="虚构油轮", draft_m=8.4, loa_m=250,
                          required_qualification="TANKER", fairway_code="CH01"),
        ]
        db.add_all(vessels)

        # 高潮时刻（虚构）：
        #   9/15 00:12、9/15 12:37、9/16 01:02、9/16 13:27、9/17 01:52、9/17 14:17
        highs = [
            D0.replace(hour=0, minute=12),
            D0.replace(hour=12, minute=37),
            D0 + timedelta(hours=24, minutes=50),
            D0 + timedelta(hours=37, minutes=27),
            D0 + timedelta(hours=49, minutes=52),
            D0 + timedelta(hours=62, minutes=17),
        ]
        db.add_all(tide_series("CH01", highs, amp=3.2, mean=-0.2))

        # 引航员（班期为 ISO 字符串，支持跨午夜夜班）
        # 夜班覆盖三个高潮夜；P1/P2/P3 有 9/16 凌晨夜班，P4 仅日班
        night_1 = (D0 - timedelta(hours=4), D0 + timedelta(hours=4))
        day_1 = (D0 + timedelta(hours=8), D0 + timedelta(hours=18))
        night_2 = (D0 + timedelta(hours=20), D0 + timedelta(hours=29))
        day_2 = (D0 + timedelta(hours=32), D0 + timedelta(hours=42))
        night_3 = (D0 + timedelta(hours=44), D0 + timedelta(hours=53))
        day_3 = (D0 + timedelta(hours=56), D0 + timedelta(hours=66))

        def ww(*pairs):
            return [{"start": a.isoformat(), "end": b.isoformat()} for a, b in pairs]

        db.add_all([
            models.Pilot(code="P1", name="引航员甲",
                         qualifications=["GENERAL", "TANKER"],
                         work_windows=ww(night_1, day_1, night_2, day_2, night_3, day_3)),
            models.Pilot(code="P2", name="引航员乙",
                         qualifications=["GENERAL"],
                         work_windows=ww(night_1, day_1, night_2, day_2, night_3, day_3)),
            models.Pilot(code="P3", name="引航员丙",
                         qualifications=["GENERAL", "TANKER"],
                         work_windows=ww(night_1, night_2, day_2, night_3, day_3)),
            models.Pilot(code="P4", name="引航员丁",
                         qualifications=["GENERAL"],
                         work_windows=ww(night_1, day_1, night_2, day_2, day_3)),
            models.Pilot(code="P5", name="引航员戊",
                         qualifications=["GENERAL"],
                         work_windows=ww(night_1, night_2, day_2, day_3)),
        ])

        # 接送艇：两条 1 座（夜潮极窄窗内多艘船并行登轮会超出 2 座位，演示容量不足）
        db.add_all([
            models.Boat(code="B1", name="引航艇一号", seats=1),
            models.Boat(code="B2", name="引航艇二号", seats=1),
        ])

        # 转场时间矩阵（分钟，虚构）
        pairs = {
            ("BAR", "DOCKS"): 20, ("DOCKS", "BAR"): 20,
            ("BAR", "BAR"): 0, ("DOCKS", "DOCKS"): 0,
            ("BAR", "ANCH"): 10, ("ANCH", "BAR"): 10,
            ("DOCKS", "ANCH"): 25, ("ANCH", "DOCKS"): 25,
        }
        db.add_all([
            models.TravelTime(from_code=a, to_code=b, minutes=m)
            for (a, b), m in pairs.items()
        ])

        db.commit()
    finally:
        if own_session:
            db.close()


if __name__ == "__main__":
    seed()
    print("seed done (fictional data)")
