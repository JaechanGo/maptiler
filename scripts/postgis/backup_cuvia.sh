#!/usr/bin/env bash
# CUVIA PostGIS 전체 백업 런처 — 컨테이너 내부 pg_dump(17.x)로 cuvia 전체를
#   ~/geocode-build/backups/cuvia_YYYYMMDD_HHMM.dump (-Fc -Z6) 로 백업. 읽기/덤프만(DB 무수정).
#   대량 적재/UPDATE(특히 load_geocode 의 address+poi TRUNCATE) 직전의 복원점 확보.
#   동시 대량작업 cap=1 게이트: pg_advisory_lock(911001) — 덤프 동안 세션 유지로 보유.
#   버전정합: 서버 17.5 ⇄ 컨테이너 pg_dump(17.x). ⚠ 호스트 pg_dump(18.4) 단독 사용 금지(17.x 복원 비호환).
#
#   실행규약: env bash scripts/postgis/backup_cuvia.sh [--execute]   (zsh _safe_eval 회피)
#     · (기본) 인자 없음/--dry/--preflight : preflight 만 — DB 무영향, 사람 승인 불필요.
#     · --execute                          : 실 덤프 — ⚠ 사람 승인 후에만. 덤프 완료까지 블로킹(락 보유).
#       백그라운드 실행은 호출측에서:
#         nohup env bash scripts/postgis/backup_cuvia.sh --execute > run.log 2>&1 &
#   ⚠ 절대 금지: 공간확보 목적 Docker diskSizeMiB 직접편집/재시작(데이터 손실). 산출물 iCloud 밖(~/geocode-build).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$HERE/_pg-env.sh" 2>/dev/null || true
: "${PGUSER:=cuvia}"; : "${PGDATABASE:=cuvia}"; : "${PGPASSWORD:=cuvia}"; export PGPASSWORD

CTN="${CTN:-server-postgis-1}"
LOCK_KEY=911001
BK_DIR="${BK_DIR:-$HOME/geocode-build/backups}"

# 락 보유 세션/임시자원 상태(트랩 정리용)
LOCK_PID=""; FIFO=""; LOCK_OUT=""; OUT=""

die(){ echo "✗ $*" >&2; exit 1; }
dex(){ docker exec "$CTN" "$@"; }   # 컨테이너 실행 래퍼

# 셔뱅 다음 주석 헤더 블록만 출력(첫 비주석 라인에서 정지 — 라인번호 비의존)
usage(){ awk 'NR>1{ if(/^#/){sub(/^# ?/,"");print} else exit }' "$0"; }

require_container(){
  docker ps --format '{{.Names}}' | grep -qx "$CTN" \
    || die "컨테이너 미가동: $CTN (cd server && docker compose --profile postgis up -d postgis)"
}

# ── preflight: 사전점검(DB 무수정). 락은 단발 try(세션 종료로 자동해제 → 부수효과 없음). ──
preflight(){
  echo "━━ preflight (컨테이너=$CTN, DB=$PGDATABASE) ━━"
  require_container
  dex pg_isready -U "$PGUSER" -d "$PGDATABASE" >/dev/null || die "pg_isready 실패"
  echo "  pg_isready ✓"

  # 버전 추출은 "pg_dump (PostgreSQL) 17.5 (Debian 17.5-1.pgdg110+1)" 같은 변형에 견고하게 첫 N.N 만.
  local ver; ver="$(dex pg_dump --version | grep -oE '[0-9]+\.[0-9]+' | head -1)"
  case "$ver" in
    17.*) echo "  pg_dump(컨테이너)=$ver ✓ (17.x — 서버 17.5 정합)";;
    *)    die "컨테이너 pg_dump 버전 비정합: $ver (호스트 18.x 단독 사용 금지)";;
  esac
  if command -v pg_dump >/dev/null 2>&1; then
    echo "  (참고) 호스트 pg_dump=$(pg_dump --version 2>/dev/null | awk '{print $NF}') — 단독 사용 금지(검증/덤프는 컨테이너)"
  fi

  echo "  컨테이너 df(/var/lib/postgresql/data):"
  dex df -h /var/lib/postgresql/data | sed 's/^/    /'
  mkdir -p "$BK_DIR" || die "백업 폴더 생성 실패: $BK_DIR"
  echo "  호스트 df($BK_DIR):"
  df -h "$BK_DIR" | sed 's/^/    /'

  echo "  DB 크기(추정): $(dex psql -U "$PGUSER" -d "$PGDATABASE" -tAc "SELECT pg_size_pretty(pg_database_size('$PGDATABASE'))")"

  # 락 획득가능 단발 점검 — psql -c 종료 시 세션 닫혀 자동 해제(부수효과 없음)
  local g; g="$(dex psql -U "$PGUSER" -d "$PGDATABASE" -tAc "SELECT pg_try_advisory_lock($LOCK_KEY)")"
  [ "$g" = "t" ] || die "LOCK busy($LOCK_KEY) — 다른 대량작업 진행중. 중단"
  echo "  advisory_lock($LOCK_KEY) 획득가능 ✓"
  echo "✓ preflight OK"
}

