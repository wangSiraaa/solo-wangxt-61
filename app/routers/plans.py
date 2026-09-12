"""计划求解、确认（锁定）与显式修订。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..service import build_and_solve, confirm_plan, persist_plan, supersede_previous_confirmed

router = APIRouter(tags=["plans"])


class SolveRequest(BaseModel):
    horizon_start: datetime
    horizon_end: datetime
    declaration_ids: Optional[list[int]] = None
    note: Optional[str] = None


def _serialize(result, decl_by_id, plan_id: Optional[int],
               horizon_start: datetime, horizon_end: datetime,
               include_fixed: bool = False) -> schemas.PlanSolveOut:
    scheduled = []
    for it in result.scheduled:
        if it.fixed and not include_fixed:
            continue
        d = decl_by_id[it.declaration_id]
        scheduled.append(
            schemas.ScheduledTask(
                declaration_id=it.declaration_id,
                request_id=d.request_id,
                vessel_imo=d.vessel_imo,
                pilot_code=it.pilot_code,
                start_boat_code=it.start_boat_code,
                end_boat_code=it.end_boat_code,
                starts_at=it.starts_at,
                ends_at=it.ends_at,
                delay_minutes=it.delay_minutes,
                committed=d.committed,
            )
        )
    unscheduled = []
    for u in result.unscheduled:
        d = decl_by_id[u.declaration_id]
        unscheduled.append(
            schemas.UnscheduledDeclaration(
                declaration_id=u.declaration_id,
                request_id=d.request_id,
                vessel_imo=d.vessel_imo,
                committed=d.committed,
                feasible_windows=[
                    {"start": s.isoformat(), "end": e.isoformat()}
                    for s, e in result.feasible_windows_by_job.get(u.declaration_id, [])
                ],
                conflicts=[schemas.ConflictResource(**c.__dict__) for c in u.conflicts],
            )
        )
    return schemas.PlanSolveOut(
        horizon_start=horizon_start, horizon_end=horizon_end,
        scheduled=scheduled, unscheduled=unscheduled,
        status="PROPOSED", plan_id=plan_id,
    )


@router.post("/plans/solve", response_model=schemas.PlanSolveOut)
def solve_plan(payload: SolveRequest, db: Session = Depends(get_db)):
    """试排：只求解并落一份 PROPOSED 草案，不锁定任何资源。"""
    result, decl_by_id, _ = build_and_solve(
        db, payload.horizon_start, payload.horizon_end, payload.declaration_ids
    )
    plan = persist_plan(db, result, decl_by_id,
                        payload.horizon_start, payload.horizon_end, payload.note)
    return _serialize(result, decl_by_id, plan.id, payload.horizon_start, payload.horizon_end)


@router.get("/plans", response_model=list[schemas.PlanOut])
def list_plans(db: Session = Depends(get_db)):
    return list(db.scalars(select(models.Plan).order_by(models.Plan.id)).all())


@router.get("/plans/{plan_id}", response_model=schemas.PlanOut)
def get_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(models.Plan, plan_id)
    if not plan:
        raise HTTPException(404, "计划不存在")
    return plan


@router.post("/plans/{plan_id}/confirm", response_model=schemas.PlanOut)
def confirm(plan_id: int, db: Session = Depends(get_db)):
    """正式锁定。重复确认同一计划是幂等的：不会产生第二个有效计划/任务。

    若该草案是对已锁定计划的修订（含修订来源标记），确认时旧计划
    标记为 SUPERSEDED 并保留审计留痕。
    """
    plan = db.get(models.Plan, plan_id)
    if not plan:
        raise HTTPException(404, "计划不存在")
    if plan.status == "CONFIRMED":
        return plan  # 已锁定：直接返回，绝不重复锁定
    is_revision = bool(plan.note and plan.note.startswith("revision of plan"))
    if is_revision:
        supersede_previous_confirmed(db)
    return confirm_plan(db, plan)


@router.post("/plans/{plan_id}/revise", response_model=schemas.PlanSolveOut)
def revise(plan_id: int, payload: schemas.RevisionIn, db: Session = Depends(get_db)):
    """显式修订：正式计划锁定后只允许通过本接口修改。

    原 CONFIRMED 计划保持不变（审计留痕），生成一份新的 PROPOSED 计划：
    既有任务作为固定占用保留，可加入新申报、取消既有申报。
    idempotency_key 重复时直接返回上一次修订结果。
    """
    plan = db.get(models.Plan, plan_id)
    if not plan:
        raise HTTPException(404, "计划不存在")
    if plan.status != "CONFIRMED":
        raise HTTPException(409, "只有已锁定(CONFIRMED)的计划需要修订；草案可直接重新求解")

    if payload.idempotency_key:
        dup = db.scalar(
            select(models.Revision).where(
                models.Revision.plan_id == plan_id,
                models.Revision.idempotency_key == payload.idempotency_key,
            )
        )
        if dup and dup.resulting_plan_id:
            rp = db.get(models.Plan, dup.resulting_plan_id)
            return _plan_as_solve_out(db, rp)

    # 取消既有申报（后续求解固定任务会跳过已取消项）
    for did in payload.cancel_declaration_ids:
        decl = db.get(models.Declaration, did)
        if not decl:
            raise HTTPException(422, f"申报 {did} 不存在")
        decl.cancelled = True
        decl.promised = False
    new_decls = []
    for did in payload.add_declaration_ids:
        decl = db.get(models.Declaration, did)
        if not decl:
            raise HTTPException(422, f"申报 {did} 不存在")
        new_decls.append(decl)

    # 修订视野 = 原计划视野 ∪ 所有新增申报窗口
    hs, he = plan.horizon_start, plan.horizon_end
    for decl in new_decls:
        hs = min(hs, decl.ready_at)
        he = max(he, decl.due_at)

    revision = models.Revision(
        plan_id=plan.id,
        idempotency_key=payload.idempotency_key,
        added_declaration_ids=payload.add_declaration_ids,
        cancelled_declaration_ids=payload.cancel_declaration_ids,
        note=payload.note,
    )
    db.add(revision)
    db.flush()

    result, decl_by_id, _ = build_and_solve(
        db, hs, he,
        include_declaration_ids=payload.add_declaration_ids or None,
    )
    new_plan = persist_plan(
        db, result, decl_by_id, hs, he,
        note=f"revision of plan {plan.id}: {payload.note or ''}",
        include_fixed=True,
    )
    revision.resulting_plan_id = new_plan.id
    db.commit()
    return _serialize(result, decl_by_id, new_plan.id, hs, he, include_fixed=True)


def _plan_as_solve_out(db: Session, plan: models.Plan) -> schemas.PlanSolveOut:
    scheduled = []
    for t in plan.tasks:
        decl = db.get(models.Declaration, t.declaration_id)
        scheduled.append(
            schemas.ScheduledTask(
                declaration_id=t.declaration_id,
                request_id=decl.request_id,
                vessel_imo=decl.vessel_imo,
                pilot_code=t.pilot_code,
                start_boat_code=t.start_boat_code,
                end_boat_code=t.end_boat_code,
                starts_at=t.starts_at,
                ends_at=t.ends_at,
                delay_minutes=max(0, int((t.starts_at - decl.ready_at).total_seconds() // 60)),
                committed=decl.committed,
            )
        )
    return schemas.PlanSolveOut(
        horizon_start=plan.horizon_start, horizon_end=plan.horizon_end,
        scheduled=scheduled, unscheduled=[], status="PROPOSED", plan_id=plan.id,
    )
