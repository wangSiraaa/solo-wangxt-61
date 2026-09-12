"""基础资源路由：航道/船舶/潮位/引航员/接送艇/转场时间。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(tags=["resources"])


@router.post("/fairways", response_model=schemas.FairwayOut)
def create_fairway(payload: schemas.FairwayIn, db: Session = Depends(get_db)):
    if db.get(models.Fairway, payload.code):
        raise HTTPException(409, f"航道 {payload.code} 已存在")
    obj = models.Fairway(**payload.model_dump())
    db.add(obj)
    db.commit()
    return obj


@router.post("/vessels", response_model=schemas.VesselOut)
def create_vessel(payload: schemas.VesselIn, db: Session = Depends(get_db)):
    if db.get(models.Vessel, payload.imo):
        raise HTTPException(409, f"船舶 {payload.imo} 已存在")
    if not db.get(models.Fairway, payload.fairway_code):
        raise HTTPException(422, f"航道 {payload.fairway_code} 不存在")
    obj = models.Vessel(**payload.model_dump())
    db.add(obj)
    db.commit()
    return obj


@router.post("/tide-samples", response_model=schemas.TideSampleOut)
def create_tide_sample(payload: schemas.TideSampleIn, db: Session = Depends(get_db)):
    if not db.get(models.Fairway, payload.fairway_code):
        raise HTTPException(422, f"航道 {payload.fairway_code} 不存在")
    exists = db.scalar(
        select(models.TideSample).where(
            models.TideSample.fairway_code == payload.fairway_code,
            models.TideSample.ts == payload.ts,
        )
    )
    if exists:
        raise HTTPException(409, "该时刻潮位样本已存在")
    obj = models.TideSample(**payload.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/tide-samples/{fairway_code}", response_model=list[schemas.TideSampleOut])
def list_tide_samples(fairway_code: str, db: Session = Depends(get_db)):
    return list(db.scalars(
        select(models.TideSample)
        .where(models.TideSample.fairway_code == fairway_code)
        .order_by(models.TideSample.ts)
    ).all())


@router.post("/pilots", response_model=schemas.PilotOut)
def create_pilot(payload: schemas.PilotIn, db: Session = Depends(get_db)):
    if db.get(models.Pilot, payload.code):
        raise HTTPException(409, f"引航员 {payload.code} 已存在")
    # 班期统一以 ISO8601 字符串存入 JSON 列
    windows = [{"start": _iso(w["start"]), "end": _iso(w["end"])} for w in payload.work_windows]
    obj = models.Pilot(
        code=payload.code, name=payload.name,
        qualifications=payload.qualifications, work_windows=windows,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/boats", response_model=schemas.BoatOut)
def create_boat(payload: schemas.BoatIn, db: Session = Depends(get_db)):
    if db.get(models.Boat, payload.code):
        raise HTTPException(409, f"接送艇 {payload.code} 已存在")
    obj = models.Boat(**payload.model_dump())
    db.add(obj)
    db.commit()
    return obj


@router.post("/travel-times", status_code=201)
def upsert_travel_time(payload: schemas.TravelTimeIn, db: Session = Depends(get_db)):
    obj = db.scalar(
        select(models.TravelTime).where(
            models.TravelTime.from_code == payload.from_code,
            models.TravelTime.to_code == payload.to_code,
        )
    )
    if obj:
        obj.minutes = payload.minutes
    else:
        obj = models.TravelTime(**payload.model_dump())
        db.add(obj)
    db.commit()
    return {"from_code": payload.from_code, "to_code": payload.to_code, "minutes": payload.minutes}


def _dt(v):
    from datetime import datetime

    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(v)


def _iso(v) -> str:
    from datetime import datetime

    return v.isoformat() if isinstance(v, datetime) else str(v)
