"""船舶申报路由（按 request_id 幂等）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(tags=["declarations"])


@router.post("/declarations", response_model=schemas.DeclarationOut, status_code=201)
def submit_declaration(payload: schemas.DeclarationIn, db: Session = Depends(get_db)):
    # 幂等：同一 request_id 重复提交返回既有申报，不产生第二条
    existing = db.scalar(
        select(models.Declaration).where(models.Declaration.request_id == payload.request_id)
    )
    if existing:
        return existing

    if not db.get(models.Vessel, payload.vessel_imo):
        raise HTTPException(422, f"船舶 {payload.vessel_imo} 不存在")
    if payload.ready_at >= payload.due_at:
        raise HTTPException(422, "ready_at 必须早于 due_at")

    obj = models.Declaration(**payload.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/declarations", response_model=list[schemas.DeclarationOut])
def list_declarations(
    include_cancelled: bool = Query(False),
    db: Session = Depends(get_db),
):
    stmt = select(models.Declaration).order_by(models.Declaration.id)
    if not include_cancelled:
        stmt = stmt.where(models.Declaration.cancelled == False)  # noqa: E712
    return list(db.scalars(stmt).all())


@router.get("/declarations/{decl_id}", response_model=schemas.DeclarationOut)
def get_declaration(decl_id: int, db: Session = Depends(get_db)):
    obj = db.get(models.Declaration, decl_id)
    if not obj:
        raise HTTPException(404, "申报不存在")
    return obj
