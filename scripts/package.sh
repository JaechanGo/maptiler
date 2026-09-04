#!/usr/bin/env bash
# [온라인 단계] 폐쇄망 반입용 번들 생성: Docker 이미지 tar + 산출물 tgz
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/versions.sh"   # MAPLIBRE_VERSION 등 자산 버전 핀(01-download-data.sh 공용 단일출처)

# Docker 표준 bin 경로 PATH 보강 — build-studio 가 축소된 PATH(node 경로 위주)로 이 스크립트를
# 호출하면 `docker pull`/`buildx` 의 자격증명 헬퍼(docker-credential-desktop, credsStore=desktop)
# 를 못 찾아 "executable file not found in $PATH" 로 [3/4] Docker 이미지 단계가 종료코드 1로 실패한다.
# → Docker Desktop·Homebrew·/usr/local 의 bin 을 중복 없이 보강해 pull/buildx/creds 헬퍼를 모두 해석.
for _dbin in /Applications/Docker.app/Contents/Resources/bin /usr/local/bin /opt/homebrew/bin; do
  if [ -d "$_dbin" ]; then
    case ":$PATH:" in *":$_dbin:"*) ;; *) PATH="$PATH:$_dbin" ;; esac
  fi
done
export PATH
# 산출물(images.tar + 멀티GB tgz)도 iCloud 밖에 둔다(기본 BUILD_HOME/dist). DIST 로 재정의 가능.
DIST="${DIST:-${BUILD_HOME:-$HOME/geocode-build}/dist}"
mkdir -p "$DIST"

# 대용량 산출물(geocode.sqlite 4.6GB, buildings 1GB, poi 174MB 등)은 iCloud(com~apple~CloudDocs)
# repo 안에 두면 evict/스로틀로 막히므로 iCloud 밖 BUILD_HOME 에 둔다. tiles/ 와 geocode.sqlite 는
# 여기서 가져오고, style/demo/vendor/server/docs/scripts 는 repo(ROOT)에서 가져온다.
BUILD_HOME="${BUILD_HOME:-$HOME/geocode-build}"
TILES_DIR="$BUILD_HOME/tiles"
GEOCODE_DB="$BUILD_HOME/geocode.sqlite"

# tiles 통합 — 출력경로가 스크립트별로 갈린다: korea/dong/terrain 은 02/05/03 이 repo tiles/ 에,
# buildings/poi 는 10/12 가 BUILD_HOME/tiles 에 쓴다(dev tileserver 는 ../tiles=repo 마운트라 그쪽 필요).
# 번들/QC 정본은 BUILD_HOME/tiles 이므로, repo/tiles 에만 있거나 더 최신인 산출물을 여기로 모은다
# (APFS clonefile=즉시·무추가공간; 타 볼륨이면 일반 복사 폴백). → 번들이 5종을 빠짐없이 담는다.
mkdir -p "$TILES_DIR"
for mb in korea.mbtiles terrain.mbtiles dong.mbtiles admin.mbtiles; do   # buildings/poi 는 PostGIS→martin(/dyn) 서빙
  if [ -s "$ROOT/tiles/$mb" ] && { [ ! -s "$TILES_DIR/$mb" ] || [ "$ROOT/tiles/$mb" -nt "$TILES_DIR/$mb" ]; }; then
    echo "  ↪ tiles 통합: tiles/$mb → $TILES_DIR/$mb"
    cp -c "$ROOT/tiles/$mb" "$TILES_DIR/$mb" 2>/dev/null \
      || cp "$ROOT/tiles/$mb" "$TILES_DIR/$mb" \
      || { echo "오류: tiles 통합 복사 실패: tiles/$mb → $TILES_DIR/$mb (대용량 복사 중단/축출 의심 — repo 가 iCloud면 'brctl download tiles/$mb' 로 materialize 후 재시도)" >&2; exit 1; }
  fi
done

