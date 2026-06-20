#!/usr/bin/env bash
# [온라인 단계] OSM 추출본 → OpenMapTiles 스키마 벡터타일(.mbtiles)
# --download: Natural Earth/수역 폴리곤 등 보조 데이터 자동 다운로드(최초 1회, ~1GB)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p tiles data/osm   # 클린 clone 호스트 대비(출력 디렉토리)
# planetiler.jar 는 .gitignore 제외 벤더 바이너리 → clone 호스트엔 없음. JVM 의 모호한 메시지 대신 명확히 안내.
if [ ! -s planetiler/planetiler.jar ]; then
  echo "[오류] planetiler/planetiler.jar 없음 — 'scripts/setup-build-host.sh' 실행(자동 다운로드) 후 재시도" >&2
  exit 1
fi
# planetiler 는 Java 21+ 필요(v0.8.0~). 시스템 기본 java 가 17(타 서비스용)이어도 빌드만 21을 쓰도록 해석:
#   1) PLANETILER_JAVA 명시경로  2) 기본 java 가 21+ 면 그대로  3) /usr/lib/jvm 등에서 21+ 자동탐색
pick_java(){
  if [ -n "${PLANETILER_JAVA:-}" ]; then echo "$PLANETILER_JAVA"; return 0; fi
  if command -v java >/dev/null 2>&1 && java -version 2>&1 | grep -qE 'version "(2[1-9]|[3-9][0-9])'; then command -v java; return 0; fi
  for d in /usr/lib/jvm/*21* /usr/lib/jvm/*2[2-9]* /usr/lib/jvm/jre-21* \
           /opt/homebrew/opt/openjdk@21 /usr/local/opt/openjdk@21; do
    [ -x "$d/bin/java" ] && { echo "$d/bin/java"; return 0; }
  done
  echo java; return 0
}
JAVA_BIN="$(pick_java)"
JV="$("$JAVA_BIN" -version 2>&1 | head -1 || true)"
if ! printf '%s' "$JV" | grep -qE 'version "(2[1-9]|[3-9][0-9])'; then
  echo "[오류] planetiler 는 Java 21+ 필요 (현재: ${JV:-java 미발견}). 'scripts/setup-build-host.sh' 실행(java-21 설치) 또는 PLANETILER_JAVA 로 21 경로 지정" >&2
  exit 1
fi
echo "Java: $JV  ($JAVA_BIN)"
# -Xmx: 한국 규모는 12g면 충분. 더 큰 지역(대륙/행성)은 머신 RAM에 맞춰 상향할 것.
"$JAVA_BIN" -Xmx12g -jar planetiler/planetiler.jar \
  --osm_path="data/osm/south-korea.osm.pbf" \
  --output="tiles/korea.mbtiles" \
  --download --force
echo "벡터 타일 생성 완료: tiles/korea.mbtiles"
