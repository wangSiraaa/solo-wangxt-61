from __future__ import annotations

import os
import tempfile

import pytest

# 必须在导入 app 之前配置独立数据库
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ.setdefault("SOLVER_TIME_LIMIT_SECONDS", "5")

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app import seed as seed_module  # noqa: E402
from app.main import app  # noqa: E402

D0 = seed_module.D0


@pytest.fixture()
def client():
    """每个测试使用全新的内存/临时数据库，避免锁定计划互相干扰。"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed_module.seed(db)
    db.close()
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


def post(client, path, **kw):
    r = client.post(path, **kw)
    assert r.status_code in (200, 201), r.text
    return r.json()


def solve(client, start, end, ids=None):
    return post(client, "/plans/solve",
                json={"horizon_start": start.isoformat(),
                      "horizon_end": end.isoformat(),
                      "declaration_ids": ids})
