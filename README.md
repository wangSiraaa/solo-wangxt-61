# 引航站排班 API（虚构示例）

把 **船舶申报 × 潮汐通航窗口 × 引航员资质/可工作时段 × 接送艇容量** 放进同一个排班 API。

> ⚠️ 本项目所有潮汐、船舶吃水、航道水深、接送时间均为**离线虚构数据**，
> 潮窗仅按示例吃水规则计算，**不提供任何真实航行决策支持**。

技术栈：FastAPI · OR-Tools CP-SAT · PostgreSQL（测试用 SQLite）· 无前端。

## 模型与规则

### 通航窗口（示例吃水规则）

```
所需潮位下限 = 船舶吃水 + 富余水深(UKC) − 海图基准水深
另可叠加航道级 min_tide / max_tide（max_tide 模拟“流速过大禁止通航”）
```

离散潮位样本之间**线性插值**，按 1 分钟栅格判定可行性，再把连续可行分钟合并为
半开区间 `[start, end)`，**窗口天然可跨午夜**。船舶允许的开始窗口还会与申报
期望窗口 `[ready_at, due_at)` 求交。

### 引航员

* 必须持有船舶要求的资质（如 `TANKER`）；
* 整个作业必须落在某个可工作时段内（班期本身可以是跨午夜夜班）；
* 两个任务之间强制满足：

```
下一任务开始 >= 上一任务结束 + 离船时间(默认10min) + 离船点→下一登轮点转场时间
```

转场时间来自引航站之间的矩阵（`travel_times`，非对称），
**不是简单判断两个作业时间段是否相交**。

### 接送艇

* 每个任务有两次接送：作业开始前的登轮接送、作业结束后的离船接送，
  两次可由不同艇承担（由模型自行分配）；
* 同一条艇同一时刻在途接送数不超过其座位数（CP-SAT `AddCumulative` 容量约束）。

### 目标优先级（字典序）

1. 已承诺（`committed=true`）任务尽量全部排入；
2. 其余任务尽量排入；
3. 减少延误（开始时刻相对 `ready_at` 的等待分钟数，承诺任务权重更高）；
4. 同分倾向更早开始（tie-break）。

求解器固定单线程 + 固定随机种子（`num_search_workers=1, random_seed=42`），
同输入结果确定。

### 不可排任务的冲突诊断

未排入的申报会返回其潮位通航窗口，以及冲突资源与时间段：

| kind | 含义 |
|---|---|
| `TIDE` | 期望窗口内没有满足吃水规则的潮位 |
| `PILOT` | 无资质，或班期外，或被已排任务占用（含离船/转场缓冲） |
| `BOAT` | 接送艇座位在该时段全部在途（含全局艇队 `FLEET` 饱和） |

### 计划锁定与修订

* `POST /plans/solve` 只产生 `PROPOSED` 草案；
* `POST /plans/{id}/confirm` 正式锁定为 `CONFIRMED`，申报置为 `promised`，
  已锁定任务在后续求解中成为不可移动的固定占用；
* 锁定后只能通过 `POST /plans/{id}/revise` **显式修订**（新增/取消申报），
  旧计划标记 `SUPERSEDED` 留痕，生成新草案，确认后生效；
* 确认接口与修订接口均幂等：
  同一计划重复确认不会产生第二个有效任务；
  同一 `idempotency_key` 的修订返回同一草案；
  申报按 `request_id` 幂等，重复提交不产生第二条申报。

## 快速开始

### Docker（PostgreSQL）

```bash
docker compose up --build
# API: http://localhost:8000  文档: /docs
# 容器启动时自动建表并灌入虚构种子数据
```

### 本地（SQLite）

```bash
pip install -r requirements.txt
export DATABASE_URL="sqlite:///./pilotage.db"
python -m app.seed
uvicorn app.main:app --reload
```

### 请求样例

```bash
# 1. 船舶申报（幂等键 request_id，committed=true 为已承诺任务）
curl -X POST localhost:8000/declarations \
  -H 'Content-Type: application/json' -d @examples/declaration.json

# 2. 试排（草案，返回 scheduled + 不可排任务的冲突资源/时间段）
curl -X POST localhost:8000/plans/solve \
  -H 'Content-Type: application/json' -d @examples/solve.json

# 3. 正式锁定
curl -X POST localhost:8000/plans/1/confirm

# 4. 锁定后显式修订
curl -X POST localhost:8000/plans/1/revise \
  -H 'Content-Type: application/json' -d @examples/revise.json
```

完整脚本：`bash examples/run_demo.sh`。

### 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/fairways` `/vessels` `/tide-samples` `/pilots` `/boats` `/travel-times` | 基础资源 |
| POST/GET | `/declarations` | 船舶申报（`request_id` 幂等） |
| POST | `/plans/solve` | 试排，返回草案与冲突诊断 |
| POST | `/plans/{id}/confirm` | 正式锁定（重复确认幂等） |
| POST | `/plans/{id}/revise` | 显式修订（幂等键 + 新增/取消） |
| GET | `/plans` `/plans/{id}` | 计划查询 |

## 测试（确定性）

```bash
pytest -q
# 或容器：docker compose run --rm tests
```

覆盖的关键场景：

1. **跨午夜潮窗**：高潮在 00:12，深吃水船窗口覆盖 23:43–00:43，被正常排入；
2. **合格人员充足但接送艇不足**：夜班 5 名合格引航员，2 个艇座位，
   窄窗内多艘船的登轮接送无法并行，未排入任务返回 `BOAT` 冲突与时间段；
3. **重复确认幂等**：同一草案重复确认不产生第二个有效任务，申报重复提交不新增；
4. 引航员两任务之间的**离船 + 转场缓冲**（不是时间相交判断）；
5. 容量受限时**已承诺任务优先**；
6. 锁定后必须走显式修订、旧计划 `SUPERSEDED` 留痕、修订幂等、取消承诺任务；
7. 同输入两次求解结果完全一致（确定性）。

## 虚构种子数据

* 航道 `CH01`：海图基准水深 6.0m，UKC 0.5m；
* 船舶：小货轮（吃水 5.5m，`GENERAL`）、深吃水货轮/油轮（8.4m，后者需 `TANKER`）；
* 6 个高潮点（9/15–9/17），尖峰余弦形态潮位，10 分钟一个样本；
* 5 名引航员（含跨午夜班期）、2 艘 1 座接送艇、BAR/DOCKS/ANCH 转场矩阵。

## 目录

```
app/
  main.py          FastAPI 入口
  models.py        ORM（SQLAlchemy 2.0）
  schemas.py       Pydantic 模型
  tide.py          潮位窗口（插值/吃水规则/区间求交）
  scheduler.py     OR-Tools CP-SAT 模型 + 冲突诊断
  service.py       求解输入组装 / 草案持久化 / 锁定 / 取代
  seed.py          离线虚构种子数据
  routers/         resources / declarations / plans
examples/          请求样例与端到端脚本
tests/             确定性测试
```
