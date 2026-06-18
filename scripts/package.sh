#!/usr/bin/env bash
# [온라인 단계] 폐쇄망 반입용 번들 생성: Docker 이미지 tar + 산출물 tgz
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# 산출물(images.tar + 멀티GB tgz)도 iCloud 밖에 둔다(기본 BUILD_HOME/dist). DIST 로 재정의 가능.
DIST="${DIST:-${BUILD_HOME:-$HOME/geocode-build}/dist}"
mkdir -p "$DIST"

# 대용량 산출물(geocode.sqlite 4.6GB, buildings 1GB, poi 174MB 등)은 iCloud(com~apple~CloudDocs)
# repo 안에 두면 evict/스로틀로 막히므로 iCloud 밖 BUILD_HOME 에 둔다. tiles/ 와 geocode.sqlite 는
# 여기서 가져오고, style/demo/vendor/server/docs/scripts 는 repo(ROOT)에서 가져온다.
BUILD_HOME="${BUILD_HOME:-$HOME/geocode-build}"
TILES_DIR="$BUILD_HOME/tiles"
GEOCODE_DB="$BUILD_HOME/geocode.sqlite"

echo "[1/4] 스타일 조립(최신화)"
"$ROOT/scripts/build-style.sh"

# tileserver-config.json 이 참조하는 mbtiles 5종 — 하나라도 빠진 번들은
# 폐쇄망에서 TileServer-GL 기동 실패/레이어 누락으로 이어지므로 패키징 단계에서 차단한다.
for mb in korea.mbtiles terrain.mbtiles dong.mbtiles buildings.mbtiles poi.mbtiles; do
  if [ ! -s "$TILES_DIR/$mb" ]; then
    echo "오류: $TILES_DIR/$mb 가 없거나 0바이트 — 02/03/05/10/12 생성 스크립트를 먼저 실행하세요." >&2
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

# geocode 서비스가 참조하는 통합 지오코딩 인덱스 — 없으면 geocode 컨테이너가 503 으로 뜨므로 차단.
if [ ! -s "$GEOCODE_DB" ]; then
  echo "오류: $GEOCODE_DB 가 없습니다 — scripts/09-gen-geocode.py 를 먼저 실행하세요." >&2
  exit 1
fi

# QC 게이트: 구조검사(NFC·좌표범위·시도커버리지·인덱스·스타일↔타일 정합) FAIL 시 번들 차단.
# (골든질의는 실행중 API가 필요하므로 패키징 단계에선 --api 생략 → 스킵/경고 처리)
echo "[2/4] QC 검증 게이트"
if [ -f "$ROOT/scripts/13-qc-check.py" ]; then
  python3 "$ROOT/scripts/13-qc-check.py" \
    --db "$GEOCODE_DB" --tiles "$TILES_DIR" \
    --style "$ROOT/style/style.json" --config "$ROOT/server/tileserver-config.json" --api "" \
    || { echo "오류: QC FAIL — 위 항목을 고친 뒤 다시 패키징하세요." >&2; exit 1; }
else
  echo "  (scripts/13-qc-check.py 없음 — QC 게이트 스킵)"
fi

echo "[3/4] Docker 이미지 (linux/amd64 강제 — 폐쇄망 x86_64 용)"
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
# 주의: `A && mv`로 묶으면 set -e가 A(docker save) 실패를 전파하지 못해 stale tar로 0종료한다.
# → 별도 명령으로 분리하여 set -e가 docker save 실패에서 즉시 중단하도록 한다.
docker save -o "$DIST/images.tar.tmp" "${SAVE_REFS[@]}" \
  || { echo "오류: docker save 실패" >&2; rm -f "$DIST/images.tar.tmp"; exit 1; }
mv "$DIST/images.tar.tmp" "$DIST/images.tar"

echo "[4/4] 산출물 번들"
# 번들 레이아웃은 airgap compose(server/docker-compose.yml)의 ../tiles, ../geocode/geocode.sqlite,
# ../style 등 상대마운트에 맞춘다. tiles 와 geocode.sqlite 는 BUILD_HOME 에 있으므로,
# geocode.sqlite 만 geocode/ 하위 레이아웃으로 스테이징(APFS clonefile=즉시·무추가공간)한 뒤 묶는다.
STAGE="$BUILD_HOME/.pkg-stage"
rm -rf "$STAGE"; mkdir -p "$STAGE/geocode"
cp -c "$GEOCODE_DB" "$STAGE/geocode/geocode.sqlite" 2>/dev/null \
  || cp "$GEOCODE_DB" "$STAGE/geocode/geocode.sqlite"   # clonefile 불가 시(타 볼륨) 일반 복사 폴백

# M2: vendor/ 는 maplibre·maputnik 등 오프라인 자산 전체를 포함하며 의도적으로 통째로 번들링함.
# C2: 번들 tgz 도 원자적으로 기록 (tmp → final rename 방식, 01-download-data.sh 와 동일 관례)
# `tar && mv`로 묶으면 set -e가 tar 실패를 전파하지 못해 stale 번들로 0종료한다 → 분리.
tar -czf "$DIST/cuvia-map-bundle.tgz.tmp" \
  -C "$ROOT"       style demo vendor server scripts/deploy.sh scripts/13-qc-check.py \
                   scripts/style-studio.py scripts/style_objects.py scripts/build_style.py \
                   scripts/build-style.sh scripts/start-style-studio.sh \
                   docs/integration-guide.md docs/data-licenses.md docs/data-sources.md \
                   THIRD-PARTY-NOTICES.md \
  -C "$BUILD_HOME" tiles \
  -C "$STAGE"      geocode \
  || { echo "오류: 번들 tar 실패" >&2; rm -f "$DIST/cuvia-map-bundle.tgz.tmp"; rm -rf "$STAGE"; exit 1; }
mv "$DIST/cuvia-map-bundle.tgz.tmp" "$DIST/cuvia-map-bundle.tgz"
rm -rf "$STAGE"
ls -lh "$DIST"
echo "반입 대상 2개: $DIST/images.tar, $DIST/cuvia-map-bundle.tgz"
