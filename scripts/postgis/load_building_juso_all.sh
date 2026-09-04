#!/usr/bin/env bash
# juso 건물도형 전국 패치 — 시도별 순차(압축 해제 → load_building_juso.sh → 해제본 삭제).
#
# AL_D010(건물통합정보) 적재(load_building.sh) **다음에** 돈다 — 신개발지구 신축이
# AL_D010 에 수년 지연 반영되는 공백을 도로명주소 건물도형(부여 시점 생성)으로 메운다.
# 레이어 전략·dedup 규칙은 load_building_juso.sh 헤더 참조(건물군 vs 건물군내동).
#
# 원천(BUILD_HOME 기준):
#   sources/juso_building_shp/건물도형_전체분_{시도}.zip        (TL_SGCO_RNADR_MST 등)
#   sources/juso_building_dong/건물군내동도형_전체분_{시도}.zip  (TL_SGCO_RNADR_DONG 등)
#   staged/navi/match_build_{키}.txt                            (내비DB — 층수·건물명)
# 시도별로 풀고 적재한 뒤 해제본을 즉시 지운다(전국 동시 해제는 수십 GB).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_HOME="${BUILD_HOME:-$HOME/geocode-build}"
SHP_DIR="$BUILD_HOME/sources/juso_building_shp"
DONG_DIR="$BUILD_HOME/sources/juso_building_dong"
NAVI_DIR="$BUILD_HOME/staged/navi"
WORK="$BUILD_HOME/staged/_juso_work"

# 시도명(zip 파일명) → building 파티션 코드 · 내비 파일 접미.
# 12 = 전남광주통합특별시(2026 개편) — 원천·building 파티션 모두 12 단일 코드다.
declare -A SIDO_CD=(
  [서울특별시]=11 [부산광역시]=26 [대구광역시]=27 [인천광역시]=28
  [대전광역시]=30 [울산광역시]=31 [세종특별자치시]=36 [경기도]=41 [강원특별자치도]=51
  [충청북도]=43 [충청남도]=44 [전북특별자치도]=52 [경상북도]=47
  [경상남도]=48 [제주특별자치도]=50 [전남광주통합특별시]=12
)
declare -A NAVI_KEY=(
  [서울특별시]=seoul [부산광역시]=busan [대구광역시]=daegu [인천광역시]=incheon
  [대전광역시]=daejeon [울산광역시]=ulsan [세종특별자치시]=sejong
  [경기도]=gyunggi [강원특별자치도]=gangwon [충청북도]=chungbuk [충청남도]=chungnam
  [전북특별자치도]=jeonbuk [경상북도]=gyeongbuk [경상남도]=gyeongnam
  [제주특별자치도]=jeju [전남광주통합특별시]=jeonnamgwangju
)

[ -d "$SHP_DIR" ] || { echo "(건너뜀) juso 건물도형 없음: $SHP_DIR"; exit 0; }
mkdir -p "$WORK"

TOTAL=0; FAIL=0
for ZIP in "$SHP_DIR"/*.zip; do
  [ -e "$ZIP" ] || break
  NAME="$(basename "$ZIP" .zip)"; NAME="${NAME##*_}"
  CD="${SIDO_CD[$NAME]:-}"; NK="${NAVI_KEY[$NAME]:-}"
  [ -n "$CD" ] && [ -n "$NK" ] || { echo "⚠ 매핑 없음 — 건너뜀: $NAME"; continue; }
  NAVI="$NAVI_DIR/match_build_${NK}.txt"
  [ -s "$NAVI" ] || { echo "⚠ 내비DB 없음 — 건너뜀: $NAVI"; FAIL=1; continue; }

  echo "--- [$NAME / $CD]"
  rm -rf "${WORK:?}"/*
  unzip -o -q "$ZIP" -d "$WORK" || { echo "  ✗ 압축 해제 실패: $ZIP"; FAIL=1; continue; }
  DZIP="$DONG_DIR/건물군내동도형_전체분_${NAME}.zip"
  [ -s "$DZIP" ] && unzip -o -q "$DZIP" -d "$WORK"
  MST="$(find "$WORK" -name '*TL_SGCO_RNADR_MST*.shp' | head -1)"
  DONG="$(find "$WORK" -name '*TL_SGCO_RNADR_DONG*.shp' | head -1)"
  [ -n "$MST" ] || { echo "  ✗ 건물군 SHP 없음"; FAIL=1; continue; }
  [ -n "$DONG" ] || echo "  ⚠ 동도형 없음 — 아파트 단지가 한 덩어리로 보일 수 있음"

  bash "$HERE/load_building_juso.sh" \
       --mst "$MST" ${DONG:+--dong "$DONG"} --navi "$NAVI" --sido "$CD" || FAIL=1
  rm -rf "${WORK:?}"/*
done
rmdir "$WORK" 2>/dev/null || true

[ "$FAIL" = "0" ] && echo "OK: juso 건물 패치 완료" || { echo "✗ 일부 시도 실패 — 로그 확인" >&2; exit 1; }
# 단독 실행(월간 패치 등) 시 캐시 3겹 교체를 잊지 말 것 — load-all 경유면 거기서 체인된다.
echo "→ 단독 실행이었다면 다음을 꼭 실행: scripts/postgis/refresh_tile_cache.sh (martin L1·L2·스타일 버전)"
