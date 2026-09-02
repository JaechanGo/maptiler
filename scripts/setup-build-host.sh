#!/usr/bin/env bash
# 빌드 호스트 준비 — p7zip + Java 21(타 서비스 17 불간섭) + planetiler.jar 부트스트랩 + GDAL·tippecanoe + PostGIS 적재툴(psql·osm2pgsql).
# OS/패키지매니저 자동감지: Ubuntu·Debian(apt) / RHEL·Rocky·CentOS·BlueOnyx(dnf|yum+EPEL) / macOS(brew).
#   ./scripts/setup-build-host.sh
# (폐쇄망 서빙 서버는 불필요 — 빌드는 인터넷 빌드 호스트에서만 7z가 필요)
set -uo pipefail
have(){ command -v "$1" >/dev/null 2>&1; }
SUDO=""; [ "$(id -u)" -ne 0 ] && have sudo && SUDO="sudo"

echo "── [1/5] p7zip(7z) ─────────────────────────────"
if have 7z || have 7za || have 7zr; then
  echo "✓ 이미 설치됨: $(command -v 7z 7za 7zr 2>/dev/null | head -1)"
elif have apt-get; then
  echo "→ apt: p7zip-full"; $SUDO apt-get update -qq && $SUDO apt-get install -y p7zip-full
elif have dnf; then
  echo "→ dnf: EPEL + p7zip"
  # 죽은 서드파티 repo(예: EOL된 pgdg13 → 410)가 dnf 트랜잭션 전체를 막지 않도록 제외.
  # p7zip 미존재 미러 대비 신형 7zip(/usr/bin/7z) 폴백.
  $SUDO dnf install -y --disablerepo='pgdg*' epel-release 2>/dev/null
  $SUDO dnf install -y --disablerepo='pgdg*' p7zip p7zip-plugins \
    || $SUDO dnf install -y --disablerepo='pgdg*' 7zip
elif have yum; then
  echo "→ yum: EPEL + p7zip"
  $SUDO yum install -y --disablerepo='pgdg*' epel-release 2>/dev/null
  $SUDO yum install -y --disablerepo='pgdg*' p7zip p7zip-plugins \
    || $SUDO yum install -y --disablerepo='pgdg*' 7zip
elif have brew; then
  echo "→ brew: p7zip"; brew install p7zip
else
  echo "✗ 패키지 매니저(apt/dnf/yum/brew) 미발견 — p7zip 수동 설치 필요"
fi
if have 7z || have 7za || have 7zr; then echo "✓ 7z 사용 가능 → 내비DB .7z 업로드 시 빌드때 자동 추출"
else echo "✗ 7z 여전히 없음 — 위 메시지 확인(권한/네트워크) 또는 .txt 직접배치"; fi

