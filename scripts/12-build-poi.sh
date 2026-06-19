#!/bin/bash
# geocode.sqlite의 시설(kind=biz) → poi.mbtiles (지도 라벨용, 줌별 밀도 자동조절)
set -e
export PATH="/opt/homebrew/bin:$PATH"
DB="$HOME/geocode-build/geocode.sqlite"
JL="$HOME/geocode-build/poi.geojsonl"
OUT="$HOME/geocode-build/tiles/poi.mbtiles"

echo "[1/2] biz → GeoJSONSeq (출처별 전건 적재 — 표시용 중복제거는 is_primary로)"
python3 - "$DB" "$JL" <<'PY'
import sqlite3, json, sys
db = sqlite3.connect(sys.argv[1]); n = 0
SKIP_NAMES = {"업소명없음", "상호명없음", "-", "."}   # 원본 상가데이터 플레이스홀더 제외
# 출처(source: sangga/localdata)·대표플래그(is_primary)를 그대로 타일에 실어 보낸다.
# 빌드시점 GROUP BY 제거 — 같은 점포라도 출처별 전건을 보존(스타일에서 출처 토글 가능).
# 중복 제거는 09-gen-geocode.py가 DB에서 is_primary=1 로 표시; 스타일 기본 필터가 대표만 표출.
with open(sys.argv[2], "w", encoding="utf-8") as f:
    for name, sub, cat1, cat2, source, is_primary, lon, lat in db.execute(
            "SELECT name,subtype,cat1,cat2,source,is_primary,lon,lat FROM places WHERE kind='biz'"):
        if lon is None or lat is None: continue
        if not name or name.strip() in SKIP_NAMES: continue
        f.write(json.dumps({"type":"Feature",
            "properties":{"name":name,"cat":sub or "","cat1":cat1 or "","cat2":cat2 or "",
                          "source":source or "","is_primary":int(is_primary or 0)},
            "geometry":{"type":"Point","coordinates":[round(lon,6),round(lat,6)]}},
            ensure_ascii=False) + "\n"); n += 1
print(f"  {n:,} features", file=sys.stderr)
PY

echo "[2/2] tippecanoe → $OUT (z11–17, 줌별 솎기 / 클러스터 제거 — 확대 시 라벨 표출)"
tippecanoe -o "$OUT" -l poi -Z11 -z17 \
  --drop-densest-as-needed \
  -y name -y cat -y cat1 -y cat2 -y source -y is_primary --maximum-tile-bytes=700000 --quiet --force "$JL"
rm -f "$JL"
echo "OK: $OUT ($(du -h "$OUT"|cut -f1))"