# route 통합 — 길찾기 그래프(FEAT-007). 07 이 repo route/{car,foot,bicycle} 에 쓰므로 tiles 와 동일하게
# BUILD_HOME/route 로 모은다(번들 정본). customize 완주 마커(.mldgr)가 더 최신일 때만 교체.
ROUTE_DIR="$BUILD_HOME/route"
for rp in car foot bicycle; do
  if [ -s "$ROOT/route/$rp/south-korea.osrm.mldgr" ] && \
     { [ ! -s "$ROUTE_DIR/$rp/south-korea.osrm.mldgr" ] || \
       [ "$ROOT/route/$rp/south-korea.osrm.mldgr" -nt "$ROUTE_DIR/$rp/south-korea.osrm.mldgr" ]; }; then
    echo "  ↪ route 통합: route/$rp → $ROUTE_DIR/$rp"
    rm -rf "$ROUTE_DIR/$rp"; mkdir -p "$ROUTE_DIR"
    # clonefile(-c) 실패 시(타 볼륨) 부분 생성된 목적지가 남는다 — 그대로 cp -R 하면
    # 기존 디렉토리 '안으로' 복사돼 route/car/car 중첩이 생기므로 반드시 지우고 폴백.
    cp -Rc "$ROOT/route/$rp" "$ROUTE_DIR/$rp" 2>/dev/null \
      || { rm -rf "$ROUTE_DIR/$rp"; cp -R "$ROOT/route/$rp" "$ROUTE_DIR/$rp"; } \
      || { echo "오류: route 통합 복사 실패: route/$rp" >&2; exit 1; }
  fi
done
# 길찾기 그래프 게이트 — compose osrm-car/foot/bike 가 ../route/{car,foot,bicycle}/south-korea.osrm 고정 참조.
# 없는 채 반출하면 폐쇄망에서 osrm 컨테이너 crash-loop + 데모 길찾기 실패. 레거시(길찾기 제외) 번들은 SKIP_ROUTE=1.
ROUTE_BUNDLE=""
if [ -n "${SKIP_ROUTE:-}" ]; then
  echo "  (건너뜀) 길찾기 그래프 — SKIP_ROUTE=1 (osrm 서비스는 폐쇄망에서 기동 실패 상태로 남음)"
else
  for rp in car foot bicycle; do
    [ -s "$ROUTE_DIR/$rp/south-korea.osrm.mldgr" ] || {
      echo "오류: $ROUTE_DIR/$rp/south-korea.osrm.mldgr 없음 — 길찾기 그래프 미빌드." >&2
      echo "  → scripts/07-gen-route-graph.sh 실행(또는 Build Studio '길찾기 그래프' 단계) 후 재패키징." >&2
      echo "    길찾기 없이 반출하려면 SKIP_ROUTE=1 로 재실행." >&2; exit 1; }
  done
  ROUTE_BUNDLE="route"
fi

# Build Studio 로 가져온 style.json(staged/style) 이 있으면 그대로 사용, 없으면 기본 조립.
echo "[1/4] 스타일 조립(최신화)"
STYLE_STAGED="$(ls -1 "$BUILD_HOME/staged/style/"*.json 2>/dev/null | head -n1 || true)"
if [ -n "${STYLE_STAGED:-}" ]; then
  echo "  가져온 스타일 사용: $STYLE_STAGED"
  STYLE_IMPORT="$STYLE_STAGED" "$ROOT/scripts/build-style.sh"
else
  "$ROOT/scripts/build-style.sh"
fi

# tileserver-config.json 이 참조하는 베이스 mbtiles 3종(korea/terrain/dong) — 하나라도 빠진 번들은
# 폐쇄망에서 TileServer-GL 기동 실패/레이어 누락으로 이어지므로 패키징 단계에서 차단한다.
# (buildings/poi/parcel 등 동적 레이어는 PostGIS→martin 서빙 — pg_dump 로 별도 번들, 위 WITH_POSTGIS)
for mb in korea.mbtiles terrain.mbtiles dong.mbtiles admin.mbtiles; do   # buildings/poi 는 PostGIS→martin(/dyn) 서빙
  if [ ! -s "$TILES_DIR/$mb" ]; then
    if [ "$mb" = terrain.mbtiles ]; then
      echo "오류: $TILES_DIR/$mb 없음 — 지형타일은 정적 산출물(빌드 그래프 'terrain' 단계=03-gen-terrain.sh)." >&2
      echo "  → Build Studio 에서 '지형 음영 타일' 단계를 빌드(온라인 SRTM·rio-rgbify 필요)하거나," >&2
      echo "    기존 빌드/배포본 terrain.mbtiles 를 $ROOT/tiles/ 에 두고 재패키징(tileserver-config 가 3종 고정 참조 → 필수)." >&2
    else
      echo "오류: $TILES_DIR/$mb 가 없거나 0바이트 — 이 빌드가 생성하는 베이스 타일(02/05). 먼저 생성 후 재시도." >&2
    fi
    exit 1
  fi
