from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- 基础资源 ----------


class FairwayIn(BaseModel):
    code: str
    name: str
    chart_datum_depth_m: float
    safety_ukc_m: float = 0.5
    min_tide_m: float = 0.0
    max_tide_m: Optional[float] = None


class FairwayOut(FairwayIn):
    pass


class VesselIn(BaseModel):
    imo: str
    name: str
    draft_m: float
    loa_m: float = 0.0
    required_qualification: str = "GENERAL"
    fairway_code: str


class VesselOut(VesselIn):
    pass


class TideSampleIn(BaseModel):
    fairway_code: str
    ts: datetime
    height_m: float


class TideSampleOut(ORMModel):
    id: int
    fairway_code: str
    ts: datetime
    height_m: float


class PilotIn(BaseModel):
    code: str
    name: str
    qualifications: list[str] = Field(default_factory=list)
    work_windows: list[dict] = Field(default_factory=list)  # [{"start": dt, "end": dt}]


class PilotOut(ORMModel):
    code: str
    name: str
    qualifications: list[str]
    work_windows: list[dict]


class BoatIn(BaseModel):
    code: str
    name: str
    seats: int = 4


class BoatOut(BoatIn):
    pass


class TravelTimeIn(BaseModel):
    from_code: str
    to_code: str
    minutes: int


# ---------- 申报 ----------


class DeclarationIn(BaseModel):
    request_id: str = Field(..., description="幂等键，重复提交不会产生第二条申报")
    vessel_imo: str
    movement: str = "INBOUND"
    ready_at: datetime
    due_at: datetime
    duration_minutes: int = 60
    board_location: str = "BAR"
    land_location: str = "DOCKS"
    committed: bool = False


class DeclarationOut(ORMModel):
    id: int
    request_id: str
    vessel_imo: str
    movement: str
    ready_at: datetime
    due_at: datetime
    duration_minutes: int
    board_location: str
    land_location: str
    committed: bool
    promised: bool
    cancelled: bool


# ---------- 求解结果 ----------


class ScheduledTask(BaseModel):
    declaration_id: int
    request_id: str
    vessel_imo: str
    pilot_code: str
    start_boat_code: str
    end_boat_code: str
    starts_at: datetime
    ends_at: datetime
    delay_minutes: int
    committed: bool


class ConflictResource(BaseModel):
    kind: str  # PILOT / BOAT / TIDE / SHIFT
    code: str  # 资源编码；TIDE 时为航道编码
    blocked_from: datetime
    blocked_to: datetime
    detail: str


class UnscheduledDeclaration(BaseModel):
    declaration_id: int
    request_id: str
    vessel_imo: str
    committed: bool
    feasible_windows: list[dict]  # 该船允许的通航（潮位）窗口
    conflicts: list[ConflictResource]


class PlanSolveOut(BaseModel):
    horizon_start: datetime
    horizon_end: datetime
    scheduled: list[ScheduledTask]
    unscheduled: list[UnscheduledDeclaration]
    status: str
    plan_id: Optional[int] = None


class PlanOut(ORMModel):
    id: int
    status: str
    horizon_start: datetime
    horizon_end: datetime
    created_at: datetime
    confirmed_at: Optional[datetime]
    note: Optional[str]
    tasks: list["PlanTaskOut"]


class PlanTaskOut(ORMModel):
    id: int
    declaration_id: int
    pilot_code: str
    start_boat_code: str
    end_boat_code: str
    starts_at: datetime
    ends_at: datetime


class RevisionIn(BaseModel):
    idempotency_key: Optional[str] = None
    add_declaration_ids: list[int] = Field(default_factory=list)
    cancel_declaration_ids: list[int] = Field(default_factory=list)
    note: Optional[str] = None


class MessageOut(BaseModel):
    detail: str