# ── trap cleanup: 모든 종료경로(정상/에러/시그널)에서 unlock + 임시자원 정리 + 실패시 부분덤프 삭제 ──
cleanup(){
  local rc=$?
  if [ -n "$LOCK_PID" ]; then
    # 1) unlock SELECT 송신(락 즉시 해제) + \q(psql 명시 종료). docker exec -i 는 FIFO stdin EOF 를
    #    원격 psql 로 안정적으로 전달하지 못해(FD9 닫기만으로는 미종료) wait 가 무한대기할 수 있으므로
    #    \q 로 클린 종료를 유도한다. unlock 만으로도 락은 이미 풀린다(세션 종료와 무관).
    { printf 'SELECT pg_advisory_unlock(%s);\n\\q\n' "$LOCK_KEY" >&9; } 2>/dev/null || true
    exec 9>&- 2>/dev/null || true
    # 2) 바운드 대기(최대 ~5s) 후 강제 종료(\q 미반영/미EOF 대비). 락은 이미 해제되어 무해.
    local k=0
    while [ "$k" -lt 10 ]; do kill -0 "$LOCK_PID" 2>/dev/null || break; sleep 0.5; k=$((k+1)); done
    kill "$LOCK_PID" 2>/dev/null || true
    wait "$LOCK_PID" 2>/dev/null || true
  fi
  [ -n "$FIFO" ] && rm -f "$FIFO" 2>/dev/null || true
  [ -n "$LOCK_OUT" ] && rm -f "$LOCK_OUT" 2>/dev/null || true
  if [ "$rc" -ne 0 ] && [ -n "$OUT" ] && [ -f "$OUT" ]; then
    echo "  (cleanup) 비정상 종료(rc=$rc) → 부분 덤프 삭제: $OUT" >&2
    rm -f "$OUT" 2>/dev/null || true
  fi
}

# ── 락 보유 세션 — FIFO 로 stdin 유지(EOF 방지)해 덤프 종료 시점까지 락 보유(bash 3.2 호환, coproc 미사용) ──
hold_lock(){
  FIFO="$(mktemp -u)"; mkfifo "$FIFO" || die "mkfifo 실패"
  LOCK_OUT="$(mktemp)"
  # ⚠ FIFO writer 는 반드시 read+write(9<>)로 연다. 단순 9>"$FIFO" 는 리더가 열릴 때까지
  #   블로킹되는데 리더(docker exec)를 그 다음에 띄우므로 데드락. 9<> 는 같은 FD가 read 端도
  #   쥐어 블로킹 없이 열리고, FD9(유일 writer 端) 를 닫으면 docker psql 의 stdin 이 EOF→세션종료.
  exec 9<>"$FIFO"                                 # writer FD 유지 → psql 가 EOF 안 받음(닫을 때까지)
  docker exec -i "$CTN" psql -U "$PGUSER" -d "$PGDATABASE" -Atq < "$FIFO" >"$LOCK_OUT" 2>&1 &
  LOCK_PID=$!
  printf 'SELECT pg_try_advisory_lock(%s);\n' "$LOCK_KEY" >&9
  local i=0
  while [ "$i" -lt 20 ]; do                       # 최대 ~10s 폴링(0.5s*20)
    if grep -qx t "$LOCK_OUT" 2>/dev/null; then
      echo "  advisory_lock($LOCK_KEY) 보유(세션 유지, pid=$LOCK_PID) ✓"; return 0
    fi
    grep -qx f "$LOCK_OUT" 2>/dev/null && die "LOCK busy($LOCK_KEY) — 다른 대량작업 진행중. 중단"
    sleep 0.5; i=$((i+1))
  done
  die "LOCK 획득 타임아웃($LOCK_KEY) — $LOCK_OUT 확인"
}