done

# vendor 오프라인 자산이 비어있지(0바이트) 않은지 검증 — 없으면 자동 복구(unpkg 재다운로드) 후 재검증.
# vendor/maplibre/* 는 .gitignore 대상이라 clone 본(빌드호스트)엔 없고 01-download-data.sh 가 채운다.
# build-studio 빌드그래프에 01 단계가 없어 비어있을 수 있으므로 패키징 직전 자동 복구한다.
# 맥(iCloud) 작업본에서 evict 되면 dataless 0바이트로 번들돼 폐쇄망 데모가 깨진다. [ -s ] = 존재 & 크기>0.
for asset in maplibre-gl.js maplibre-gl.css; do
  dst="$ROOT/vendor/maplibre/$asset"
  if [ ! -s "$dst" ]; then
    echo "  vendor 자산 누락/0바이트: vendor/maplibre/$asset → unpkg 재다운로드(@${MAPLIBRE_VERSION}) …" >&2
    mkdir -p "$ROOT/vendor/maplibre"
    if curl -fLs -o "$dst.tmp" "https://unpkg.com/maplibre-gl@${MAPLIBRE_VERSION}/dist/$asset" && [ -s "$dst.tmp" ]; then
      mv "$dst.tmp" "$dst" || rm -f "$dst.tmp"
    else
      rm -f "$dst.tmp"
    fi
  fi
  if [ ! -s "$dst" ]; then
    echo "오류: vendor/maplibre/$asset 가 없거나 0바이트입니다(자동 복구 실패)." >&2
    echo "  · 빌드호스트(인터넷 불가/에어갭): scripts/01-download-data.sh 로 vendor 자산을 먼저 받아두세요." >&2
    echo "  · 맥(iCloud) 작업본: evict 의심 — 'brctl download \"$dst\"' 또는 'cat \"$dst\" >/dev/null' 로 materialize 후 재시도." >&2
    exit 1
  fi
done
# maputnik(오프라인 스타일 편집기)은 선택 자산 — vendor 전체를 통째 번들하므로 없으면 번들에서 빠진다.
# 01-download-data.sh 가 받지만 build-studio 빌드그래프엔 01 단계가 없어 비어있을 수 있음(데모 지도 자체는 무관) → WARN 만.
[ -s "$ROOT/vendor/maputnik/dist/index.html" ] || \
  echo "  경고: vendor/maputnik 없음 — 번들에 오프라인 스타일 편집기 미포함(데모 동작엔 무관). 필요시 scripts/01-download-data.sh 실행." >&2

# glyphs(폰트 PBF)도 vendor 와 동일 — .gitignore 라 clone 본엔 없고 01-download-data.sh [3/5] 가 채운다.
# 누락 시 tileserver /fonts 가 400 → 라벨이 폴백으로만 렌더(한글은 localIdeographFontFamily 로 뜨지만 라틴·숫자 깨짐).
# 베이스 mbtiles 만 보는 QC 로는 안 잡히므로(실측: glyphs 빈 채 폐쇄망 반입) 패키징 직전 자동 복구 + 게이트.
GLYPH_REG="$ROOT/style/glyphs/KlokanTech Noto Sans Regular/0-255.pbf"
if [ ! -s "$GLYPH_REG" ]; then
  echo "  glyphs 누락/0바이트 → openmaptiles/fonts noto-open-sans(@${FONTS_VERSION:-v2.0}) 재다운로드 …" >&2
  if command -v unzip >/dev/null 2>&1; then
    _gz="$(mktemp /tmp/noto-open-sans.XXXXXX.zip)"; _gd="$(mktemp -d /tmp/noto_extract.XXXXXX)"
    if curl -fLs -o "$_gz" "https://github.com/openmaptiles/fonts/releases/download/${FONTS_VERSION:-v2.0}/noto-open-sans.zip" \
       && unzip -oq "$_gz" -d "$_gd"; then
      mkdir -p "$ROOT/style/glyphs"
      rm -rf "$ROOT/style/glyphs/KlokanTech Noto Sans Regular" "$ROOT/style/glyphs/KlokanTech Noto Sans Bold"
      mv "$_gd/Noto Sans Regular" "$ROOT/style/glyphs/KlokanTech Noto Sans Regular" 2>/dev/null || true
      mv "$_gd/Noto Sans Bold"    "$ROOT/style/glyphs/KlokanTech Noto Sans Bold"    2>/dev/null || true
    fi
    rm -rf "$_gz" "$_gd"
  else
    echo "  (unzip 미설치 — glyph 자동 복구 불가)" >&2
  fi
