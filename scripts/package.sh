#!/usr/bin/env bash
# [온라인 단계] 폐쇄망 반입용 번들 생성: Docker 이미지 tar + 산출물 tgz
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
mkdir -p "$DIST"

echo "[1/3] 스타일 조립(최신화)"
"$ROOT/scripts/build-style.sh"

# tileserver-config.json 이 세 mbtiles 를 모두 참조 — 하나라도 빠진 번들은
# 폐쇄망에서 TileServer-GL 기동 실패로 이어지므로 패키징 단계에서 차단한다.
for mb in korea.mbtiles terrain.mbtiles dong.mbtiles; do
  if [ ! -f "$ROOT/tiles/$mb" ]; then
    echo "오류: tiles/$mb 가 없습니다 — 02/03/05 생성 스크립트를 먼저 실행하세요." >&2
    exit 1
  fi
done

# vendor 오프라인 자산이 비어있지(0바이트) 않은지 검증.
# iCloud(com~apple~CloudDocs) 경로에서 evict 되면 dataless 0바이트로 번들되어
# 폐쇄망 데모가 깨진다(빈 maplibre-gl.js → 지도 미초기화). [ -s ] = 존재 & 크기>0.
for asset in vendor/maplibre/maplibre-gl.js vendor/maplibre/maplibre-gl.css; do
  if [ ! -s "$ROOT/$asset" ]; then
    echo "오류: $asset 가 없거나 0바이트입니다 (iCloud evict 의심)." >&2
    echo "  → 01-download-data.sh 재실행 또는 'cat $asset >/dev/null' 로 materialize 후 다시 시도." >&2
    exit 1
  fi
done

# geocode 서비스가 참조하는 지오코딩 인덱스 — 없으면 geocode 컨테이너가 503 으로 뜨므로 차단.
if [ ! -s "$ROOT/geocode/geocode.sqlite" ]; then
  echo "오류: geocode/geocode.sqlite 가 없습니다 — scripts/07-gen-geocode.py 를 먼저 실행하세요." >&2
  exit 1
fi

echo "[2/3] Docker 이미지 (linux/amd64 강제 — 폐쇄망 x86_64 용)"
# compose 파일에 고정된 태그를 그대로 사용해 드리프트를 방지한다.
# ※ ROOT 에 공백이 포함될 수 있으므로 while read 로 라인 단위 파싱 (bash 3.2 호환)
TAGS=()
while IFS= read -r _tag; do
  TAGS+=("$_tag")
done < <(grep -E '^\s+image:' "$ROOT/server/docker-compose.yml" | awk '{print $2}')

# W2: compose 파일에서 이미지 태그를 하나도 파싱하지 못한 경우는 이후 처리가 무의미하다.
if [ "${#TAGS[@]}" -eq 0 ]; then
  echo "오류: docker-compose.yml 에서 image: 항목을 찾을 수 없습니다." >&2
  exit 1
fi

# Docker Desktop containerd 스토어에서는 멀티아치 인덱스 태그로 docker save 하면
# 로컬에 없는 다른 아치 레이어를 참조해 실패한다.
# 회피책: linux/amd64 단일 플랫폼 다이제스트를 pull 한 뒤 compose 고정 태그로 재태깅하고
# 태그로 save → RepoTags 가 보존되므로 폐쇄망 docker load 후 바로 compose up 가능.
SAVE_REFS=()
for tag in "${TAGS[@]}"; do
  # buildx imagetools inspect 출력에서 linux/amd64 플랫폼 전 마지막 Name: 값을 추출
  # (Name: 라인이 Platform: 라인보다 앞에 나오므로 "마지막 Name 추적" 방식 사용)
  # C1: pipefail 환경에서 inspect 실패 시 전체 스크립트가 abort 되지 않도록 || true 를 추가.
  #     awk 로직은 buildx 출력에서 Name: 이 Platform: 보다 먼저 나온다는 구조를 전제로 함.
  ref=$(docker buildx imagetools inspect "$tag" 2>/dev/null \
    | awk '/^  Name:/{name=$2} /^  Platform:.*linux\/amd64/{print name; exit}' \
    || true)
  if [ -z "$ref" ]; then
    echo "  경고: $tag 에서 linux/amd64 다이제스트를 찾지 못했습니다 — 태그 직접 사용"
    ref="$tag"
  fi
  echo "  pull $ref"
  docker pull "$ref"
  docker tag "$ref" "$tag"   # 단일 아치 이미지에 고정 태그를 다시 부여 → save 시 RepoTags 보존
  SAVE_REFS+=("$tag")
done

# C2: 이미지 tar 를 원자적으로 기록 (저장 도중 실패해도 이전 tar 를 오염시키지 않음)
docker save -o "$DIST/images.tar.tmp" "${SAVE_REFS[@]}" && mv "$DIST/images.tar.tmp" "$DIST/images.tar"

echo "[3/3] 산출물 번들"
# M2: vendor/ 는 maplibre·maputnik 등 오프라인 자산 전체를 포함하며 의도적으로 통째로 번들링함.
# C2: 번들 tgz 도 원자적으로 기록 (tmp → final rename 방식, 01-download-data.sh 와 동일 관례)
tar -czf "$DIST/cuvia-map-bundle.tgz.tmp" -C "$ROOT" \
  tiles style demo vendor server geocode scripts/deploy.sh docs/integration-guide.md \
  docs/data-licenses.md docs/data-sources.md THIRD-PARTY-NOTICES.md \
  && mv "$DIST/cuvia-map-bundle.tgz.tmp" "$DIST/cuvia-map-bundle.tgz"
ls -lh "$DIST"
echo "반입 대상 2개: dist/images.tar, dist/cuvia-map-bundle.tgz"