# ── execute: 실 덤프(읽기만). 사람 승인 후에만. 덤프 완료까지 블로킹(락 보유). ──
execute(){
  trap cleanup EXIT INT TERM
  preflight                                       # 게이트 재실행(단발 락 점검 포함)

  local TS; TS="$(date +%Y%m%d_%H%M)"
  OUT="$BK_DIR/cuvia_${TS}.dump"
  local LOG="$BK_DIR/dump_${TS}.log"
  echo "━━ execute (TS=$TS) ━━" | tee -a "$LOG"

  hold_lock                                       # 덤프 전 구간 락 보유

  # stdout 파이프 1순위: 컨테이너 17.x pg_dump → 호스트 파일로 직접 스트림(컨테이너 임시·docker cp 불필요)
  echo "[start] $(date) — pg_dump(컨테이너) -Fc -Z6 --no-owner --no-privileges → $OUT" | tee -a "$LOG"
  if ! docker exec "$CTN" pg_dump -U "$PGUSER" -d "$PGDATABASE" \
        -Fc -Z6 --no-owner --no-privileges > "$OUT" 2>>"$LOG"; then
    die "pg_dump 실패 — $LOG 확인(부분 덤프는 cleanup 이 삭제)"
  fi

  # 무결성 검증(복원 없이 TOC 만, 컨테이너 17.x pg_restore)
  [ -s "$OUT" ] || die "덤프 size 0 — 실패: $OUT"
  docker cp "$OUT" "$CTN":/tmp/v_${TS}.dump >>"$LOG" 2>&1 || die "검증본 docker cp 실패"
  local hits
  hits="$(docker exec "$CTN" pg_restore --list /tmp/v_${TS}.dump 2>>"$LOG" \
            | grep -Ec "TABLE( DATA)?.*(parcel|address|poi|lawd_dong|lawd_sigungu)")"
  docker exec "$CTN" rm -f /tmp/v_${TS}.dump >>"$LOG" 2>&1 || true
  [ "${hits:-0}" -ge 5 ] || die "pg_restore -l 핵심 테이블 누락(hits=${hits:-0})"
  echo "  pg_restore -l 핵심 테이블 hits=$hits (>=5) ✓" | tee -a "$LOG"

  # 행수 리포트(환경별 상이 가능 → 하드 실패 게이트로 쓰지 않음)
  echo "  행수(리포트):" | tee -a "$LOG"
  local t c
  for t in parcel address lawd_dong lawd_sigungu poi; do
    c="$(dex psql -U "$PGUSER" -d "$PGDATABASE" -tAc "SELECT count(*) FROM $t" 2>>"$LOG" || echo '?')"
    echo "    $t = $c" | tee -a "$LOG"
  done

  echo "[done] $(date) size=$(du -h "$OUT" | cut -f1) → $OUT" | tee -a "$LOG"
  echo "✓ 백업 완료: $OUT (로그: $LOG)"
  # 락/임시자원 해제는 trap cleanup 이 정상종료(rc=0) 경로에서 OUT 보존하며 수행
}

case "${1:-}" in
  --execute)            execute;;
  -h|--help)            usage;;
  ""|--dry|--preflight) preflight;;
  *)                    echo "✗ 알 수 없는 인자: $1" >&2; echo; usage; exit 2;;
esac