fi
if [ ! -s "$GLYPH_REG" ]; then
  echo "오류: style/glyphs 폰트(KlokanTech Noto Sans Regular/0-255.pbf)가 없습니다(자동 복구 실패)." >&2
  echo "  · 빌드호스트: scripts/01-download-data.sh [3/5] 로 글리프를 먼저 받아두세요(unzip 필요)." >&2
  echo "  · 누락 채로 번들되면 폐쇄망에서 /fonts 400 → 라벨 폰트가 폴백으로만 렌더됩니다." >&2
  exit 1
fi
echo "  glyphs OK — KlokanTech Noto Sans Regular/Bold 포함"

# 연동 가이드(tileserver frontPage + 게이트웨이 /·/info) — demo/guide.html 누락 시 :8080/ frontPage 가
# 빈 디렉토리로 깨지고 게이트웨이 /·/info 가 404 가 되는데 /health 는 200(과거 frontPage 사고류) → 패키징에서 차단.
# 특히 클론 빌드호스트엔 git untracked 파일이 존재하지 않으므로(이 파일이 커밋 안 되면 번들에서 조용히 누락) 게이트 필수.
GUIDE="$ROOT/demo/guide.html"
if [ ! -s "$GUIDE" ]; then
  echo "오류: demo/guide.html 가 없거나 0바이트 — 연동 가이드(:8080/ · 게이트웨이 /·/info)가 깨집니다." >&2
  echo "  · git 에 커밋됐는지 확인하세요 — 클론 빌드호스트엔 untracked 파일이 없습니다." >&2
  exit 1
fi
# frontPage 는 tileserver-gl 이 Handlebars 로 컴파일 → 이중 중괄호(mustache) 토큰이 있으면 렌더가 깨진다. 차단.
if grep -Fq '{{' "$GUIDE" || grep -Fq '}}' "$GUIDE"; then
  echo "오류: demo/guide.html 에 Handlebars 이중 중괄호 토큰이 있습니다 — frontPage 렌더가 깨집니다. 제거 후 재패키징." >&2
  exit 1
fi
echo "  guide.html OK — 연동 가이드 포함(Handlebars 토큰 없음)"

# geocode 서비스가 참조하는 통합 지오코딩 인덱스 — 없으면 geocode 컨테이너가 503 으로 뜨므로 차단.
if [ ! -s "$GEOCODE_DB" ]; then
  echo "오류: $GEOCODE_DB 가 없습니다 — scripts/09-gen-geocode.py 를 먼저 실행하세요." >&2
  exit 1
fi

# 행정경계 폴리곤(역지오코딩 동 판정)·표준 카테고리·법정/행정동 코드는 모두 geocode.sqlite 안에 적재되어
# 함께 반입된다(별도 areas.sqlite/cat-crosswalk 번들 불필요 — 빌드 시 09 가 DB 에 굽는다).
# areas 가 비면 역지오 동 폴리곤 없이 배포되므로 경고만(차단 X — areas 는 부가 레이어). 상세검증은 아래 QC 게이트.
AREAS_N=$(python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('SELECT count(*) FROM areas').fetchone()[0])" "$GEOCODE_DB" 2>/dev/null || echo 0)   # sqlite3 CLI 대신 python3 내장 모듈(리눅스 빌드호스트 미설치 대비)
if [ "${AREAS_N:-0}" -eq 0 ]; then
  echo "  경고: geocode.sqlite 에 행정경계 areas 0건 — 역지오코딩 동 폴리곤 없이 배포됩니다 (06-gen-areas.py + areas.sqlite 확인)." >&2
