#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# T026 검증 — 인천 자치구 개편(안 A: 응답 경계 치환)을 **L1(DB) / L2(API) 2층으로 분리** 보고한다.
#
# 왜 2층인가 (T018 의 교훈):
#   T018 은 DB 값과 API 응답을 하나의 합격/불합격으로 뭉쳐 보고했다가 회귀 원인을 특정하지 못했다.
#   L1 이 통과했는데 L2 가 실패하면 원인은 **API 치환면**(S6)이고,
#   L1 이 실패하면 원인은 **원천·대응표**(S2·S3)다. 그래서 절대 섞지 않는다.
#
# 안 A 의 핵심 전제: **DB 는 한 행도 바꾸지 않는다.**
#   따라서 L1 은 "대응표가 올바로 만들어졌는가" 와 "본체 테이블이 그대로인가" 를 함께 본다.
#   L1-7new 가 그 증명이다 — 신 코드(28125/28155/28275/28290)가 본체에 0건이어야 한다.
#
# 검증 대상 서버 (Conductor 확정 구조. 계획서 §9-2 의 "8092 단일" 전제에서 벗어난 부분):
#   OURS   = http://127.0.0.1:8093   ← 우리 워크트리를 마운트한 `t026-verify` 컨테이너 (after)
#   BEFORE = http://127.0.0.1:8092   ← geocode-none-fix 워크트리를 마운트한 기준선 (before, 동결)
#   운영 서버(192.168.102.245 / 112.216.247.186)는 **대상이 아니다**. 기본값도 루프백이고,
#   루프백이 아닌 주소는 --allow-remote 없이는 거부한다 (과거 검증 스크립트가 기본값을 운영
#   서버로 두어 전량 호출한 사고가 있었다).
#
# L2 는 왜 여기서 다시 구현하지 않는가:
#   `server/test_incheon_sgg_remap_integration.py` 가 이미 I1~I11 을 판정한다.
#   같은 판정을 두 벌 두면 둘이 어긋났을 때 어느 쪽이 진실인지 알 수 없다.
#   이 스크립트는 그 테스트를 **실행하고 L2 번호로 옮겨 적는** 역할만 한다.
#
# 사용:
#   scripts/verify-incheon-remap.sh                 # L1 + L2 전부
#   scripts/verify-incheon-remap.sh --db-only       # L1 만 (API 컨테이너 없이)
#   scripts/verify-incheon-remap.sh --api-only      # L2 만
#   OURS=… BEFORE=… scripts/verify-incheon-remap.sh
#   PGHOST=localhost PGPORT=5433 PGUSER=cuvia PGDATABASE=cuvia PGPASSWORD=cuvia …
#
# 판정: 산출 지표가 모두 기대와 일치하면 exit 0, 아니면 exit 1.
#       **미산출 지표(L2-9·L2-10)는 통과로 집계하지 않는다** — 별도 줄로 보고한다.
#
# 의존: psql, python3(표준 라이브러리만). 폐쇄망 일관.
#   호스트에 psql 이 없고 DB 가 컨테이너에만 있으면 PSQL 로 우회한다 (다중 단어 커맨드 가능):
#     PSQL="docker exec -i server-postgis-1 psql -U cuvia -d cuvia" \
#       OURS=http://127.0.0.1:8093 bash scripts/verify-incheon-remap.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

PSQL="${PSQL:-psql}"
# PSQL 은 `docker exec -i server-postgis-1 psql -U cuvia -d cuvia` 같은 **다중 단어**도 받는다.
# "$PSQL" 로 인용 확장하면 전체가 하나의 실행파일명으로 취급돼 command not found 가 되므로
# 여기서 한 번만 배열로 쪼개 두고, 호출부는 "${PSQL_CMD[@]}" 를 쓴다.
read -r -a PSQL_CMD <<<"$PSQL"
PYTHON="${PYTHON:-python3}"
OURS="${OURS:-http://127.0.0.1:8093}"
BEFORE="${BEFORE:-http://127.0.0.1:8092}"

