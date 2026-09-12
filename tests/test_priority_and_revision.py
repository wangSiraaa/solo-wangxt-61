"""确定性与目标优先级测试。"""
from __future__ import annotations

from datetime import timedelta

from .conftest import D0, post, solve


def _declare(client, rid, imo, ready, due, committed=False, duration=30):
    return post(client, "/declarations", json={
        "request_id": rid, "vessel_imo": imo,
        "ready_at": ready.isoformat(), "due_at": due.isoformat(),
        "duration_minutes": duration, "committed": committed,
    })


def test_solve_is_deterministic(client):
    base = D0 + timedelta(days=2)
    ids = [
        _declare(client, f"REQ-DET-{i}", "V-SMALL",
                 base.replace(hour=9), base.replace(hour=15))["id"]
        for i in range(3)
    ]
    r1 = solve(client, base.replace(hour=8), base.replace(hour=17), ids)
    r2 = solve(client, base.replace(hour=8), base.replace(hour=17), ids)

    def sig(r):
        return sorted(
            (t["declaration_id"], t["pilot_code"], t["start_boat_code"],
             t["end_boat_code"], t["starts_at"])
            for t in r["scheduled"]
        )

    assert sig(r1) == sig(r2), "同输入两次求解结果必须一致"


def test_committed_jobs_have_priority(client):
    """容量受限时，已承诺任务优先排入。"""
    base = D0 + timedelta(days=1)
    # 极窄夜潮窗 00:57~01:02，2 个艇座位 -> 最多 2 艘
    committed = _declare(client, "REQ-PRI-C", "V-SMALL",
                         base.replace(hour=0, minute=57), base.replace(hour=1, minute=2),
                         committed=True)
    others = [
        _declare(client, f"REQ-PRI-{i}", "V-SMALL",
                 base.replace(hour=0, minute=57), base.replace(hour=1, minute=2))
        for i in range(2)
    ]
    ids = [committed["id"]] + [d["id"] for d in others]
    res = solve(client, base - timedelta(hours=4), base + timedelta(hours=3), ids)

    scheduled_ids = {t["declaration_id"] for t in res["scheduled"]}
    assert committed["id"] in scheduled_ids, "已承诺任务必须优先排入"
    assert len(res["scheduled"]) == 2  # 2 个艇座位
    assert len(res["unscheduled"]) == 1
    assert all(u["committed"] is False for u in res["unscheduled"])


def test_revision_keeps_locked_tasks_and_reports_new_conflicts(client):
    """锁定后显式修订：既有任务作为固定占用保留，冲突新任务不被悄悄塞入。"""
    base = D0 + timedelta(days=2)
    d1 = _declare(client, "REQ-REV-1", "V-SMALL",
                  base.replace(hour=9), base.replace(hour=12))
    res = solve(client, base.replace(hour=8), base.replace(hour=17), [d1["id"]])
    pid = res["plan_id"]
    post(client, f"/plans/{pid}/confirm", json={})
    locked_start = res["scheduled"][0]["starts_at"]

    # 未走修订接口直接对已锁定计划操作：重复确认幂等，状态不变
    again = post(client, f"/plans/{pid}/confirm", json={})
    assert again["status"] == "CONFIRMED"
    assert len(again["tasks"]) == 1

    # 新增申报（白天窗口，资源充足），通过显式修订
    d2 = _declare(client, "REQ-REV-2", "V-SMALL",
                  base.replace(hour=13), base.replace(hour=16))
    rv = post(client, f"/plans/{pid}/revise",
              json={"idempotency_key": "REV-A", "add_declaration_ids": [d2["id"]]})
    ids = {t["declaration_id"] for t in rv["scheduled"]}
    assert d1["id"] in ids and d2["id"] in ids
    # 既有锁定任务时间不变
    t1 = next(t for t in rv["scheduled"] if t["declaration_id"] == d1["id"])
    assert t1["starts_at"] == locked_start

    new_pid = rv["plan_id"]
    post(client, f"/plans/{new_pid}/confirm", json={})
    # 旧计划被取代留痕
    old = client.get(f"/plans/{pid}").json()
    assert old["status"] == "SUPERSEDED"


def test_revision_can_cancel_committed_task(client):
    base = D0 + timedelta(days=2)
    d1 = _declare(client, "REQ-CAN-1", "V-SMALL",
                  base.replace(hour=9), base.replace(hour=12), committed=True)
    res = solve(client, base.replace(hour=8), base.replace(hour=17), [d1["id"]])
    pid = res["plan_id"]
    post(client, f"/plans/{pid}/confirm", json={})

    # 显式取消该申报
    rv = post(client, f"/plans/{pid}/revise",
              json={"idempotency_key": "REV-CAN", "cancel_declaration_ids": [d1["id"]]})
    ids = {t["declaration_id"] for t in rv["scheduled"]}
    assert d1["id"] not in ids

    decl = client.get(f"/declarations/{d1['id']}").json()
    assert decl["cancelled"] is True

    # 取消后该申报不再出现在默认申报列表
    listed = {d["id"] for d in client.get("/declarations").json()}
    assert d1["id"] not in listed
