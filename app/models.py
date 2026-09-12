"""ORM 模型。

时间一律存储为 naive UTC（ISO8601 字符串由 API 层负责转换）。
所有潮汐、吃水数据均为离线虚构示例。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Fairway(Base):
    """航道（含示例吃水/潮位规则）。"""

    __tablename__ = "fairways"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    chart_datum_depth_m: Mapped[float] = mapped_column(Float)  # 海图基准水深（虚构）
    safety_ukc_m: Mapped[float] = mapped_column(Float, default=0.5)  # 富余水深
    min_tide_m: Mapped[float] = mapped_column(Float, default=0.0)  # 允许最低潮位（可选流速下限）
    max_tide_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 允许最高潮位（可选，强流速限）


class Vessel(Base):
    __tablename__ = "vessels"

    imo: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    draft_m: Mapped[float] = mapped_column(Float)
    loa_m: Mapped[float] = mapped_column(Float, default=0.0)
    required_qualification: Mapped[str] = mapped_column(String(32), default="GENERAL")
    fairway_code: Mapped[str] = mapped_column(ForeignKey("fairways.code"))


class TideSample(Base):
    """离散潮位观测（虚构），相邻样本之间线性插值。"""

    __tablename__ = "tide_samples"
    __table_args__ = (UniqueConstraint("fairway_code", "ts", name="uq_tide_fairway_ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fairway_code: Mapped[str] = mapped_column(ForeignKey("fairways.code"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    height_m: Mapped[float] = mapped_column(Float)


class Pilot(Base):
    __tablename__ = "pilots"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    qualifications: Mapped[list] = mapped_column(JSON, default=list)  # ["GENERAL","TANKER",...]
    # 可工作时段列表：[{"start": iso8601, "end": iso8601}, ...]（支持跨午夜的夜班）
    work_windows: Mapped[list] = mapped_column(JSON, default=list)


class Boat(Base):
    """接送艇（引航艇/交通艇，虚构）。"""

    __tablename__ = "boats"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    seats: Mapped[int] = mapped_column(Integer, default=4)  # 同时可接送引航员数量


class TravelTime(Base):
    """离船后转场时间矩阵（分钟）：从引航站 from_code 到 to_code。"""

    __tablename__ = "travel_times"
    __table_args__ = (UniqueConstraint("from_code", "to_code", name="uq_travel_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_code: Mapped[str] = mapped_column(String(32), index=True)
    to_code: Mapped[str] = mapped_column(String(32), index=True)
    minutes: Mapped[int] = mapped_column(Integer, default=0)


class Declaration(Base):
    """船舶申报。

    committed=True 表示船方/调度已承诺的任务，求解时优先满足。
    ready_at/due_at 给出期望作业窗口；promised 任务已在正式计划中锁定。
    """

    __tablename__ = "declarations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # 幂等键
    vessel_imo: Mapped[str] = mapped_column(ForeignKey("vessels.imo"))
    movement: Mapped[str] = mapped_column(String(8), default="INBOUND")  # INBOUND / OUTBOUND
    ready_at: Mapped[datetime] = mapped_column(DateTime)
    due_at: Mapped[datetime] = mapped_column(DateTime)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    board_location: Mapped[str] = mapped_column(String(32), default="BAR")  # 登轮点
    land_location: Mapped[str] = mapped_column(String(32), default="DOCKS")  # 离船点
    committed: Mapped[bool] = mapped_column(Boolean, default=False)
    promised: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16), default="PROPOSED")  # PROPOSED / CONFIRMED
    horizon_start: Mapped[datetime] = mapped_column(DateTime)
    horizon_end: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    tasks: Mapped[list["PlanTask"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class PlanTask(Base):
    """计划任务。已确认计划中的任务构成下一轮求解时必须保留的承诺。

    修订会生成新版本计划并复制保留任务，因此同一申报可出现在多个计划版本中，
    但在同一个计划内只能出现一次。
    """

    __tablename__ = "plan_tasks"
    __table_args__ = (UniqueConstraint("plan_id", "declaration_id", name="uq_plan_task_plan_decl"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), index=True)
    declaration_id: Mapped[int] = mapped_column(ForeignKey("declarations.id"))
    pilot_code: Mapped[str] = mapped_column(ForeignKey("pilots.code"))
    start_boat_code: Mapped[str] = mapped_column(ForeignKey("boats.code"))
    end_boat_code: Mapped[str] = mapped_column(ForeignKey("boats.code"))
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)

    plan: Mapped["Plan"] = relationship(back_populates="tasks")


class Revision(Base):
    """正式计划的显式修订记录（正式计划锁定后仅允许显式修订）。"""

    __tablename__ = "revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    added_declaration_ids: Mapped[list] = mapped_column(JSON, default=list)
    cancelled_declaration_ids: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resulting_plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plans.id"), nullable=True)