else
  echo "  geocode.sqlite OK — 행정경계 areas ${AREAS_N}건 포함(역지오 동 폴리곤)"
fi

# QC 게이트: 구조검사(NFC·좌표범위·시도커버리지·인덱스·스타일↔타일 정합) + 스타일 spec 실검증
#   (maplibre gl-style-validate = tileserver-gl 실로드 동치 — Node 자동감지, 미설치 시 표적 zoom 검사로 폴백)
#   FAIL 시 번들 차단 → spec 위반 스타일이 폐쇄망에서 /styles.json=[]·404 로 새어나가던 사고를 빌드 단계에서 막는다.
# 골든질의는 13-qc 가 geocode.sqlite 를 인프로세스 직접질의(서버 불필요) → --api "" 라도 회귀 검사 실동작.
echo "[2/4] QC 검증 게이트"
if [ -f "$ROOT/scripts/13-qc-check.py" ]; then
  python3 "$ROOT/scripts/13-qc-check.py" \
    --db "$GEOCODE_DB" --tiles "$TILES_DIR" \
    --style "$ROOT/style/style.json" --config "$ROOT/server/tileserver-config.json" --api "" \
    || { echo "오류: QC FAIL — 위 항목을 고친 뒤 다시 패키징하세요." >&2; exit 1; }
else
  echo "  (scripts/13-qc-check.py 없음 — QC 게이트 스킵)"
fi

if [ -n "${SKIP_IMAGES:-}" ]; then
  # 온라인 서버 배포: images.tar 불필요(서버가 'docker compose pull' 로 직접 받음). docker 미사용.
  echo "[3/4] Docker 이미지 — SKIP_IMAGES=1 → 건너뜀 (서버에서 docker compose pull)"
else
echo "[3/4] Docker 이미지 (linux/amd64 강제 — 폐쇄망 x86_64 용)"
# compose 파일에 고정된 태그를 그대로 사용해 드리프트를 방지한다.
# ※ ROOT 에 공백이 포함될 수 있으므로 while read 로 라인 단위 파싱 (bash 3.2 호환)
TAGS=()
while IFS= read -r _tag; do
  TAGS+=("$_tag")
done < <(grep -E '^\s+image:' "$ROOT/server/docker-compose.yml" | awk '{print $2}' | sort -u)   # osrm-car/foot 동일 태그 중복 제거

# W2: compose 파일에서 이미지 태그를 하나도 파싱하지 못한 경우는 이후 처리가 무의미하다.
if [ "${#TAGS[@]}" -eq 0 ]; then
  echo "오류: docker-compose.yml 에서 image: 항목을 찾을 수 없습니다." >&2
  exit 1
fi

# Docker Desktop containerd 스토어에서는 멀티아치 인덱스 태그로 docker save 하면
# 로컬에 없는 다른 아치 레이어를 참조해 실패한다.
# 회피책: linux/amd64 단일 플랫폼 다이제스트를 pull 한 뒤 compose 고정 태그로 재태깅하고
# 태그로 save → RepoTags 가 보존되므로 폐쇄망 docker load 후 바로 compose up 가능.
# WITH_POSTGIS: PostGIS 지오코더 이미지를 로컬 빌드(psycopg 베이크) → 아래 save 에 포함
if [ -n "${WITH_POSTGIS:-}" ]; then
  echo "  PostGIS 지오코더 이미지 빌드: cuvia-geocode-pg:local"
  docker build --platform linux/amd64 -t cuvia-geocode-pg:local \
    -f "$ROOT/server/geocode-pg.Dockerfile" "$ROOT/server"
fi

SAVE_REFS=()
for tag in "${TAGS[@]}"; do
  case "$tag" in
    *:local)   # 로컬 빌드 이미지(cuvia-geocode-pg:local) — registry pull 대상 아님. 존재할 때만 save.
      if docker image inspect "$tag" >/dev/null 2>&1; then
        echo "  로컬 이미지 포함: $tag"; SAVE_REFS+=("$tag")
      else
        echo "  (건너뜀) 로컬 이미지 미빌드: $tag — PostGIS 포함하려면 WITH_POSTGIS=1 로 재패키징"
      fi
      continue;;
  esac
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
fi