DO_DB=1
DO_API=1
ALLOW_REMOTE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --db-only)      DO_API=0 ;;
    --api-only)     DO_DB=0 ;;
    --ours)         OURS="$2"; shift ;;
    --before)       BEFORE="$2"; shift ;;
    --allow-remote) ALLOW_REMOTE=1 ;;
    -h|--help)      /usr/bin/sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "알 수 없는 인자: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── 안전장치: 운영 서버 오조준 방지 ───────────────────────────────────────────
_is_loopback() {
  case "$1" in
    http://127.0.0.1:*|http://localhost:*|http://[::1]:*) return 0 ;;
    *) return 1 ;;
  esac
}
if [ "$DO_API" = 1 ] && [ "$ALLOW_REMOTE" = 0 ]; then
  for u in "$OURS" "$BEFORE"; do
    if ! _is_loopback "$u"; then
      echo "✖ 거부: '$u' 는 루프백이 아니다. 운영 서버 오조준을 막기 위한 기본 정책이다." >&2
      echo "  의도한 것이라면 --allow-remote 를 명시하라(운영 서버는 조회만 허용된다)." >&2
      exit 2
    fi
  done
fi

PASS=0
FAIL=0
FAILED_IDS=""

# chk <id> <설명> <실측> <기대>
chk() {
  local id="$1" desc="$2" got="$3" want="$4"
  if [ "$got" = "$want" ]; then
    PASS=$((PASS + 1))
    printf '  %-9s %-38s = %-22s 기대 %-22s PASS\n' "$id" "$desc" "$got" "$want"
  else
    FAIL=$((FAIL + 1))
    FAILED_IDS="$FAILED_IDS $id"
    printf '  %-9s %-38s = %-22s 기대 %-22s ✖FAIL\n' "$id" "$desc" "$got" "$want"
  fi
}

