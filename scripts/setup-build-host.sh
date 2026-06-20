#!/usr/bin/env bash
# 빌드 호스트 준비 — p7zip 설치 + Java 21 설치(타 서비스 17 불간섭) + planetiler.jar 부트스트랩 + 툴체인 점검.
# OS/패키지매니저 자동감지: Ubuntu·Debian(apt) / RHEL·Rocky·CentOS·BlueOnyx(dnf|yum+EPEL) / macOS(brew).
#   ./scripts/setup-build-host.sh
# (폐쇄망 서빙 서버는 불필요 — 빌드는 인터넷 빌드 호스트에서만 7z가 필요)
set -uo pipefail
have(){ command -v "$1" >/dev/null 2>&1; }
SUDO=""; [ "$(id -u)" -ne 0 ] && have sudo && SUDO="sudo"

echo "── [1/4] p7zip(7z) ──────────────────────────────"
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
echo "── [2/4] Java 21 (planetiler 런타임) ─────────────"
# planetiler v0.8.0~ 는 Java 21+ 필요(이전 v0.7.0 까지가 Java 17). 기존 Java 17 등 타 서비스는
# 건드리지 않고(전역 alternatives 변경 안 함) 21을 '나란히' 설치만 한다 — 빌드는 02-gen-vector.sh 가
# 이 21을 자동 선택(PLANETILER_JAVA 로 덮어쓰기 가능). pgdg* 죽은 repo 는 [1/4] 와 동일하게 제외.
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
echo "── [3/4] planetiler.jar (OSM 벡터타일 빌드) ──────"
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
echo "── [4/4] 빌드 툴체인 점검(없으면 안내) ───────────"
for t in python3 java ogr2ogr gdaltransform tippecanoe tile-join; do
  if have "$t"; then echo "  ✓ $t"; else echo "  ✗ $t (없음 — 해당 빌드 단계에 필요)"; fi
done
echo
echo "툴체인 설치 예(필요 시 · gdal/tippecanoe 등. Java 21 은 위 [2/4] 가 처리):"
echo "  Ubuntu : $SUDO apt-get install -y gdal-bin              # tippecanoe/tile-join 은 소스 빌드"
echo "  RHEL   : $SUDO dnf install -y gdal                      # tippecanoe 는 소스 빌드"
echo "  macOS  : brew install gdal tippecanoe openjdk"
