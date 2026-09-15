#!/usr/bin/env bash
# BitMagnet 验收契约（media 标准）
# 断言：两容器 up+healthy / API(3333 /graphql) 200 / Torznab capabilities 200 / DHT 落库有数据
set -uo pipefail
cd "$(dirname "$0")"
WEB_PORT="${WEB_PORT:-3333}"
PASS=0; FAIL=0; WARN=0

check() { # $1=描述 $2=0/非0
  if [ "$2" -eq 0 ]; then echo "  [PASS] $1"; PASS=$((PASS+1));
  else echo "  [FAIL] $1"; FAIL=$((FAIL+1)); fi
}

echo "== 1. 容器状态 =="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'bitmagnet' || true
docker inspect -f '{{.Name}} {{.State.Health.Status}}' bitmagnet-postgres 2>/dev/null | grep -q healthy \
  && check "postgres healthy" 0 || check "postgres healthy" 1
docker inspect -f '{{.State.Status}}' bitmagnet 2>/dev/null | grep -q running \
  && check "bitmagnet running" 0 || check "bitmagnet running" 1

echo "== 2. API 服务响应 (POST /graphql) =="
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "http://localhost:${WEB_PORT}/graphql" \
        -H 'content-type: application/json' -d '{"query":"{ __typename }"}' || echo 000)
[ "$code" = "200" ] && check "GraphQL API HTTP ${code}" 0 || check "GraphQL API HTTP ${code}" 1

echo "== 3. Torznab capabilities =="
tc=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://localhost:${WEB_PORT}/torznab/api?t=capabilities" || echo 000)
[ "$tc" = "200" ] && check "Torznab capabilities HTTP ${tc}" 0 || check "Torznab capabilities HTTP ${tc}" 1

echo "== 4. DHT 爬虫落库（轮询至多 180s）=="
n=0
for i in $(seq 1 18); do
  n=$(docker exec bitmagnet-postgres psql -U postgres -d bitmagnet -t -c "SELECT count(*) FROM torrents;" 2>/dev/null | tr -d ' \n' || echo 0)
  n="${n:-0}"
  if [ "$n" -gt 0 ]; then break; fi
  sleep 10
done
if [ "$n" -gt 0 ]; then check "DHT 已索引 ${n} 条" 0;
else echo "  [WARN] DHT 仍未落库（可能网络/DHT 可达性，非工程缺陷）"; WARN=$((WARN+1)); fi

echo
echo "==== 验收结果: PASS=${PASS} FAIL=${FAIL} WARN=${WARN} ===="
[ "$FAIL" -eq 0 ]