echo
echo "── [2/5] Java 21 (planetiler 런타임) ────────────"
# planetiler v0.8.0~ 는 Java 21+ 필요(이전 v0.7.0 까지가 Java 17). 기존 Java 17 등 타 서비스는
# 건드리지 않고(전역 alternatives 변경 안 함) 21을 '나란히' 설치만 한다 — 빌드는 02-gen-vector.sh 가
# 이 21을 자동 선택(PLANETILER_JAVA 로 덮어쓰기 가능). pgdg* 죽은 repo 는 [1/5] 와 동일하게 제외.
java21_path(){   # 21+ JVM 의 java 경로 출력(없으면 빈 문자열). 기본 java 가 21+ 면 그걸 사용.
  if have java && java -version 2>&1 | grep -qE 'version "(2[1-9]|[3-9][0-9])'; then command -v java; return; fi
  for d in /usr/lib/jvm/*21* /usr/lib/jvm/*2[2-9]* /usr/lib/jvm/jre-21* \
           /opt/homebrew/opt/openjdk@21 /usr/local/opt/openjdk@21; do
    [ -x "$d/bin/java" ] && { echo "$d/bin/java"; return; }
  done
}
J="$(java21_path)"
if [ -n "$J" ]; then
  echo "✓ 이미 있음: $("$J" -version 2>&1 | head -1)  ($J)"
elif have apt-get; then
  echo "→ apt: openjdk-21-jre-headless"; $SUDO apt-get update -qq && $SUDO apt-get install -y openjdk-21-jre-headless
elif have dnf; then
  echo "→ dnf: java-21-openjdk-headless"; $SUDO dnf install -y --disablerepo='pgdg*' java-21-openjdk-headless
elif have yum; then
  echo "→ yum: java-21-openjdk-headless"; $SUDO yum install -y --disablerepo='pgdg*' java-21-openjdk-headless
elif have brew; then
  echo "→ brew: openjdk@21"; brew install openjdk@21
else
  echo "✗ 패키지 매니저 미발견 — Java 21+ 수동 설치 필요(Adoptium Temurin 등)"
fi
J="$(java21_path)"
if [ -n "$J" ]; then
  echo "✓ Java 21+ 준비: $J"
  echo "  (전역 기본 java 는 변경 안 함 — 빌드만 이 21을 사용. 강제 지정: PLANETILER_JAVA=$J)"
else
  echo "✗ Java 21+ 확보 실패 — OSM 벡터타일(osm_vector) 빌드 불가(UnsupportedClassVersionError)"
fi

echo
echo "── [3/5] planetiler.jar (OSM 벡터타일 빌드) ─────"
# .gitignore 제외 벤더 바이너리(~90MB) — clone 호스트엔 없으므로 공식 릴리스에서 부트스트랩.
# 02-gen-vector.sh 가 'java -jar planetiler/planetiler.jar' 로 사용. 없으면 osm_vector 빌드 불가.
# 재현성 위해 PLANETILER_URL 로 특정 버전 고정 가능(미지정 시 latest).
PROOT="$(cd "$(dirname "$0")/.." && pwd)"
JAR="$PROOT/planetiler/planetiler.jar"
PLANETILER_URL="${PLANETILER_URL:-https://github.com/onthegomap/planetiler/releases/latest/download/planetiler.jar}"
fetch(){ if have curl; then curl -fSL --retry 3 -o "$1" "$2"; elif have wget; then wget -qO "$1" "$2"; else return 127; fi; }
if [ -s "$JAR" ]; then
  echo "✓ 이미 있음: planetiler/planetiler.jar ($(du -h "$JAR" 2>/dev/null | cut -f1))"
else
  mkdir -p "$PROOT/planetiler"
  echo "→ 다운로드: $PLANETILER_URL"
  if fetch "$JAR.tmp" "$PLANETILER_URL"; then
    ok=1; sumtool="$(command -v sha256sum || command -v shasum || true)"
    # .sha256 동봉 — 잘린 파일/HTML 에러페이지를 무결성으로 차단(가능할 때만).
    if [ -n "$sumtool" ] && fetch "$JAR.sha256" "$PLANETILER_URL.sha256" 2>/dev/null; then
      want="$(awk '{print $1}' "$JAR.sha256")"; got="$($sumtool "$JAR.tmp" | awk '{print $1}')"
      [ -n "$want" ] && [ "$want" != "$got" ] && { ok=0; echo "✗ sha256 불일치 — 다운로드 손상"; }
      rm -f "$JAR.sha256"
    fi
    if [ "$ok" = 1 ]; then mv "$JAR.tmp" "$JAR"; else rm -f "$JAR.tmp"; fi
  else
    rm -f "$JAR.tmp" 2>/dev/null || true
    echo "✗ 다운로드 실패(curl/wget 미설치 또는 네트워크) — planetiler.jar 수동 배치: $JAR"
  fi
  if [ -s "$JAR" ]; then echo "✓ 받음: planetiler/planetiler.jar ($(du -h "$JAR" 2>/dev/null | cut -f1))"
  else echo "✗ planetiler.jar 확보 실패 — OSM 벡터타일(osm_vector) 빌드 불가"; fi
fi

echo
echo "── [4/5] 빌드 툴체인(GDAL·tippecanoe) 설치 ──────"
# 패키지 설치가 불가능한 호스트(EOL 배포판 등)를 위한 **도커 래퍼 폴백**.
#   [실측 2026-09-01 .244/CentOS 7] yum 미러가 EOL 로 사망해 gdal·tippecanoe 를 못 깐다.
#   이미 psql·ogr2ogr 가 같은 방식(컨테이너 실행)으로 대체돼 동작 중이라, 나머지 도구도 동일 규약으로 만든다.
#   호출부(스크립트)는 무수정 — PATH 의 실행파일처럼 보이고 cwd·/home 마운트가 유지된다.
# GDAL 이미지는 GEOS 포함본(alpine-normal)이어야 한다. alpine-small 은 GEOS 가 없어 ogr2ogr -simplify 가
#   피처마다 "ERROR 6: GEOS support not enabled" 을 내고 **단순화만 조용히 건너뛴다**(원본 도형 그대로 통과).
#   [실측 2026-09-02 .244] 06-gen-areas 가 8,983건 경고 후 비단순화 링으로 적재 — 데이터 누락은 아니나 areas 비대·PIP 저하.
GDAL_IMAGE="${GDAL_IMAGE:-ghcr.io/osgeo/gdal:alpine-normal-latest}"
TIPPE_IMAGE="${TIPPE_IMAGE:-naxgrp/tippecanoe:latest}"   # ghcr.io/felt/* 는 익명 pull 거부(실측)
BUILD_ROOT_MOUNT="${BUILD_ROOT_MOUNT:-${BUILD_HOME:-$HOME}}"   # 컨테이너에 그대로 마운트할 작업 루트
mk_docker_wrapper() {   # $1=명령명 $2=이미지 — 마운트 경로는 생성 시점에 확정해 박는다
  _bin="/usr/local/bin/$1"
  $SUDO sh -c "cat > '$_bin'" <<WRAP
#!/bin/sh
# $1 도커 래퍼 — setup-build-host.sh 가 생성(패키지 설치 불가 호스트 폴백).
# -i: gdaltransform 처럼 **stdin 으로 입력받는** 도구가 있어 항상 붙인다. 없으면 입력이 컨테이너에
#     닿지 않아 조용히 빈 출력을 내고, 호출부는 0건으로 흘러간다(실측: localdata '유지 0 · 제외 225만').
# /tmp 마운트: 호출부가 **/tmp 에 임시파일을 만들어 경로로 넘기는** 경우가 있다
#     (06-gen-areas.py → tmpXXXX.geojson). 작업루트만 마운트하면 컨테이너가 그 파일을 못 봐
#     FileNotFoundError 로 죽는다(실측 2026-09-01 areas 단계).
# 환경변수 전달: 호출부가 **env 로 동작을 지정**하는 도구가 있다(06-gen-areas.py 는
#     SHAPE_ENCODING=CP949 로 SHP 한글 속성을 해석시킨다). 컨테이너는 호스트 env 를 상속하지 않아
#     빠뜨리면 인코딩이 틀어져 UnicodeDecodeError 로 죽는다(실측 2026-09-01 areas 단계).
_envs=""
for _v in SHAPE_ENCODING CPL_DEBUG GDAL_DATA PGCLIENTENCODING OGR_GEOMETRY_ACCEPT_UNCLOSED_RING; do
  eval "_val=\\\${\$_v:-}"; [ -n "\$_val" ] && _envs="\$_envs -e \$_v=\$_val"
done
# shellcheck disable=SC2086
exec docker run --rm -i --network host \$_envs \\
  -v $BUILD_ROOT_MOUNT:$BUILD_ROOT_MOUNT \\
  -v /tmp:/tmp \\
  -w "\$(pwd)" \\
  $2 $1 "\$@"
WRAP
  $SUDO chmod +x "$_bin"
  echo "  ↪ 도커 래퍼 생성: $1 ($2, 마운트 $BUILD_ROOT_MOUNT)"
}

