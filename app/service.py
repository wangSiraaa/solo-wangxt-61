"""服务层：潮汐窗口计算、求解输入组装、计划锁定/修订。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .config import settings
from .scheduler import (
    BoatInput,
    JobInput,
    PilotInput,
    SchedulerInput,
    solve,
)
from .tide import feasible_tide_windows, required_tide_lower


def parse_windows(raw: list[dict]) -> list[tuple[datetime, datetime]]:
    return [(_dt(w["start"]), _dt(w["end"])) for w in raw]


def _cancelled(db: Session, decl_id: int) -> bool:
    decl = db.get(models.Declaration, decl_id)
    return decl is None or decl.cancelled


def _dt(v) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(v)


def get_tide_samples(db: Session, fairway_code: str) -> list[tuple[datetime, float]]:
    rows = db.scalars(
        select(models.TideSample)
        .where(models.TideSample.fairway_code == fairway_code)
        .order_by(models.TideSample.ts)
    ).all()
    return [(r.ts, r.height_m) for r in rows]


def tide_windows_for(db: Session, vessel: models.Vessel,
                     lo: datetime, hi: datetime) -> list[tuple[datetime, datetime]]:
    fairway = db.get(models.Fairway, vessel.fairway_code)
    samples = get_tide_samples(db, fairway.code)
    lower = required_tide_lower(
        vessel.draft_m, fairway.safety_ukc_m,
        fairway.chart_datum_depth_m, fairway.min_tide_m,
    )
    return feasible_tide_windows(samples, lower, fairway.max_tide_m, lo, hi)


def build_and_solve(db: Session, horizon_start: datetime, horizon_end: datetime,
                    include_declaration_ids: Optional[list[int]] = None):
    # 两端各预留接送航程，避免接送区间落到视野之外
    tr = 5
    hs = horizon_start - timedelta(minutes=tr)
    he = horizon_end + timedelta(minutes=tr)

    travel = {(t.from_code, t.to_code): t.minutes for t in db.scalars(select(models.TravelTime)).all()}
    pilots = [
        PilotInput(code=p.code, qualifications=list(p.qualifications), work_windows=parse_windows(p.work_windows))
        for p in db.scalars(select(models.Pilot)).all()
    ]
    boats = [BoatInput(code=b.code, seats=b.seats) for b in db.scalars(select(models.Boat)).all()]

    # 已锁定（已确认计划中的任务）作为固定占用
    fixed_tasks = db.scalars(
        select(models.PlanTask)
        .join(models.Plan, models.PlanTask.plan_id == models.Plan.id)
        .where(models.Plan.status == "CONFIRMED")
    ).all()
    fixed_decl_ids = {t.declaration_id for t in fixed_tasks if not _cancelled(db, t.declaration_id)}
    fixed_tasks = [t for t in fixed_tasks if not _cancelled(db, t.declaration_id)]

    jobs: list[JobInput] = []
    decl_by_id: dict[int, models.Declaration] = {}

    for t in fixed_tasks:
        decl = db.get(models.Declaration, t.declaration_id)
        if decl.cancelled:
            continue
        vessel = db.get(models.Vessel, decl.vessel_imo)
        jobs.append(
            JobInput(
                declaration_id=decl.id, request_id=decl.request_id, vessel_imo=decl.vessel_imo,
                fairway_code=vessel.fairway_code, required_qualification=vessel.required_qualification,
                ready_at=t.starts_at, due_at=t.ends_at,
                duration_minutes=int((t.ends_at - t.starts_at).total_seconds() // 60),
                board_location=decl.board_location, land_location=decl.land_location,
                committed=True,
                tide_windows=[(t.starts_at, t.ends_at)],
                fixed=True, fixed_pilot=t.pilot_code,
                fixed_start_boat=t.start_boat_code, fixed_end_boat=t.end_boat_code,
                fixed_starts_at=t.starts_at,
            )
        )
        decl_by_id[decl.id] = decl

    stmt = select(models.Declaration).where(models.Declaration.cancelled == False)  # noqa: E712
    if include_declaration_ids is not None:
        stmt = stmt.where(models.Declaration.id.in_(include_declaration_ids))
    declarations = list(db.scalars(stmt).all())

    for decl in declarations:
        if decl.id in fixed_decl_ids:
            continue
        vessel = db.get(models.Vessel, decl.vessel_imo)
        windows = tide_windows_for(db, vessel, hs, he)
        # 通航窗口 ∩ 申报期望窗口（半开区间 [ready_at, due_at)）
        from .tide import intersect_windows
        windows = intersect_windows(windows, [(decl.ready_at, decl.due_at)])
        jobs.append(
            JobInput(
                declaration_id=decl.id, request_id=decl.request_id, vessel_imo=decl.vessel_imo,
                fairway_code=vessel.fairway_code, required_qualification=vessel.required_qualification,
                ready_at=decl.ready_at, due_at=decl.due_at,
                duration_minutes=decl.duration_minutes,
                board_location=decl.board_location, land_location=decl.land_location,
                committed=decl.committed, tide_windows=windows,
            )
        )
        decl_by_id[decl.id] = decl

    inp = SchedulerInput(
        horizon_start=hs, horizon_end=he, jobs=jobs, pilots=pilots, boats=boats,
        travel_minutes=travel, disembark_minutes=settings.DISEMBARK_MINUTES,
        transfer_minutes=tr, time_limit_seconds=settings.SOLVER_TIME_LIMIT_SECONDS,
    )
    result = solve(inp)
    return result, decl_by_id, inp


def persist_plan(db: Session, result, decl_by_id, horizon_start, horizon_end,
                 note: Optional[str] = None, include_fixed: bool = False) -> models.Plan:
    """落草案。include_fixed=True（修订场景）时把保留的锁定任务一并复制进新计划。"""
    plan = models.Plan(
        status="PROPOSED", horizon_start=horizon_start, horizon_end=horizon_end, note=note,
    )
    db.add(plan)
    db.flush()
    for item in result.scheduled:
        if item.fixed and not include_fixed:
            continue  # 固定任务属于既有正式计划，普通试排不复制
        db.add(
            models.PlanTask(
                plan_id=plan.id,
                declaration_id=item.declaration_id,
                pilot_code=item.pilot_code,
                start_boat_code=item.start_boat_code,
                end_boat_code=item.end_boat_code,
                starts_at=item.starts_at,
                ends_at=item.ends_at,
            )
        )
    db.commit()
    db.refresh(plan)
    return plan


def supersede_previous_confirmed(db: Session) -> list[models.Plan]:
    """确认新计划前，把现有 CONFIRMED 计划标记为 SUPERSEDED（审计留痕）。"""
    old = list(db.scalars(select(models.Plan).where(models.Plan.status == "CONFIRMED")).all())
    for p in old:
        p.status = "SUPERSEDED"
    return old


def confirm_plan(db: Session, plan: models.Plan) -> models.Plan:
    """正式锁定计划：标记 CONFIRMED，并把排入任务对应的申报置为 promised。"""
    plan.status = "CONFIRMED"
    plan.confirmed_at = datetime.utcnow()
    for t in plan.tasks:
        decl = db.get(models.Declaration, t.declaration_id)
        decl.promised = True
    db.commit()
    db.refresh(plan)
    return plan