echo "[4/4] 산출물 번들"
# 번들 레이아웃은 airgap compose(server/docker-compose.yml)의 ../tiles, ../geocode/geocode.sqlite,
# ../style 등 상대마운트에 맞춘다. tiles 와 geocode.sqlite 는 BUILD_HOME 에 있으므로,
# geocode.sqlite 만 geocode/ 하위 레이아웃으로 스테이징(APFS clonefile=즉시·무추가공간)한 뒤 묶는다.
STAGE="$BUILD_HOME/.pkg-stage"
rm -rf "$STAGE"; mkdir -p "$STAGE/geocode"
# APFS clonefile(-c) → 하드링크 → 일반 복사 순 폴백. 리눅스(ext4/xfs)는 -c 가 없어 곧장 7.2GB 를
# 복사하는데, 디스크가 빠듯한 배포호스트에서 이것만으로 패키징이 실패한다(.244 실측: 여유 17GB).
# 하드링크는 같은 파일시스템이면 0바이트·즉시이고, tar 는 STAGE 안의 유일한 참조를 일반 파일로 담는다.
cp -c "$GEOCODE_DB" "$STAGE/geocode/geocode.sqlite" 2>/dev/null \
  || ln "$GEOCODE_DB" "$STAGE/geocode/geocode.sqlite" 2>/dev/null \
  || cp "$GEOCODE_DB" "$STAGE/geocode/geocode.sqlite"   # 타 볼륨이면 일반 복사 폴백

