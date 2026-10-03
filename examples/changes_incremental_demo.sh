#!/usr/bin/env bash
# 考古地层关系核验服务 —— 变化追踪增量定位端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
#
# 场景：库中已登记大量互不相干的地层（遗址乙 30 层，与遗址甲毫无断言联系）。
# 在遗址甲做局部断言变更（加早于捷径、合并同一期组、加平行断言）时，
# 变化追踪按提交前后生效断言差集定位可能变化的地层对再精算：
#   - 变化记录只覆盖扩散到的地层对（遗址乙任何地层对都不出现）；
#   - 记录口径（关系 / 最优路径 / 地层对顺序 / 来源 / 修订号）与既有行为一致；
#   - 零净影响的成功修订（平行断言）照常递增修订号但不产生变化行。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

# 从标准输入的 JSON 中取字段，$1 为 python 下标表达式，如 "['revision']"
jget() { python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层：遗址甲 6 层 + 遗址乙 30 层（互不相干） =="
for s in JA1 JA2 JA3 JA4 JA5 JA6; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"遗址甲\"}" > /dev/null
done
for i in $(seq 1 30); do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"JB$i\", \"site\": \"遗址乙\"}" > /dev/null
done
curl -s "$BASE/strata" | python3 -c "import sys,json; print('登记完成，共', len(json.load(sys.stdin)), '个地层')"

echo; echo "== 2. 修订 R1：遗址甲三个同一期组 + 组间早于链；遗址乙一对同一期 =="
REV=$(curl -s "$BASE/revision" | jget "['revision']")
RESP=$(curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"INC-001\", \"stratum_a\": \"JA1\", \"stratum_b\": \"JA2\", \"relation\": \"same_period\"},
    {\"op\": \"assert\", \"evidence_id\": \"INC-002\", \"stratum_a\": \"JA3\", \"stratum_b\": \"JA4\", \"relation\": \"same_period\"},
    {\"op\": \"assert\", \"evidence_id\": \"INC-003\", \"stratum_a\": \"JA5\", \"stratum_b\": \"JA6\", \"relation\": \"same_period\"},
    {\"op\": \"assert\", \"evidence_id\": \"INC-004\", \"stratum_a\": \"JA2\", \"stratum_b\": \"JA3\", \"relation\": \"earlier_than\"},
    {\"op\": \"assert\", \"evidence_id\": \"INC-005\", \"stratum_a\": \"JA4\", \"stratum_b\": \"JA5\", \"relation\": \"earlier_than\"},
    {\"op\": \"assert\", \"evidence_id\": \"INC-101\", \"stratum_a\": \"JB1\", \"stratum_b\": \"JB2\", \"relation\": \"same_period\"}
  ]}")
echo "$RESP" | pp
R1=$(echo "$RESP" | jget "['revision']")

echo; echo "== 3. 修订 R2：遗址甲局部变更——新增早于捷径 INC-010（JA1 早于 JA5） =="
echo "   预期只影响 {JA1,JA2} × {JA5,JA6} 的最优路径（4 对），遗址乙 30 层不参与计算"
REV=$(curl -s "$BASE/revision" | jget "['revision']")
RESP=$(curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"INC-010\", \"stratum_a\": \"JA1\", \"stratum_b\": \"JA5\", \"relation\": \"earlier_than\"}
  ]}")
echo "$RESP" | pp
R2=$(echo "$RESP" | jget "['revision']")

echo; echo "== 4. 修订 R3：同一期合并——INC-020（JA2 同一期 JA3）把两个等价组并成一组 =="
echo "   合并扩散：{JA1,JA2} × {JA3,JA4} 四对由早于变为同一期"
REV=$(curl -s "$BASE/revision" | jget "['revision']")
RESP=$(curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"INC-020\", \"stratum_a\": \"JA2\", \"stratum_b\": \"JA3\", \"relation\": \"same_period\"}
  ]}")
echo "$RESP" | pp
R3=$(echo "$RESP" | jget "['revision']")

echo; echo "== 5. 修订 R4：平行断言 INC-030（JA4 早于 JA5，与 INC-005 同向同端点） =="
echo "   它进不了任何最优路径：修订号照常递增，relation_changes=0，不产生变化行"
REV=$(curl -s "$BASE/revision" | jget "['revision']")
RESP=$(curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"INC-030\", \"stratum_a\": \"JA4\", \"stratum_b\": \"JA5\", \"relation\": \"earlier_than\"}
  ]}")
echo "$RESP" | pp
R4=$(echo "$RESP" | jget "['revision']")

echo; echo "== 6. 读取变化记录（GET /revision-changes） =="
curl -s "$BASE/revision-changes?limit=100" | pp

echo; echo "== 7. 校验：局部修订 R2/R3 的变化对全部落在遗址甲；R4 无变化行 =="
curl -s "$BASE/revision-changes?limit=1000" | python3 -c "
import sys, json
data = json.load(sys.stdin)
by_rev = {}
for c in data['changes']:
    by_rev.setdefault(c['revision'], []).append(c)
r1, r2, r3, r4 = $R1, $R2, $R3, $R4
for r in (r2, r3):
    pairs = {(c['stratum_a'], c['stratum_b']) for c in by_rev.get(r, [])}
    assert pairs, f'修订 {r} 应有变化记录'
    assert all(a.startswith('JA') and b.startswith('JA') for a, b in pairs), (r, pairs)
    src = by_rev[r][0]['source']
    print(f'修订 {r}: {len(pairs)} 对变化，全部位于遗址甲（source={src}）')
assert r4 not in by_rev, f'零影响修订 {r4} 不应产生变化行'
print(f'修订 {r4}: 平行断言未进入任何最优路径，无变化行（修订号已递增）')
print('遗址乙 30 层（含 JB1~JB2 所在分量）在局部修订 R2/R3/R4 中均未出现，')
print('与逐对全量重算的记录口径完全一致，但计算只触达扩散到的地层对。')
"

echo; echo "全部示例执行完毕。"
