"""OR-Tools CP-SAT 排班求解器。

目标按优先级（字典序，用分层权重实现）：
  1. 已承诺(committed)任务尽量全部排入；
  2. 其余任务尽量排入；
  3. 减少延误（开始时间相对 ready_at 的等待分钟数，承诺任务权重更高）；
  4. 同分时倾向更早开始（确定性 tie-break）。

约束：
  * 开始时刻必须落在「潮位通航窗口 ∩ 期望窗口」内（窗口可跨午夜）；
  * 引航员必须具备资质、且整个作业处于其某个可工作时段内；
  * 同一引航员两个任务之间必须满足
        上一任务结束 + 离船时间 + 从上一离船点到下一登轮点的转场时间
    （不是简单判断作业时间段是否相交）；
  * 每个任务的登轮、离船两次接送都要分配接送艇，同一条艇同时在途
    接送数不超过其座位数（同一请求的两次接送独立分配）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ortools.sat.python import cp_model

from .tide import ONE_MINUTE

# 目标权重（保证分层优先：任何延误收益都抵不上一个未排入任务）
W_COMMITTED_MISSING = 50_000_000
W_MISSING = 5_000_000
W_DELAY_COMMITTED = 30
W_DELAY = 10
W_START_TIEBREAK = 1


@dataclass
class JobInput:
    declaration_id: int
    request_id: str
    vessel_imo: str
    fairway_code: str
    required_qualification: str
    ready_at: datetime
    due_at: datetime
    duration_minutes: int
    board_location: str
    land_location: str
    committed: bool
    tide_windows: list[tuple[datetime, datetime]]  # 已与期望窗口求交，可跨午夜
    fixed: bool = False  # 已锁定的正式任务（forced presence）
    fixed_pilot: str | None = None
    fixed_start_boat: str | None = None
    fixed_end_boat: str | None = None
    fixed_starts_at: datetime | None = None


@dataclass
class PilotInput:
    code: str
    qualifications: list[str]
    work_windows: list[tuple[datetime, datetime]]  # 支持跨午夜班期


@dataclass
class BoatInput:
    code: str
    seats: int


@dataclass
class SchedulerInput:
    horizon_start: datetime
    horizon_end: datetime
    jobs: list[JobInput]
    pilots: list[PilotInput]
    boats: list[BoatInput]
    travel_minutes: dict[tuple[str, str], int]  # (from,to) 转场分钟
    disembark_minutes: int = 10
    transfer_minutes: int = 5  # 单程接送航程（分钟）
    time_limit_seconds: float = 10.0


@dataclass
class ScheduledItem:
    declaration_id: int
    pilot_code: str
    start_boat_code: str
    end_boat_code: str
    starts_at: datetime
    ends_at: datetime
    delay_minutes: int
    fixed: bool


@dataclass
class Conflict:
    kind: str
    code: str
    blocked_from: datetime
    blocked_to: datetime
    detail: str


@dataclass
class UnscheduledItem:
    declaration_id: int
    conflicts: list[Conflict] = field(default_factory=list)


@dataclass
class SchedulerResult:
    scheduled: list[ScheduledItem]
    unscheduled: list[UnscheduledItem]
    feasible_windows_by_job: dict[int, list[tuple[datetime, datetime]]]


def _to_windows_iso(windows):
    return [{"start": s.isoformat(), "end": e.isoformat()} for s, e in windows]


def solve(inp: SchedulerInput) -> SchedulerResult:
    horizon_start, horizon_end = inp.horizon_start, inp.horizon_end
    H = int((horizon_end - horizon_start).total_seconds() // 60)
    tr = inp.transfer_minutes
    jobs = inp.jobs

    def off(dt: datetime) -> int:
        return int((dt - horizon_start).total_seconds() // 60)

    # ---- 预计算：每个作业允许的开始分钟窗口 ----
    # 约定：job_windows 中 (a,b) 为闭区间，a/b 均为可行开始分钟（半开潮窗右端减 1）。
    job_windows: list[list[tuple[int, int]]] = []
    for j in jobs:
        ws = []
        for s, e in j.tide_windows:
            a = max(off(s), 0)
            b = min(off(e) - 1, H - j.duration_minutes)
            if a <= b:
                ws.append((a, b))
        job_windows.append(merge_iv(ws))

    pilots = inp.pilots
    boats = inp.boats
    pilot_idx = {p.code: k for k, p in enumerate(pilots)}
    boat_idx = {b.code: q for q, b in enumerate(boats)}
    travel = inp.travel_minutes
    dis = inp.disembark_minutes

    m = cp_model.CpModel()

    # ---- 作业变量 ----
    x, starts, ends, delays, wsel = [], [], [], [], []
    for k, j in enumerate(jobs):
        xk = m.new_constant(1) if j.fixed else m.new_bool_var(f"x_{k}")
        sk = m.new_int_var(0, H, f"s_{k}")
        ek = m.new_int_var(0, H, f"e_{k}")
        dk = m.new_int_var(0, H, f"delay_{k}")
        m.add(ek == sk + j.duration_minutes)
        ready = off(j.ready_at)
        m.add(sk >= ready)
        m.add_max_equality(dk, [0, sk - ready])
        if j.fixed:
            m.add(sk == off(j.fixed_starts_at))
        else:
            # 开始时刻落在某个通航窗口内
            ws = job_windows[k]
            bvs = [m.new_bool_var(f"tide_{k}_{w}") for w in range(len(ws))]
            for bv, (a, b) in zip(bvs, ws):
                m.add(sk >= a).only_enforce_if(bv)
                m.add(sk <= b).only_enforce_if(bv)
            m.add(sum(bvs) == xk)
            wsel.append(bvs)
        x.append(xk)
        starts.append(sk)
        ends.append(ek)
        delays.append(dk)

    # ---- 引航员分配 ----
    y: dict[tuple[int, int], cp_model.IntVar] = {}
    for k, j in enumerate(jobs):
        allowed: list[int] = []
        for pi, p in enumerate(pilots):
            if j.fixed:
                ok = p.code == j.fixed_pilot
            else:
                ok = j.required_qualification in p.qualifications
            if ok:
                allowed.append(pi)
                v = m.new_bool_var(f"y_{k}_{pi}")
                y[(k, pi)] = v
                if j.fixed:
                    m.add(v == 1)
        m.add(sum(y[(k, pi)] for pi in allowed) == x[k])
        # 班期：作业必须完整落在该引航员某个可工作时段内
        for pi in allowed:
            shifts = [
                (max(off(s), 0), min(off(e), H)) for s, e in pilots[pi].work_windows
            ]
            kvars = []
            for wi, (u, v) in enumerate(shifts):
                kv = m.new_bool_var(f"shift_{k}_{pi}_{wi}")
                kvars.append(kv)
                m.add(starts[k] >= u).only_enforce_if(kv)
                m.add(ends[k] <= v).only_enforce_if(kv)
            if kvars:
                m.add(sum(kvars) >= y[(k, pi)])

    # ---- 引航员任务对：离船 + 转场时间 ----
    for pi, p in enumerate(pilots):
        idxs = [k for k in range(len(jobs)) if (k, pi) in y]
        for ii in range(len(idxs)):
            for jj in range(ii + 1, len(idxs)):
                i, k = idxs[ii], idxs[jj]
                gap_i_to_k = dis + travel.get((jobs[i].land_location, jobs[k].board_location), 0)
                gap_k_to_i = dis + travel.get((jobs[k].land_location, jobs[i].board_location), 0)
                order = m.new_bool_var(f"ord_{i}_{k}_{pi}")  # 1 => i 在 k 之前
                yi, yk = y[(i, pi)], y[(k, pi)]
                m.add(starts[k] >= ends[i] + gap_i_to_k).only_enforce_if([order, yi, yk])
                m.add(starts[i] >= ends[k] + gap_k_to_i).only_enforce_if(
                    order.negated(), yi, yk
                )

    # ---- 接送艇：登轮/离船各一次，用可选区间 + 累积容量约束 ----
    # 登轮接送：[开始接送, 登轮)；离船接送：[作业结束, 结束+航程)
    z1: dict[tuple[int, int], cp_model.IntVar] = {}
    z2: dict[tuple[int, int], cp_model.IntVar] = {}
    boat_intervals: dict[int, list[cp_model.IntervalVar]] = {q: [] for q in range(len(boats))}
    for k, j in enumerate(jobs):
        for q, b in enumerate(boats):
            if j.fixed:
                present1 = m.new_constant(1 if b.code == j.fixed_start_boat else 0)
                present2 = m.new_constant(1 if b.code == j.fixed_end_boat else 0)
            else:
                present1 = m.new_bool_var(f"z1_{k}_{q}")
                present2 = m.new_bool_var(f"z2_{k}_{q}")
            z1[(k, q)], z2[(k, q)] = present1, present2
            iv1 = m.new_optional_interval_var(
                starts[k] - tr, tr, starts[k], present1, f"iv1_{k}_{q}"
            )
            iv2 = m.new_optional_interval_var(
                ends[k], tr, ends[k] + tr, present2, f"iv2_{k}_{q}"
            )
            boat_intervals[q].extend([iv1, iv2])
        m.add(sum(z1[(k, q)] for q in range(len(boats))) == x[k])
        m.add(sum(z2[(k, q)] for q in range(len(boats))) == x[k])

    for q, b in enumerate(boats):
        # 同一时刻在途接送数不超过该艇座位数
        demands = [1] * len(boat_intervals[q])
        m.add_cumulative(boat_intervals[q], demands, b.seats)

    # ---- 目标 ----
    obj_terms = []
    for k, j in enumerate(jobs):
        miss_pen = W_COMMITTED_MISSING if j.committed else W_MISSING
        obj_terms.append((1 - x[k]) * miss_pen)
        obj_terms.append(delays[k] * (W_DELAY_COMMITTED if j.committed else W_DELAY))
        obj_terms.append(starts[k] * W_START_TIEBREAK)
    m.minimize(sum(obj_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = inp.time_limit_seconds
    solver.parameters.num_search_workers = 1  # 确定性
    solver.parameters.random_seed = 42
    solver.parameters.linearization_level = 1
    status = solver.solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # 极端情况（例如没有任何合格引航员）：全部任务不可排
        return _all_unscheduled(inp, job_windows, H, off)

    scheduled, unsched_ids = [], []
    for k, j in enumerate(jobs):
        if solver.value(x[k]) == 1:
            pi = next(pi for pi in range(len(pilots)) if (k, pi) in y and solver.value(y[(k, pi)]) == 1)
            q1 = next(q for q in range(len(boats)) if solver.value(z1[(k, q)]) == 1)
            q2 = next(q for q in range(len(boats)) if solver.value(z2[(k, q)]) == 1)
            sdt = horizon_start + timedelta(minutes=solver.value(starts[k]))
            scheduled.append(
                ScheduledItem(
                    declaration_id=j.declaration_id,
                    pilot_code=pilots[pi].code,
                    start_boat_code=boats[q1].code,
                    end_boat_code=boats[q2].code,
                    starts_at=sdt,
                    ends_at=horizon_start + timedelta(minutes=solver.value(ends[k])),
                    delay_minutes=solver.value(delays[k]),
                    fixed=j.fixed,
                )
            )
        else:
            unsched_ids.append(k)

    result_windows = {
        j.declaration_id: jobs[k].tide_windows for k, j in enumerate(jobs) if not j.fixed
    }
    result = SchedulerResult(scheduled=scheduled, unscheduled=[], feasible_windows_by_job=result_windows)
    result.scheduled.sort(key=lambda s: (s.starts_at, s.declaration_id))

    # ---- 未排入任务：诊断冲突资源与时间段 ----
    occupancy = _build_occupancy(inp, scheduled)
    for k in unsched_ids:
        result.unscheduled.append(_diagnose(inp, jobs[k], job_windows[k], occupancy, H, off))
    return result


def merge_iv(ivs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = []
    for a, b in sorted(ivs):
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _all_unscheduled(inp, job_windows, H, off):
    result_windows = {
        j.declaration_id: j.tide_windows for j in inp.jobs if not j.fixed
    }
    occupancy = _build_occupancy(inp, [])
    unsched = []
    for k, j in enumerate(inp.jobs):
        if not j.fixed:
            unsched.append(_diagnose(inp, j, job_windows[k], occupancy, H, off))
    return SchedulerResult(scheduled=[], unscheduled=unsched, feasible_windows_by_job=result_windows)


# ---------------- 冲突诊断 ----------------


def _build_occupancy(inp: SchedulerInput, scheduled: list[ScheduledItem]):
    """根据已排入/已锁定任务，构造引航员与接送艇的分钟级占用。"""
    H = int((inp.horizon_end - inp.horizon_start).total_seconds() // 60)
    pilot_busy: dict[str, list[tuple[int, int]]] = {p.code: [] for p in inp.pilots}
    boat_seats = {b.code: b.seats for b in inp.boats}
    # 每艘艇的占用区间（分钟偏移，半开）
    boat_usage: dict[str, list[tuple[int, int]]] = {b.code: [] for b in inp.boats}
    tr = inp.transfer_minutes

    def add_item(decl_id, pilot_code, s_off, e_off, start_boat, end_boat):
        pilot_busy[pilot_code].append((s_off, e_off, decl_id))
        boat_usage[start_boat].append((s_off - tr, s_off))
        boat_usage[end_boat].append((e_off, e_off + tr))

    for k, j in enumerate(inp.jobs):
        if j.fixed:
            s = int((j.fixed_starts_at - inp.horizon_start).total_seconds() // 60)
            add_item(j.declaration_id, j.fixed_pilot, s, s + j.duration_minutes,
                     j.fixed_start_boat, j.fixed_end_boat)
    for it in scheduled:
        s = int((it.starts_at - inp.horizon_start).total_seconds() // 60)
        e = int((it.ends_at - inp.horizon_start).total_seconds() // 60)
        j = next(jj for jj in inp.jobs if jj.declaration_id == it.declaration_id)
        add_item(it.declaration_id, it.pilot_code, s, e, it.start_boat_code, it.end_boat_code)

    # 艇的分钟级座位占用计数
    boat_count: dict[str, list[int]] = {}
    for code, ivs in boat_usage.items():
        arr = [0] * (H + 1)
        for a, b in ivs:
            for t in range(max(0, a), min(b, H + 1)):
                arr[t] += 1
        boat_count[code] = arr
    return {"pilot": pilot_busy, "boat_count": boat_count, "boat_seats": boat_seats}


def _diagnose(inp, job, windows_off, occ, H, off) -> UnscheduledItem:
    item = UnscheduledItem(declaration_id=job.declaration_id, conflicts=[])
    hs = inp.horizon_start
    d = job.duration_minutes
    dis, travel, tr = inp.disembark_minutes, inp.travel_minutes, inp.transfer_minutes

    def dt(x):
        return hs + timedelta(minutes=x)

    if not windows_off:
        item.conflicts.append(
            Conflict(
                kind="TIDE",
                code=job.fairway_code,
                blocked_from=job.ready_at,
                blocked_to=job.due_at,
                detail="期望窗口内没有满足该船吃水规则的潮位通航窗口",
            )
        )
        return item

    qualified = [p for p in inp.pilots if job.required_qualification in p.qualifications]
    if not qualified:
        item.conflicts.append(
            Conflict(
                kind="PILOT",
                code="ANY",
                blocked_from=job.ready_at,
                blocked_to=job.due_at,
                detail=f"没有具备资质 {job.required_qualification} 的引航员",
            )
        )

    # 逐引航员扫描可行开始分钟
    feasible_pilots = []
    for p in qualified:
        shifts = [(max(off(s), 0), min(off(e), H)) for s, e in p.work_windows]
        blocked_minutes = set()
        feasible = False
        for a, b in windows_off:
            for s in range(a, b + 1):
                e = s + d
                if e > H:
                    break
                in_shift = any(u <= s and e <= v for u, v in shifts)
                if not in_shift:
                    blocked_minutes.add(s)
                    continue
                clash = False
                for bs, be, _did in occ["pilot"][p.code]:
                    gap_before = dis + travel.get((job.land_location, _loc(inp, _did, "board")), 0)
                    gap_after = dis + travel.get((_loc(inp, _did, "land"), job.board_location), 0)
                    # _did 之后：本作业开始须 >= bs..be 任务结束 + gap
                    if s < be + gap_after and not (e + gap_before <= bs):
                        clash = True
                        break
                if clash:
                    blocked_minutes.add(s)
                else:
                    feasible = True
        if feasible:
            feasible_pilots.append(p.code)
        elif qualified:
            for a, b in _minutes_to_ranges(sorted(blocked_minutes), windows_off):
                item.conflicts.append(
                    Conflict(
                        kind="PILOT",
                        code=p.code,
                        blocked_from=dt(a),
                        blocked_to=dt(b + 1),
                        detail="班期外或被已排任务占用（含离船/转场时间）",
                    )
                )

    # 接送艇容量扫描：分钟级全局在途接送数与总座位比较
    if inp.boats:
        total_seats = sum(occ["boat_seats"][bt.code] for bt in inp.boats)
        usage = [0] * (H + 1)
        for bt in inp.boats:
            arr = occ["boat_count"][bt.code]
            for t in range(H + 1):
                usage[t] += arr[t]
        saturated = {t for t in range(H + 1) if usage[t] >= total_seats}

        boat_feasible_minutes = set()
        for a, b in windows_off:
            for s in range(a, b + 1):
                pick, drop = s - tr, s + d
                if pick < 0 or drop > H:
                    continue
                if pick not in saturated and drop not in saturated:
                    boat_feasible_minutes.add(s)

        if not boat_feasible_minutes:
            # 每艘艇单独给出其饱和时间段（容量被哪艘艇卡住）
            for bt in inp.boats:
                seats = occ["boat_seats"][bt.code]
                arr = occ["boat_count"][bt.code]
                sat = [
                    s for a, b in windows_off
                    for s in range(a, b + 1)
                    if (s - tr >= 0 and arr[s - tr] >= seats)
                    or (s + d <= H and arr[s + d] >= seats)
                ]
                for a, b in _minutes_to_ranges(sorted(set(sat)), windows_off):
                    item.conflicts.append(
                        Conflict(
                            kind="BOAT",
                            code=bt.code,
                            blocked_from=dt(a),
                            blocked_to=dt(b + 1),
                            detail=f"接送艇 {seats} 个座位在该时段全部在途",
                        )
                    )
            if not any(c.kind == "BOAT" for c in item.conflicts):
                # 全局容量饱和但单艇均未满（调度组合冲突）：报告全局饱和区间
                sat_global = [
                    s for a, b in windows_off for s in range(a, b + 1)
                    if s in saturated or (s + d) in saturated
                ]
                for a, b in _minutes_to_ranges(sorted(set(sat_global)), windows_off):
                    item.conflicts.append(
                        Conflict(
                            kind="BOAT", code="FLEET",
                            blocked_from=dt(a), blocked_to=dt(b + 1),
                            detail=f"全部接送艇合计 {total_seats} 个座位在该时段占满",
                        )
                    )
    else:
        item.conflicts.append(
            Conflict("BOAT", "ANY", job.ready_at, job.due_at, "没有可用接送艇")
        )

    return item


def _loc(inp, declaration_id, which):
    j = next(jj for jj in inp.jobs if jj.declaration_id == declaration_id)
    return j.board_location if which == "board" else j.land_location


def _minutes_to_ranges(minutes, clip_windows):
    ranges = []
    for a, b in clip_windows:
        seg = [t for t in minutes if a <= t <= b]
        if not seg:
            continue
        start = prev = seg[0]
        for t in seg[1:]:
            if t == prev + 1:
                prev = t
            else:
                ranges.append((start, prev))
                start = prev = t
        ranges.append((start, prev))
    return ranges
