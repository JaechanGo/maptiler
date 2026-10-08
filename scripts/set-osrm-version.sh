#!/usr/bin/env bash
# OSRM 이미지 버전 고정값 단일 관리 — versions.sh 의 OSRM_IMAGE 기본값과 docker-compose.yml 의
# osrm-car·osrm-foot·osrm-bike image 줄을 함께 바꾸고 검사한다.
# 그래프 파일 포맷이 OSRM 버전에 결합돼 있어 두 곳이 어긋나면(07 은 versions.sh 로 빌드, 서버는 compose 로 기동)
# osrm-routed 가 기동 실패한다. 손으로 고치지 말고 이 스크립트로만 바꾼다(07 의 OSRM_IMAGE_NEXT 경로가 호출).
#
# 사용:
#   set-osrm-version.sh <image-ref>   # 예: ghcr.io/project-osrm/osrm-backend:v26.10.0-debian
#   set-osrm-version.sh --check       # 일치 0 / 불일치·대상 줄 없음 1
#   set-osrm-version.sh --print       # versions.sh 기본값(파일 값 — env override 무시)
# 종료코드: 0 성공 / 1 불일치·대상 줄 없음 / 2 사용법·형식 오류
# bash 3.2(macOS)·4.2(CentOS 7) 공용 — sed -i 는 GNU/BSD 가 달라 awk 로 치환한다.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSIONS_FILE="${OSRM_VERSIONS_FILE:-$ROOT/scripts/versions.sh}"
COMPOSE_FILE="${OSRM_COMPOSE_FILE:-$ROOT/server/docker-compose.yml}"
REF_RE='^[a-z0-9][a-z0-9._/-]*/osrm-backend:[A-Za-z0-9][A-Za-z0-9._-]*$'
PRE='export OSRM_IMAGE="${OSRM_IMAGE:-'   # versions.sh 대상 줄 머리(문자열 비교 — 정규식 이스케이프 불필요)

usage() { echo "사용: $(basename "$0") <image-ref> | --check | --print" >&2; exit 2; }

# versions.sh 의 기본값 — 대상 줄 없으면 빈 문자열
pinned() {
  [ -f "$VERSIONS_FILE" ] || return 0
  awk -v pre="$PRE" 'index($0, pre) == 1 {
      rest = substr($0, length(pre) + 1); i = index(rest, "}")
      if (i > 1) { print substr(rest, 1, i - 1); exit } }' "$VERSIONS_FILE"
}

# compose 의 osrm-backend image 줄 — "줄번호<TAB>서비스<TAB>값".
# services 블록(다음 최상위 키 전까지) 안만 본다 — networks:/volumes: 아래 키를 서비스로 오인하지 않게.
# 서비스 키 = services: 아래 첫 들여쓰기 폭과 같은 키(2칸·4칸 모두) — 서비스 안쪽 volumes: 등은 제외.
compose_tags() {
  [ -f "$COMPOSE_FILE" ] || return 0
  awk '/^[^[:space:]#]/ { in_svc = ($0 ~ /^services:/); ind = -1; svc = "" }
       in_svc && /^[[:space:]]+[A-Za-z0-9_.-]+:[[:space:]]*$/ { k = match($0, /[^[:space:]]/) - 1
         if (ind < 0) ind = k
         if (k == ind) { svc = $1; sub(/:$/, "", svc) } }
       in_svc && /^[[:space:]]+image:[[:space:]]+[^[:space:]]*osrm-backend:[^[:space:]]+/ {
         v = $0; sub(/^[[:space:]]+image:[[:space:]]+/, "", v); sub(/[[:space:]].*$/, "", v)
         print NR "\t" (svc == "" ? "?" : svc) "\t" v }' "$COMPOSE_FILE"
}

# 두 파일의 대상 줄을 모두 확인 — 하나라도 없으면 아무것도 쓰지 않고 1
require_targets() {
  PIN="$(pinned)"; TAGS="$(compose_tags)"
  [ -n "$PIN" ] || { echo "오류: $VERSIONS_FILE 에 OSRM_IMAGE 기본값 줄이 없음" >&2; exit 1; }
  [ -n "$TAGS" ] || { echo "오류: $COMPOSE_FILE 에 osrm-backend image 줄이 없음" >&2; exit 1; }
}

do_check() {
  require_targets
  local bad=0 n svc v
  while IFS="$(printf '\t')" read -r n svc v; do
    if [ "$v" != "$PIN" ]; then echo "불일치: versions.sh=$PIN compose=$v($svc 줄 $n)"; bad=1; fi
  done <<< "$TAGS"
  [ "$bad" = 0 ] && echo "OK $PIN"
  return "$bad"
}

# 원자적 치환: 같은 디렉토리 임시파일(cp -p 로 권한 보존) → awk 결과로 덮기 → mv
rewrite() {   # $1=파일 $2=awk 프로그램 $3=새 값
  local f="$1" tmp
  tmp="$(mktemp "$f.XXXXXX")"
  cp -p "$f" "$tmp"
  if ! awk -v pre="$PRE" -v new="$3" "$2" "$f" > "$tmp"; then rm -f "$tmp"; echo "오류: $f 치환 실패" >&2; exit 1; fi
  mv -f "$tmp" "$f"
}

do_write() {
  local new="$1" cnt
  [[ "$new" =~ $REF_RE ]] || { echo "오류: image-ref 형식이 아님: '$new' (예: ghcr.io/project-osrm/osrm-backend:v26.10.0-debian)" >&2; exit 2; }
  require_targets
  cnt="$(printf '%s\n' "$TAGS" | wc -l | tr -d ' ')"
  if [ "$PIN" = "$new" ] && ! printf '%s\n' "$TAGS" | cut -f3 | grep -v -x -F -e "$new" >/dev/null; then
    echo "OSRM 이미지 변경 없음: $new"; return 0
  fi
  rewrite "$VERSIONS_FILE" 'index($0, pre) == 1 && !done {
      rest = substr($0, length(pre) + 1); i = index(rest, "}")
      if (i > 1) { $0 = pre new substr(rest, i); done = 1 } }
    { print }' "$new"
  rewrite "$COMPOSE_FILE" '/^[^[:space:]#]/ { in_svc = ($0 ~ /^services:/) }
    in_svc && /^[[:space:]]+image:[[:space:]]+[^[:space:]]*osrm-backend:[^[:space:]]+/ {
      match($0, /^[[:space:]]+image:[[:space:]]+/); head = substr($0, 1, RLENGTH); rest = substr($0, RLENGTH + 1)
      j = match(rest, /[[:space:]]/); tail = (j ? substr(rest, j) : "")
      $0 = head new tail }
    { print }' "$new"
  echo "OSRM 이미지: $PIN → $new (versions.sh 1줄, compose ${cnt}줄)"
}

[ $# -eq 1 ] || usage
case "$1" in
  --check) do_check ;;
  --print) PIN="$(pinned)"; [ -n "$PIN" ] || { echo "오류: $VERSIONS_FILE 에 OSRM_IMAGE 기본값 줄이 없음" >&2; exit 1; }; echo "$PIN" ;;
  -*|'') usage ;;
  *) do_write "$1" ;;
esac
