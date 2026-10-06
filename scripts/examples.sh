#!/usr/bin/env bash
# 公交换乘票价清分服务 —— 调用示例（需先启动服务：docker compose up --build）
# 用法：bash scripts/examples.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
ACC="demo-account-001"
j() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 1) 健康检查 =="
curl -s "$BASE/healthz" | j; echo

echo "== 2) 提交三程：08:00(200) 09:30(500) 间隔90分钟同组；11:01(100) 另起一组 =="
for spec in "T1 2026-10-06T08:00:00+08:00 200" \
            "T2 2026-10-06T09:30:00+08:00 500" \
            "T3 2026-10-06T11:01:00+08:00 100"; do
  set -- $spec
  curl -s -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
    \"trip_id\": \"$1\", \"account_id\": \"$ACC\",
    \"boarding_at\": \"$2\", \"base_fare_cents\": $3
  }" | j; echo
done

echo "== 3) 同号同内容重传（幂等，idempotent=true，不产生新流水） =="
curl -s -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
  \"trip_id\": \"T1\", \"account_id\": \"$ACC\",
  \"boarding_at\": \"2026-10-06T08:00:00+08:00\", \"base_fare_cents\": 200
}" | j; echo

echo "== 4) 异内容冲突（409 IDEMPOTENCY_CONFLICT） =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/trips" \
  -H 'Content-Type: application/json' -d "{
  \"trip_id\": \"T1\", \"account_id\": \"$ACC\",
  \"boarding_at\": \"2026-10-06T08:05:00+08:00\", \"base_fare_cents\": 200
}"

echo "== 5) 查询运营日换乘组与实收（组费 500 + 100 = 600） =="
curl -s "$BASE/accounts/$ACC/days/2026-10-06" | j; echo

echo "== 6) 凭版本更正 T1 票价 200 -> 800（组费变 800，冲销500再记800） =="
curl -s -X POST "$BASE/trips/T1/correct" -H 'Content-Type: application/json' -d "{
  \"account_id\": \"$ACC\", \"boarding_at\": \"2026-10-06T08:00:00+08:00\",
  \"base_fare_cents\": 800, \"expected_version\": 1
}" | j; echo

echo "== 7) 陈旧版本更正（409 VERSION_CONFLICT） =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/trips/T1/correct" \
  -H 'Content-Type: application/json' -d "{
  \"account_id\": \"$ACC\", \"boarding_at\": \"2026-10-06T08:00:00+08:00\",
  \"base_fare_cents\": 800, \"expected_version\": 1
}"

echo "== 8) 撤销 T3（404/409 错误示例见 README） =="
curl -s -X POST "$BASE/trips/T3/cancel?expected_version=1" | j; echo

echo "== 9) 游标翻页查询流水（首页 limit=3，之后用 next_cursor） =="
curl -s "$BASE/accounts/$ACC/ledger?after_seq=0&limit=3" | j; echo
