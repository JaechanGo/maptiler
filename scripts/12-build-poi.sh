#!/bin/bash
# geocode.sqlite의 시설(kind=biz) → poi.mbtiles (지도 라벨용, 줌별 밀도 자동조절)
set -e
export PATH="/opt/homebrew/bin:$PATH"
DB="$HOME/geocode-build/geocode.sqlite"
JL="$HOME/geocode-build/poi.geojsonl"
OUT="$HOME/geocode-build/tiles/poi.mbtiles"

echo "[1/2] biz → GeoJSONSeq (이름+좌표 중복 제거)"
python3 - "$DB" "$JL" <<'PY'
import sqlite3, json, sys, re, unicodedata
db = sqlite3.connect(sys.argv[1]); n = 0
SKIP_NAMES = {"업소명없음", "상호명없음", "-", "."}   # 원본 상가데이터 플레이스홀더 제외
_PUNCT = re.compile(r"[\s()\[\]{}<>（）【】·.,/&-]+")
def _nrm(s):   # 상호 정규화: NFC + 공백·괄호·기호 제거 + 소문자 → 춘의닭집(치킨)==춘의닭집치킨
    return _PUNCT.sub("", unicodedata.normalize("NFC", s or "")).lower()
db.create_function("nrm", 1, _nrm)
# 같은 상호(정규화)+근접 좌표(3자리≈90m) 중복 1건만 — 상가+인허가/다중인허가 중복 제거.
# (4자리는 경계 함정으로 3~5m 중복도 놓쳐 3자리로 완화. 진짜 지점은 'OO점' 등 이름이 달라 유지)
with open(sys.argv[2], "w", encoding="utf-8") as f:
    for name, sub, cat1, lon, lat in db.execute(
            "SELECT name,subtype,cat1,lon,lat FROM places WHERE kind='biz' "
            "GROUP BY nrm(name), round(lon,3), round(lat,3)"):
        if lon is None or lat is None: continue
        if not name or name.strip() in SKIP_NAMES: continue
        f.write(json.dumps({"type":"Feature",
            "properties":{"name":name,"cat":sub or "","cat1":cat1 or ""},
            "geometry":{"type":"Point","coordinates":[round(lon,6),round(lat,6)]}},
            ensure_ascii=False) + "\n"); n += 1
print(f"  {n:,} features", file=sys.stderr)
PY

echo "[2/2] tippecanoe → $OUT (z11–17, 줌별 솎기 / 클러스터 제거 — 확대 시 라벨 표출)"
tippecanoe -o "$OUT" -l poi -Z11 -z17 \
  --drop-densest-as-needed \
  -y name -y cat -y cat1 --maximum-tile-bytes=700000 --quiet --force "$JL"
rm -f "$JL"
echo "OK: $OUT ($(du -h "$OUT"|cut -f1))"