# GDAL(gdaltransform·ogr2ogr): 11-build-localdata.py 좌표변환(EPSG:5174→4326)·load_building.sh 변환에 필수.
if have gdaltransform && have ogr2ogr; then
  echo "✓ 이미 설치됨: GDAL ($(command -v ogr2ogr))"
elif have apt-get; then
  echo "→ apt: gdal-bin"; $SUDO apt-get install -y gdal-bin
elif have dnf; then
  echo "→ dnf: gdal"; $SUDO dnf install -y --disablerepo='pgdg*' gdal
elif have yum; then
  echo "→ yum: gdal"; $SUDO yum install -y --disablerepo='pgdg*' gdal
elif have brew; then
  echo "→ brew: gdal"; brew install gdal
else
  echo "✗ 패키지 매니저 미발견 — GDAL 수동 설치 필요"
fi
if ! (have gdaltransform && have ogr2ogr) && have docker; then
  echo "→ GDAL 패키지 설치 실패 → 도커 래퍼로 폴백 ($GDAL_IMAGE)"
  for _g in ogr2ogr gdaltransform gdalbuildvrt gdalwarp gdal_translate gdaladdo gdalinfo; do
    have "$_g" || mk_docker_wrapper "$_g" "$GDAL_IMAGE"
  done
fi
if have gdaltransform && have ogr2ogr; then echo "✓ GDAL 사용 가능"; else echo "✗ GDAL 여전히 없음 — localdata/buildings 빌드 불가(권한/네트워크 확인)"; fi