# note <id> <설명> <값>  — 기대값을 고정하지 않고 기록만 하는 참고 지표
note() {
  printf '  %-9s %-38s = %s\n' "$1" "$2" "$3"
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ═════════════════════════════════════════════════════════════════════════════
# L1 — DB 층
# ═════════════════════════════════════════════════════════════════════════════
L1OUT="$TMP/l1.tsv"

_m() { /usr/bin/awk -F'\t' -v k="$1" '$1==k{print $2; f=1} END{if(!f) print "?"}' "$L1OUT"; }

run_l1() {
  echo "[L1] DB 층 — 대응표 정합 + 본체 무변경"
  echo "     (address 16M 행 전수 집계가 포함되어 수십 초 걸린다)"

  # 한 번의 psql 왕복으로 전 지표를 뽑는다 — 지표 간 스냅샷이 어긋나지 않게 하기 위해서다.
  "${PSQL_CMD[@]}" -X -q -A -t -F $'\t' -v ON_ERROR_STOP=1 > "$L1OUT" 2> "$TMP/l1.err" <<'SQL'
SELECT k, v FROM (
  SELECT 1 ord, 'L1-1' k, count(*)::text v FROM lawd_code_v2

  UNION ALL SELECT 2, 'L1-2', count(*)::text
    FROM lawd_code_v2 WHERE btrim(old_bcode) <> ''

  UNION ALL SELECT 3, 'L1-3', count(*)::text FROM lawd_sgg_remap

  -- 다중매핑(표) : old_emd8 이 PK 라 구조적으로 0 이지만, PK 가 바뀌는 개악을 잡기 위해 남긴다.
  UNION ALL SELECT 4, 'L1-4', (SELECT count(*)::text FROM (
      SELECT btrim(old_emd8) o FROM lawd_sgg_remap
      GROUP BY 1 HAVING count(DISTINCT btrim(new_emd8)) > 1) t)

  -- 다중매핑(원천) : 이쪽이 진짜 위험 지점이다. OLD_LAWDCD 한 값이 신 코드 둘로 갈리면
  --                 8자리 대응이 성립하지 않으므로 설계 전제 자체가 깨진다.
  UNION ALL SELECT 5, 'L1-4src', (SELECT count(*)::text FROM (
      SELECT left(btrim(old_bcode), 8) o FROM lawd_code_v2
       WHERE btrim(old_bcode) <> ''
         AND left(btrim(old_bcode), 5) IN ('28110', '28140', '28260')
       GROUP BY 1 HAVING count(DISTINCT left(btrim(bcode), 8)) > 1) t)

  UNION ALL SELECT 6, 'L1-5tot', count(*)::text
    FROM lawd_dong WHERE left(btrim(emd_cd), 5) IN ('28110', '28140', '28260')

  UNION ALL SELECT 7, 'L1-5', count(*)::text
    FROM lawd_dong d
   WHERE left(btrim(d.emd_cd), 5) IN ('28110', '28140', '28260')
     AND NOT EXISTS (SELECT 1 FROM lawd_sgg_remap r WHERE btrim(r.old_emd8) = btrim(d.emd_cd))

  UNION ALL SELECT 8, 'L1-6', (
    SELECT coalesce(string_agg(o || '>' || n || ':' || c, ' ' ORDER BY o, n), '(없음)')
      FROM (SELECT left(btrim(old_emd8), 5) o, left(btrim(new_emd8), 5) n, count(*) c
              FROM lawd_sgg_remap GROUP BY 1, 2) t)

  -- L1-7 : 안 B 였다면 UPDATE 대상이었을 행수. 안 A 는 이 행들을 **건드리지 않는다**.
  UNION ALL SELECT 9, 'L1-7addr', count(*)::text
    FROM address WHERE left(btrim(bcode), 5) IN ('28110', '28140', '28260')
  UNION ALL SELECT 10, 'L1-7parcel', count(*)::text
    FROM parcel WHERE sido_cd = '28' AND btrim(sgg_cd) IN ('28110', '28140', '28260')
  UNION ALL SELECT 11, 'L1-7building', count(*)::text
    FROM building WHERE sido_cd = '28' AND left(btrim(pnu), 5) IN ('28110', '28140', '28260')

  -- L1-7new : **DB 무변경의 증명**. 본체에 신 코드가 1건이라도 있으면 누군가 UPDATE 를 했다는 뜻이다.
  UNION ALL SELECT 12, 'L1-7new', (
      (SELECT count(*) FROM address  WHERE left(btrim(bcode), 5) IN ('28125','28155','28275','28290'))
    + (SELECT count(*) FROM parcel   WHERE sido_cd = '28' AND btrim(sgg_cd) IN ('28125','28155','28275','28290'))
    + (SELECT count(*) FROM building WHERE sido_cd = '28' AND left(btrim(pnu), 5) IN ('28125','28155','28275','28290'))
  )::text

  UNION ALL SELECT 13, 'L1-8', count(*)::text FROM lawd_sido_remap

  -- L1-9 : 전남·광주 치환층(623행)이 OLD_LAWDCD 정본과 어긋나지 않는지. 이번 변경의 무회귀 확인이다.
  UNION ALL SELECT 14, 'L1-9miss', count(*)::text
    FROM lawd_sido_remap r
   WHERE NOT EXISTS (
     SELECT 1 FROM lawd_code_v2 v
      WHERE left(btrim(v.bcode), 8) = btrim(r.new_emd8)
        AND left(btrim(v.old_bcode), 8) = btrim(r.old_emd8))

  -- L1-10 : 파티션 키 정합. 안 A 는 UPDATE 가 0 이므로 파티션 이동은 구조적으로 불가능하지만,
  --         그 전제(sido_cd 가 pnu 접두와 일치)가 실제로 성립하는지는 확인해 둔다.
  UNION ALL SELECT 15, 'L1-10', (
      (SELECT count(*) FROM parcel   WHERE sido_cd = '28' AND left(btrim(pnu), 2) <> '28')
    + (SELECT count(*) FROM building WHERE sido_cd = '28' AND left(btrim(pnu), 2) <> '28')
  )::text
) q ORDER BY ord;
SQL

  local rc=$?
  if [ $rc -ne 0 ]; then
    echo "  ✖ psql 실패 (rc=$rc):"
    /usr/bin/sed 's/^/    /' "$TMP/l1.err"
    FAIL=$((FAIL + 1))
    FAILED_IDS="$FAILED_IDS L1(psql)"
    return
  fi

  chk  "L1-1"  "lawd_code_v2 행수"            "$(_m L1-1)"    "31172"
  chk  "L1-2"  "OLD_LAWDCD 보유 행수"          "$(_m L1-2)"    "4357"
  chk  "L1-3"  "lawd_sgg_remap 행수(S4 후)"    "$(_m L1-3)"    "80"
  chk  "L1-4"  "다중매핑(표)"                  "$(_m L1-4)"    "0"
  chk  "L1-4src" "다중매핑(원천 OLD_LAWDCD)"   "$(_m L1-4src)" "0"
  note "L1-5tot" "옛3구 lawd_dong 행수"        "$(_m L1-5tot)"
  chk  "L1-5"  "옛3구 중 표 미커버"             "$(_m L1-5)"    "0"
  chk  "L1-6"  "옛→신 교차표"                  "$(_m L1-6)" \
       "28110>28125:44 28110>28155:8 28140>28125:7 28260>28275:11 28260>28290:10"
  note "L1-7"  "안 B 였다면 UPDATE 대상 행수" \
       "address $(_m L1-7addr) · parcel $(_m L1-7parcel) · building $(_m L1-7building)"
  chk  "L1-7new" "본체의 신 코드 잔존(무변경 증명)" "$(_m L1-7new)" "0"
  chk  "L1-8"  "lawd_sido_remap 행수"          "$(_m L1-8)"    "623"
  chk  "L1-9"  "sido_remap vs OLD_LAWDCD 불일치" "$(_m L1-9miss)" "0"
  chk  "L1-10" "파티션 키 불일치(시도 28)"      "$(_m L1-10)"   "0"
  echo
}

# ═════════════════════════════════════════════════════════════════════════════
# L2 — API 층 (통합 테스트에 위임하고 번호만 옮겨 적는다)
# ═════════════════════════════════════════════════════════════════════════════
IT="$REPO/server/test_incheon_sgg_remap_integration.py"
L2OUT="$TMP/l2.txt"

# 테스트 1건의 판정을 unittest -v 출력에서 읽는다. "test_x (…) … ok|FAIL|ERROR|skipped"
_t() {
  /usr/bin/awk -v t="$1" '
    index($0, t "(") == 1 || index($0, t " ") == 1 { hit = NR }
    # docstring 이 있는 테스트는 unittest -v 가 판정을 **두 번째 줄**에 낸다:
    #   test_x (__main__.C.test_x)
    #   <docstring 첫 줄> ... ok
    # 이름 줄만 보면 "... ok" 가 없어 매칭이 깨지고 실제 통과가 "?" 로 집계된다.
    # 그래서 이름 줄과 그 다음 줄까지를 판정 대상으로 삼는다.
    hit && NR <= hit + 1 {
      if ($0 ~ /\.\.\. ok$/)          { print "PASS"; f=1; exit }
      if ($0 ~ /\.\.\. skipped/)      { print "SKIP"; f=1; exit }
      if ($0 ~ /\.\.\. (FAIL|ERROR)/) { print "FAIL"; f=1; exit }
    }
    END { if (!f) print "?" }' "$L2OUT"
}

# chk2 <L2번호> <설명> <테스트명…>  — 여러 테스트가 한 지표를 이루면 전부 PASS 여야 PASS
chk2() {
  local id="$1" desc="$2"; shift 2
  local agg="PASS" t v names=""
  for t in "$@"; do
    v="$(_t "$t")"
    names="$names $t=$v"
    [ "$v" = "PASS" ] || agg="$v"
  done
  if [ "$agg" = "PASS" ]; then
    PASS=$((PASS + 1)); printf '  %-9s %-38s = PASS  (%s )\n' "$id" "$desc" "$names"
  else
    FAIL=$((FAIL + 1)); FAILED_IDS="$FAILED_IDS $id"
    printf '  %-9s %-38s = ✖%-4s (%s )\n' "$id" "$desc" "$agg" "$names"
  fi
}

run_l2() {
  echo "[L2] API 층 — OURS=$OURS  BEFORE=$BEFORE"
  if [ ! -f "$IT" ]; then
    echo "  ✖ 통합 테스트 부재: $IT"
    FAIL=$((FAIL + 1)); FAILED_IDS="$FAILED_IDS L2(파일없음)"
    return
  fi

  OURS="$OURS" BEFORE="$BEFORE" "$PYTHON" -m unittest -v \
      "server.test_incheon_sgg_remap_integration" > "$L2OUT" 2>&1 ||
    OURS="$OURS" BEFORE="$BEFORE" "$PYTHON" "$IT" -v > "$L2OUT" 2>&1

  chk2 "L2-1"  "인천 11건 신 구명 반환률"        test_i1_new_sgg_names
  chk2 "L2-2"  "b_code 신 코드 반환"             test_i1b_new_bcode
  chk2 "L2-3"  "옛 구명 입력 히트 유지(I3)"       test_i3_old_sgg_query_still_hits
  chk2 "L2-4"  "신 구명 입력 히트(I2)"            test_i2_new_sgg_query_hits
  chk2 "L2-5"  "동명 중복 오히트(I4)"             test_i4_gyeyang_oryu
  chk2 "L2-6"  "별칭 과확장(I5/I5-b 쌍)"          test_i5_seohae_daegok_zero test_i5b_geomdan_daegok_one
  chk2 "L2-7"  "전남·광주 표본 diff 0바이트"      test_i7_byte_identical
  chk2 "L2-8"  "areas[] adm_dong 오치환"          test_i8_adm_dong_code_untouched
  chk2 "L2-12" "인천 JSON 옛 구명 잔존(I11)"      test_i11_search_response test_i11_reverse_response
  echo "  ── 보조 케이스"
  chk2 "I6"    "중앙동1가 수기 보정행"            test_i6_jungangdong1ga
  chk2 "I9"    "인천 외 응답 바이트 동일"          test_i9_byte_identical
  chk2 "I10"   "DB 무변경(안 A)"                  test_i10_not_executed
  echo "  ── 선택 (S6c 미도입 시 미충족이 정상)"
  printf '  %-9s %-38s = %s  (test_i2b_nonexistent_sgg_returns_zero)\n' \
         "L2-13" "미등록 행정구역 토큰(I2-b)" "$(_t test_i2b_nonexistent_sgg_returns_zero)"
  echo
  echo "  L2-11(fail-open 시 바이트 동일)은 단위 테스트 server/test_incheon_sgg_remap.py 가 담당한다."
  echo "  (표 미적재 상태를 만들려면 프로세스 내부 스위치를 꺼야 해서 라이브 서버로는 판정할 수 없다.)"
  echo
}

# ═════════════════════════════════════════════════════════════════════════════
echo "════ T026 검증 — L1(DB) / L2(API) 2층 ════"
echo
[ "$DO_DB"  = 1 ] && run_l1
[ "$DO_API" = 1 ] && run_l2

echo "[L2-미산출] 595 순방향(L2-9)·b_code 10자리(L2-10)"
echo "     xlsx 원본 및 기준선 dump 부재로 실행 불가 (§7-4). **통과로 집계하지 않는다.**"
echo "     → 대체 가드: scripts/regression-guard-non-incheon.py (§9-4)"
echo "     대체 가드가 말할 수 있는 것은 '이번 변경으로 인한 회귀 없음'뿐이고,"
echo "     '절대 정확도 유지'는 말할 수 없다 — 기준선이 없기 때문이다."
echo
echo "════ 요약 ════  PASS=$PASS  FAIL=$FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "실패 지표:$FAILED_IDS"
  echo "원인 분리: L1 실패 → 원천·대응표(S2·S3) / L1 통과·L2 실패 → API 치환면(S6)"
  exit 1
fi
exit 0
