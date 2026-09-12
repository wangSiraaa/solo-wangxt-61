"""端到端排班测试（确定性）。"""
from __future__ import annotations

from datetime import datetime, timedelta

from .conftest import D0, post, solve

# 第一天高潮 00:12，深吃水船可行窗约 23:57(前一天)~00:27 —— 跨午夜
PREV_EVE = D0 - timedelta(days=1, hours=1)
MIDNIGHT = D0  # 2026-09-15 00:00


def _declare(client, rid, imo, ready, due, committed=False, duration=40,
             board="BAR", land="DOCKS"):
    return post(client, "/declarations", json={
        "request_id": rid, "vessel_imo": imo, "movement": "INBOUND",
        "ready_at": ready.isoformat(), "due_at": due.isoformat(),
        "duration_minutes": duration, "board_location": board, "land_location": land,
        "committed": committed,
    })


def test_cross_midnight_tide_window_is_scheduled(client):
    # 申报窗口覆盖 00:12 高潮（跨午夜）
    d = _declare(client, "REQ-XM-1", "V-DEEP",
                 D0 - timedelta(minutes=40), D0 + timedelta(minutes=40))
    res = solve(client, D0 - timedelta(hours=2), D0 + timedelta(hours=2), [d["id"]])
    assert len(res["scheduled"]) == 1, res
    t = res["scheduled"][0]
    start = datetime.fromisoformat(t["starts_at"])
    # 开始时刻落在跨午夜的潮窗内（高潮 00:12 附近）
    assert MIDNIGHT - timedelta(minutes=30) <= start <= MIDNIGHT + timedelta(minutes=30)
    assert t["pilot_code"] in ("P1", "P2", "P3", "P4", "P5")  # 夜班 GENERAL 引航员


def test_enough_pilots_but_boats_insufficient(client):
    """合格引航员充足（夜班 5 名），但 01:02 高潮极窄窗内接送艇座位不足。"""
    ids = []
    # 第二天高潮 01:02；5 艘船期望窗仅 00:57~01:02，作业 20 分钟。
    # 引航员充足（5 名夜班），但登轮接送全部挤在同一 5 分钟内，
    # 艇总座位仅 2，无法同时接走 5 名引航员。
    base = D0 + timedelta(days=1)
    for i in range(5):
        d = _declare(client, f"REQ-BOAT-{i}", "V-SMALL",
                     base.replace(hour=0, minute=57),
                     base.replace(hour=1, minute=2),
                     duration=20)
        ids.append(d["id"])
    res = solve(client, base - timedelta(hours=4), base + timedelta(hours=3), ids)

    scheduled = res["scheduled"]
    unscheduled = res["unscheduled"]
    assert len(scheduled) + len(unscheduled) == 5
    boat_conflicts = [c for u in unscheduled for c in u["conflicts"] if c["kind"] == "BOAT"]
    assert boat_conflicts, "应当报告接送艇容量冲突"
    for c in boat_conflicts:
        assert c["blocked_from"] < c["blocked_to"]

    # 5 艘船都只需 GENERAL 资质，夜班有 4 名 GENERAL 引航员（可错峰复用）：
    # 资质/班期不是瓶颈，艇容量必须被识别
    assert any(u["feasible_windows"] for u in unscheduled)