echo
# rio-rgbify(+rasterio): scripts/03-gen-terrain.sh 의 Terrain-RGB 인코딩(지형 음영 타일 terrain.mbtiles) 전용. (선택)
#   terrain 은 정적 산출물 — 빌드호스트에서 생성하거나, 기존 빌드/배포본 terrain.mbtiles 를 tiles/ 로 반입해도 동일.
if python3 -c 'import rio_rgbify' 2>/dev/null; then
  echo "✓ 이미 설치됨: rio-rgbify (지형 타일)"
else
  if ! python3 -m pip --version >/dev/null 2>&1; then   # pip 없으면 부트스트랩(best-effort)
    if have apt-get;   then echo "→ apt: python3-pip"; $SUDO apt-get install -y python3-pip
    elif have dnf;     then echo "→ dnf: python3-pip"; $SUDO dnf install -y --disablerepo='pgdg*' python3-pip
    elif have yum;     then echo "→ yum: python3-pip"; $SUDO yum install -y --disablerepo='pgdg*' python3-pip
    fi
  fi
  if python3 -m pip --version >/dev/null 2>&1; then
    echo "→ pip: rio-rgbify rasterio (지형 타일 생성용 — 선택)"
    python3 -m pip install --user -q rio-rgbify rasterio 2>/dev/null \
      || python3 -m pip install --break-system-packages -q rio-rgbify rasterio 2>/dev/null || true
    if python3 -c 'import rio_rgbify' 2>/dev/null; then
      echo "✓ rio-rgbify 설치 완료 (rio 가 PATH 에 없으면: export PATH=\$HOME/.local/bin:\$PATH)"
    else echo "△ rio-rgbify 설치 실패 — terrain 단계는 기존 terrain.mbtiles 를 tiles/ 로 반입해 대체 가능(필수 아님)."; fi
  else
    echo "△ pip 미설치(python3-pip 설치 실패/불가) — terrain 은 기존 terrain.mbtiles 를 tiles/ 로 반입해 대체."
  fi
fi

echo
# tippecanoe·tile-join: buildings·poi 벡터타일 생성/병합에 필수. apt/dnf 패키지 없음 → brew 또는 소스 빌드.
if have tippecanoe && have tile-join; then
  echo "✓ 이미 설치됨: tippecanoe ($(command -v tippecanoe))"
elif have brew; then
  echo "→ brew: tippecanoe"; brew install tippecanoe