# WITH_POSTGIS: PostGIS 데이터 덤프(동적 레이어·지오코더 backbone) → postgis/cuvia.dump (pg_dump -Fc)
STAGE_POSTGIS=""
if [ -n "${WITH_POSTGIS:-}" ]; then
  mkdir -p "$STAGE/postgis"; STAGE_POSTGIS="postgis"
  PGC="${PG_CONTAINER:-server-postgis-1}"
  # 접속 경로 1회 판정(컨테이너 우선, 없으면 host) → 무결성 게이트·덤프가 동일 경로 사용.
  if docker exec "$PGC" pg_isready -U "${PGUSER:-cuvia}" >/dev/null 2>&1; then
    PG_MODE=container
  elif command -v pg_dump >/dev/null 2>&1 && command -v psql >/dev/null 2>&1; then
    PG_MODE=host
  else
    echo "오류: PostGIS 접속 불가 — postgis 컨테이너($PGC) 미가동 & host pg_dump/psql 없음." >&2; exit 1
  fi
  pg_psql() {   # psql -tAX -c "$1" — 컨테이너/호스트 공통 라우팅(컨테이너는 소켓 trust)
    if [ "$PG_MODE" = container ]; then
      docker exec "$PGC" psql -tAX -U "${PGUSER:-cuvia}" -d "${PGDATABASE:-cuvia}" -c "$1"
    else
      PGPASSWORD="${PGPASSWORD:-cuvia}" psql -tAX -h "${PGHOST:-localhost}" -p "${PGPORT:-5432}" \
        -U "${PGUSER:-cuvia}" -d "${PGDATABASE:-cuvia}" -c "$1"
    fi
  }

  # ── 덤프 전 무결성 게이트 ── 교착 등으로 parcel/building 이 미완·무인덱스인 채 폐쇄망 번들로 새어나가는 것을 차단.
  #   (실측 사고: parcel 5.6M/≈39.6M·GiST 미재생성인데 13-qc-check.py 는 PostGIS 미점검 → 그대로 pg_dump 될 뻔.)
  #   행수 임계는 전국 기본값(부분/지역 번들이면 PARCEL_MIN/BUILDING_MIN=0 으로 우회). 인덱스 유효성은 지역 무관 강신호.
  PARCEL_MIN="${PARCEL_MIN:-30000000}"; BUILDING_MIN="${BUILDING_MIN:-5000000}"
  pcnt=$(pg_psql "SELECT count(*) FROM parcel" 2>/dev/null | tr -dc 0-9 || true); pcnt=${pcnt:-0}
  bcnt=$(pg_psql "SELECT count(*) FROM building" 2>/dev/null | tr -dc 0-9 || true); bcnt=${bcnt:-0}
  vidx=$(pg_psql "SELECT count(*) FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid WHERE c.relname IN ('parcel_geom_gix','parcel_pnu_idx','building_geom_gix') AND i.indisvalid" 2>/dev/null | tr -dc 0-9 || true); vidx=${vidx:-0}
  pg_gate=0
  [ "$pcnt" -ge "$PARCEL_MIN" ]   || { echo "오류: parcel 행수 $pcnt < 임계 $PARCEL_MIN — 적재 미완 의심(STEPS=parcel 재적재). 우회=PARCEL_MIN=0" >&2; pg_gate=1; }
  [ "$bcnt" -ge "$BUILDING_MIN" ] || { echo "오류: building 행수 $bcnt < 임계 $BUILDING_MIN — 적재 미완 의심(STEPS=building). 우회=BUILDING_MIN=0" >&2; pg_gate=1; }
  [ "$vidx" -eq 3 ]               || { echo "오류: parcel/building 핵심 인덱스 유효 $vidx/3 — --fresh 적재가 인덱스 재생성 전 중단된 정황. load_parcel/building.sh 재실행 필요." >&2; pg_gate=1; }
  # ── address/poi 신선도 게이트 ── [실측 2026-09-03] load-all.sh 가 load_geocode.py 를 --phase 없이 불러 상시 실패했는데
  #   13-qc 는 sqlite 만 보고 위 게이트는 parcel/building 만 세어, 옛 address/poi(시도 오염·시설명 오매핑 포함)가
  #   pg_dump 로 번들에 실렸다. 적재는 sqlite 전량 복사라 행수가 **정확히** 같아야 한다(load_geocode 의 검증과 동일 기준).
  #   부분/지역 번들 등 의도적 불일치는 PG_GEOCODE_MATCH=0 으로 우회.
  if [ "${PG_GEOCODE_MATCH:-1}" = 1 ] && [ -f "$GEOCODE_DB" ]; then
    acnt=$(pg_psql "SELECT count(*) FROM address" 2>/dev/null | tr -dc 0-9 || true); acnt=${acnt:-0}
    ocnt=$(pg_psql "SELECT count(*) FROM poi" 2>/dev/null | tr -dc 0-9 || true); ocnt=${ocnt:-0}
    read -r exp_a exp_p < <(python3 - "$GEOCODE_DB" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
a = c.execute("SELECT count(*) FROM places").fetchone()[0]
p = c.execute("SELECT count(*) FROM places WHERE kind IN ('biz','facility') AND lon IS NOT NULL AND lat IS NOT NULL").fetchone()[0]
print(a, p)
PYEOF
)
    [ "$acnt" = "$exp_a" ] || { echo "오류: PostGIS address $acnt ≠ geocode.sqlite places $exp_a — load_geocode(swap) 미반영·옛 판. 재실행: STEPS=geocode scripts/postgis/load-all.sh (우회=PG_GEOCODE_MATCH=0)" >&2; pg_gate=1; }
    [ "$ocnt" = "$exp_p" ] || { echo "오류: PostGIS poi $ocnt ≠ geocode.sqlite biz/facility(좌표有) $exp_p — 위와 동일" >&2; pg_gate=1; }
  fi
  [ "$pg_gate" = 0 ] || { echo "✗ PostGIS 무결성 게이트 실패 — 손상 DB 번들링 차단." >&2; exit 1; }
  echo "  ✓ PostGIS 무결성 OK — parcel $pcnt · building $bcnt · 핵심 인덱스 3/3 유효 · address/poi = sqlite (${acnt:-미검} / ${ocnt:-미검})"

  echo "  PostGIS 덤프(pg_dump -Fc → postgis/cuvia.dump)"
  if [ "$PG_MODE" = container ]; then
    docker exec "$PGC" pg_dump -U "${PGUSER:-cuvia}" -d "${PGDATABASE:-cuvia}" -n public -Fc > "$STAGE/postgis/cuvia.dump"
  else
    PGPASSWORD="${PGPASSWORD:-cuvia}" pg_dump -h "${PGHOST:-localhost}" -p "${PGPORT:-5432}" \
      -U "${PGUSER:-cuvia}" -d "${PGDATABASE:-cuvia}" -n public -Fc > "$STAGE/postgis/cuvia.dump"
  fi
  [ -s "$STAGE/postgis/cuvia.dump" ] || { echo "오류: pg_dump 결과 0바이트" >&2; exit 1; }
  echo "    → postgis/cuvia.dump ($(du -h "$STAGE/postgis/cuvia.dump" | cut -f1))"
