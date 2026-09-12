#!/usr/bin/env bash
# 端到端演示（需要先启动 API：uvicorn app.main:app，或 docker compose up）
set -euo pipefail
BASE=${BASE:-http://localhost:8000}

echo "== 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 1) 提交船舶申报（跨午夜窗口的深吃水船，已承诺）=="
curl -s -X POST "$BASE/declarations" -H 'Content-Type: application/json' \
  -d @examples/declaration.json | tee /tmp/decl1.json; echo

echo "== 1b) 再申报一艘白天靠泊的小货轮（供后续修订加入）=="
curl -s -X POST "$BASE/declarations" -H 'Content-Type: application/json' \
  --data-binary @examples/declaration2.json | tee /tmp/decl2-resp.json; echo
DECL2_ID=$(python3 -c "import json;print(json.load(open('/tmp/decl2-resp.json'))['id'])")

echo "== 2) 试排（只对第一条申报求解，返回草案 + 不可排任务的冲突诊断）=="
DECL1_ID=$(python3 -c "import json;print(json.load(open('/tmp/decl1.json'))['id'])")
python3 - "$DECL1_ID" <<'PY' > /tmp/solve-body.json
import json, sys
body = json.load(open('examples/solve.json'))
body.pop('_comment', None)
body['declaration_ids'] = [int(sys.argv[1])]
print(json.dumps(body))
PY
curl -s -X POST "$BASE/plans/solve" -H 'Content-Type: application/json' \
  --data-binary @/tmp/solve-body.json | tee /tmp/solve.json; echo

PLAN_ID=$(python3 -c "import json;print(json.load(open('/tmp/solve.json'))['plan_id'])")

echo "== 3) 正式锁定计划 =="
curl -s -X POST "$BASE/plans/$PLAN_ID/confirm"; echo

echo "== 4) 重复确认（幂等，不产生第二个有效计划/任务）=="
curl -s -X POST "$BASE/plans/$PLAN_ID/confirm"; echo

echo "== 5) 显式修订：把白天的第二艘船加入正式计划（带幂等键）=="
curl -s -X POST "$BASE/plans/$PLAN_ID/revise" -H 'Content-Type: application/json' \
  -d "{\"idempotency_key\":\"REV-EX-1\",\"add_declaration_ids\":[$DECL2_ID],\"note\":\"白天加船\"}"; echo

echo "== 6) 用相同幂等键再修订一次（返回同一份草案，不重复生成）=="
curl -s -X POST "$BASE/plans/$PLAN_ID/revise" -H 'Content-Type: application/json' \
  -d "{\"idempotency_key\":\"REV-EX-1\",\"add_declaration_ids\":[$DECL2_ID],\"note\":\"白天加船\"}"; echo
