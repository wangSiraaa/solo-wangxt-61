"""FastAPI 入口。

所有潮汐/船舶数据均为离线虚构示例，仅用于排班演示，不提供真实航行决策。
"""
from __future__ import annotations

from fastapi import FastAPI

from .database import Base, engine
from .routers import declarations, plans, resources

app = FastAPI(
    title="引航站排班 API（虚构示例）",
    description="船舶申报 × 潮位通航窗口 × 引航员资质/班期 × 接送艇容量的统一排班。"
                "数据均为离线虚构，不用于真实航行决策。",
    version="0.1.0",
)

app.include_router(resources.router)
app.include_router(declarations.router)
app.include_router(plans.router)


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