fi

# M2: vendor/ 는 maplibre·maputnik 등 오프라인 자산 전체를 포함하며 의도적으로 통째로 번들링함.
# C2: 번들 tgz 도 원자적으로 기록 (tmp → final rename 방식, 01-download-data.sh 와 동일 관례)
# `tar && mv`로 묶으면 set -e가 tar 실패를 전파하지 못해 stale 번들로 0종료한다 → 분리.
# COPYFILE_DISABLE=1: macOS tar 의 AppleDouble(._*) 리소스포크 동반을 차단한다 —
#   폐쇄망에서 풀면 style/layers/._*.json 같은 바이너리 쓰레기가 glob('*.json') 에 걸려
#   build_style 이 UnicodeDecodeError 로 죽는다(2026-08-31 .244 실측, 595개 유입).
COPYFILE_DISABLE=1 tar -czf "$DIST/cuvia-map-bundle.tgz.tmp" \
  -C "$ROOT"       style demo vendor server scripts/deploy.sh scripts/13-qc-check.py \
                   scripts/13i-route-qc.py \
                   scripts/style-studio.py scripts/style_objects.py scripts/build_style.py \
                   scripts/build-style.sh scripts/start-style-studio.sh \
                   docs/integration-guide.md docs/data-licenses.md docs/data-sources.md \
                   THIRD-PARTY-NOTICES.md \
  -C "$BUILD_HOME" tiles $ROUTE_BUNDLE \
  -C "$STAGE"      geocode $STAGE_POSTGIS \
  || { echo "오류: 번들 tar 실패" >&2; rm -f "$DIST/cuvia-map-bundle.tgz.tmp"; rm -rf "$STAGE"; exit 1; }
mv "$DIST/cuvia-map-bundle.tgz.tmp" "$DIST/cuvia-map-bundle.tgz"
rm -rf "$STAGE"

# 빌드 버전관리 — 매니페스트를 builds.json(최근 50건)에 추가. Build Studio '빌드 이력'이 읽음.
DIST="$DIST" BUILD_HOME="$BUILD_HOME" \
  VER="$(date +%Y%m%d-%H%M%S)" AT="$(date -Iseconds 2>/dev/null || date)" \
  SHA="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '')" \
  python3 - <<'PY' || echo "  (매니페스트 기록 건너뜀)"
import json, os
dist = os.environ["DIST"]
idx = os.path.join(dist, "builds.json")
def sz(p): return os.path.getsize(p) if os.path.exists(p) else 0
# 현재 데이터 기준일 스냅샷(있으면)
dv = os.path.join(os.environ.get("BUILD_HOME", ""), "data-versions.json")
srcs = {}
if os.path.exists(dv):
    try: srcs = {k: v.get("current") for k, v in json.load(open(dv)).items() if v.get("current")}
    except Exception: pass
entry = {"version": os.environ["VER"], "built_at": os.environ["AT"], "git": os.environ["SHA"],
         "bundle_bytes": sz(os.path.join(dist, "cuvia-map-bundle.tgz")),
         "images_bytes": sz(os.path.join(dist, "images.tar")), "sources": srcs}
builds = []
if os.path.exists(idx):
    try: builds = json.load(open(idx))
    except Exception: builds = []
builds.insert(0, entry)
json.dump(builds[:50], open(idx, "w"), ensure_ascii=False, indent=2)
print(f"  매니페스트: {entry['version']} → {idx}")
PY

ls -lh "$DIST"
if [ -n "${SKIP_IMAGES:-}" ]; then
  echo "반입 대상: $DIST/cuvia-map-bundle.tgz  (이미지는 서버에서 docker compose pull)"
else
  echo "반입 대상 2개: $DIST/images.tar, $DIST/cuvia-map-bundle.tgz"
fi