elif have git && have make; then
  echo "→ 소스 빌드: felt/tippecanoe (C++ 툴체인+zlib+sqlite3 헤더 동반설치)"
  if have apt-get;   then $SUDO apt-get install -y build-essential libsqlite3-dev zlib1g-dev git
  elif have dnf;     then $SUDO dnf install -y --disablerepo='pgdg*' gcc-c++ make sqlite-devel zlib-devel git
  elif have yum;     then $SUDO yum install -y --disablerepo='pgdg*' gcc-c++ make sqlite-devel zlib-devel git
  fi
  _tcdir="${TMPDIR:-/tmp}/tippecanoe-build.$$"; rm -rf "$_tcdir"
  if git clone --depth 1 https://github.com/felt/tippecanoe "$_tcdir"; then
    if make -C "$_tcdir" -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"; then
      $SUDO make -C "$_tcdir" install && echo "✓ tippecanoe 설치 완료"
    else echo "✗ tippecanoe 빌드 실패 — build-essential/sqlite-devel 설치 여부 확인"; fi
    rm -rf "$_tcdir"
  else echo "✗ tippecanoe 소스 클론 실패(네트워크?)"; fi
else
  echo "✗ tippecanoe 설치 불가 — brew 또는 git+make 필요. 수동: git clone https://github.com/felt/tippecanoe && make -j && $SUDO make install"
fi
if ! (have tippecanoe && have tile-join) && have docker; then
  echo "→ tippecanoe 설치 실패 → 도커 래퍼로 폴백 ($TIPPE_IMAGE)"
  for _t in tippecanoe tile-join; do
    have "$_t" || mk_docker_wrapper "$_t" "$TIPPE_IMAGE"
  done
fi
if have tippecanoe && have tile-join; then echo "✓ tippecanoe 사용 가능"; else echo "✗ tippecanoe 여전히 없음 — buildings/poi 타일 빌드 불가"; fi

echo
echo "── [5/5] PostGIS 적재 클라이언트(psql·osm2pgsql) ─"
# PostGIS·martin 은 컨테이너로 구동(server/docker-compose.yml profile=postgis) → 호스트엔 '클라이언트'만 필요:
#   psql(libpq): scripts/postgis/apply-schema.sh + 적재 로더.  osm2pgsql: 분석용 도로망 PostGIS 적재.
#   shp2pgsql·ogr2ogr 은 [4/5] GDAL 및 postgresql-client 에 동봉.
if have psql; then echo "✓ 이미 설치됨: psql ($(command -v psql))"
elif have apt-get; then echo "→ apt: postgresql-client"; $SUDO apt-get install -y postgresql-client
elif have dnf;     then echo "→ dnf: postgresql"; $SUDO dnf install -y --disablerepo='pgdg*' postgresql
elif have yum;     then echo "→ yum: postgresql"; $SUDO yum install -y --disablerepo='pgdg*' postgresql
elif have brew;    then echo "→ brew: libpq(psql)"; brew install libpq && brew link --force libpq 2>/dev/null || true
else echo "✗ 패키지 매니저 미발견 — psql 수동 설치 필요"; fi
if have psql; then echo "✓ psql 사용 가능"; else echo "✗ psql 없음 — PostGIS 스키마/적재 불가"; fi
echo
if have osm2pgsql; then echo "✓ 이미 설치됨: osm2pgsql ($(command -v osm2pgsql))"
elif have apt-get; then echo "→ apt: osm2pgsql"; $SUDO apt-get install -y osm2pgsql
elif have dnf;     then echo "→ dnf: osm2pgsql"; $SUDO dnf install -y --disablerepo='pgdg*' osm2pgsql
elif have yum;     then echo "→ yum: osm2pgsql"; $SUDO yum install -y --disablerepo='pgdg*' osm2pgsql
elif have brew;    then echo "→ brew: osm2pgsql"; brew install osm2pgsql
else echo "✗ 패키지 매니저 미발견 — osm2pgsql 수동 설치 필요"; fi
if have osm2pgsql; then echo "✓ osm2pgsql 사용 가능"; else echo "✗ osm2pgsql 없음 — 분석용 도로 PostGIS 적재 불가(베이스 타일은 영향 없음)"; fi

echo
echo "── 최종 툴체인 점검 ──────────────────────────────"
for t in python3 java ogr2ogr gdaltransform tippecanoe tile-join psql osm2pgsql; do
  if have "$t"; then echo "  ✓ $t"; else echo "  ✗ $t (없음 — 해당 빌드 단계에 필요)"; fi
done