def test_pilot_turnaround_includes_disembark_and_transfer(client):
    """同一引航员两任务之间必须满足 离船10min + BAR→DOCKS 20min 转场。"""
    # 00:12 高潮窗内先排一个深吃水任务
    d1 = _declare(client, "REQ-GAP-1", "V-DEEP",
                  D0 - timedelta(minutes=20), D0 + timedelta(minutes=20),
                  duration=30)
    res1 = solve(client, D0 - timedelta(hours=1), D0 + timedelta(hours=1), [d1["id"]])
    assert len(res1["scheduled"]) == 1
    t1 = res1["scheduled"][0]
    plan_id = res1["plan_id"]
    post(client, f"/plans/{plan_id}/confirm", json={})

    # 第二个申报的期望开始距第一个结束只有 20 分钟（< 30 分钟必需间隔）
    end1 = datetime.fromisoformat(t1["ends_at"])
    d2 = _declare(client, "REQ-GAP-2", "V-SMALL",
                  end1, end1 + timedelta(minutes=20), duration=20)
    res2 = solve(client, D0 - timedelta(hours=1), D0 + timedelta(hours=2), [d2["id"]])
    # 要么未排入，要么开始时间距 t1 结束 >= 30 分钟（被另一名空闲引航员接走也可）
    if res2["scheduled"]:
        t2 = res2["scheduled"][0]
        start2 = datetime.fromisoformat(t2["starts_at"])
        if t2["pilot_code"] == t1["pilot_code"]:
            assert start2 >= end1 + timedelta(minutes=30)
    else:
        kinds = {c["kind"] for u in res2["unscheduled"] for c in u["conflicts"]}
        assert "PILOT" in kinds


def test_confirm_is_idempotent_no_second_active_task(client):
    """同一申报重复确认不产生第二个有效任务。"""
    d = _declare(client, "REQ-IDEM-1", "V-SMALL",
                 D0 + timedelta(days=1, hours=9),
                 D0 + timedelta(days=1, hours=12), duration=30)
    res = solve(client, D0 + timedelta(days=1, hours=8),
                D0 + timedelta(days=1, hours=14), [d["id"]])
    plan_id = res["plan_id"]

    r1 = post(client, f"/plans/{plan_id}/confirm", json={})
    r2 = post(client, f"/plans/{plan_id}/confirm", json={})
    assert r1["status"] == r2["status"] == "CONFIRMED"
    assert r1["confirmed_at"] == r2["confirmed_at"]
    assert len(r1["tasks"]) == len(r2["tasks"]) == 1

    plans = client.get("/plans").json()
    confirmed = [p for p in plans if p["status"] == "CONFIRMED"]
    tasks_for_decl = [
        t for p in confirmed for t in p["tasks"] if t["declaration_id"] == d["id"]
    ]
    assert len(tasks_for_decl) == 1, "同一申报不得出现第二个有效任务"

    # 申报重复提交（同 request_id）也不新增
    again = post(client, "/declarations", json={
        "request_id": "REQ-IDEM-1", "vessel_imo": "V-SMALL",
        "ready_at": (D0 + timedelta(days=1, hours=9)).isoformat(),
        "due_at": (D0 + timedelta(days=1, hours=12)).isoformat(),
        "duration_minutes": 30,
    })
    assert again["id"] == d["id"]


def test_locked_plan_requires_explicit_revision(client):
    d = _declare(client, "REQ-LOCK-1", "V-SMALL",
                 D0 + timedelta(days=2, hours=9),
                 D0 + timedelta(days=2, hours=12), duration=30)
    res = solve(client, D0 + timedelta(days=2, hours=8),
                D0 + timedelta(days=2, hours=14), [d["id"]])
    pid = res["plan_id"]
    post(client, f"/plans/{pid}/confirm", json={})

    # 已锁定计划不能再次修订为普通草案之外的操作：必须走 /revise
    r = client.post(f"/plans/{pid}/confirm", json={})  # 幂等
    assert r.json()["status"] == "CONFIRMED"

    # 显式修订（幂等键重复 -> 同一结果计划）
    d2 = _declare(client, "REQ-LOCK-2", "V-SMALL",
                  D0 + timedelta(days=2, hours=13),
                  D0 + timedelta(days=2, hours=16), duration=30)
    payload = {"idempotency_key": "REV-1", "add_declaration_ids": [d2["id"]], "note": "加船"}
    rv1 = post(client, f"/plans/{pid}/revise", json=payload)
    rv2 = post(client, f"/plans/{pid}/revise", json=payload)
    assert rv1["plan_id"] == rv2["plan_id"], "修订幂等键应返回同一草案"
