#!/usr/bin/env python3
"""data/dong/dong-labels.geojson → tiles/dong.mbtiles (벡터타일, 레이어명 'dong').

순수 파이썬 MVT(Mapbox Vector Tile spec 2.1) 인코더 — 외부 의존성 없음.
포인트 전용이라 클리핑/심플리피케이션이 불필요해 직접 인코딩이 안전하다.

- 줌: z13~z14 생성 (TileJSON maxzoom=14 → MapLibre 가 z15+ 는 오버줌)
- 라벨 앵커는 정확히 한 타일에만 속하므로 버퍼 중복 불필요(포인트 특성)
- 04-gen-dong-labels.py 출력이 입력. 추후 국가 건물DB로 교체 시에도
  동일 GeoJSON 스키마({"dong": "101동"} 포인트)만 맞추면 이 스크립트 재사용.
"""
import gzip
import json
import math
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "data/dong/dong-labels.geojson"
OUT = ROOT / "tiles/dong.mbtiles"
ZOOMS = (13, 14)
EXTENT = 4096

if not SRC.exists():
    sys.exit(f"오류: {SRC} 가 없습니다 — scripts/04-gen-dong-labels.py 를 먼저 실행하세요.")

# ---- protobuf 최소 인코더 -------------------------------------------------
def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)

def zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)

def field_key(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)

def ld(field: int, payload: bytes) -> bytes:
    """length-delimited 필드"""
    return field_key(field, 2) + varint(len(payload)) + payload

def packed(field: int, ints) -> bytes:
    return ld(field, b"".join(varint(i) for i in ints))

def encode_feature(value_idx: int, px: int, py: int) -> bytes:
    body = packed(2, (0, value_idx))                      # tags: key[0]="dong" → value
    body += field_key(3, 0) + varint(1)                   # type = POINT
    body += packed(4, (9, zigzag(px), zigzag(py)))        # MoveTo(1) ×1 + 좌표
    return ld(2, body)                                    # Layer.features

def encode_layer(features: bytes, values) -> bytes:
    body = field_key(15, 0) + varint(2)                   # version = 2
    body += ld(1, b"dong")                                # name
    body += features
    body += ld(3, b"dong")                                # keys[0]
    for v in values:                                      # values[i] = string Value
        body += ld(4, ld(1, v.encode("utf-8")))
    body += field_key(5, 0) + varint(EXTENT)              # extent
    return ld(3, body)                                    # Tile.layers

# ---- 타일 좌표 ------------------------------------------------------------
def lonlat_to_tile(lon: float, lat: float, z: int):
    n = 1 << z
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    tx = min(max(int(x), 0), n - 1)
    ty = min(max(int(y), 0), n - 1)
    px = min(max(int((x - tx) * EXTENT), 0), EXTENT - 1)
    py = min(max(int((y - ty) * EXTENT), 0), EXTENT - 1)
    return tx, ty, px, py

# ---- 변환 -----------------------------------------------------------------
feats = json.loads(SRC.read_text(encoding="utf-8"))["features"]
print(f"입력: {len(feats):,}개 동 라벨")

lons = [f["geometry"]["coordinates"][0] for f in feats]
lats = [f["geometry"]["coordinates"][1] for f in feats]
bounds = (min(lons), min(lats), max(lons), max(lats))

tmp = OUT.with_name(OUT.name + ".tmp")
tmp.unlink(missing_ok=True)
db = sqlite3.connect(tmp)
db.executescript(
    """
    CREATE TABLE metadata (name TEXT, value TEXT);
    CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
    CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
    """
)

total_tiles = 0
max_tile_bytes = 0
for z in ZOOMS:
    buckets = {}
    for f in feats:
        lon, lat = f["geometry"]["coordinates"]
        tx, ty, px, py = lonlat_to_tile(lon, lat, z)
        buckets.setdefault((tx, ty), []).append((f["properties"]["dong"], px, py))
    rows = []
    for (tx, ty), pts in buckets.items():
        values = []           # 타일 내 유니크 라벨 문자열
        vidx = {}
        fbytes = b""
        for dong, px, py in pts:
            if dong not in vidx:
                vidx[dong] = len(values)
                values.append(dong)
            fbytes += encode_feature(vidx[dong], px, py)
        tile = encode_layer(fbytes, values)
        blob = gzip.compress(tile, 6)
        max_tile_bytes = max(max_tile_bytes, len(blob))
        rows.append((z, tx, (1 << z) - 1 - ty, sqlite3.Binary(blob)))  # TMS y-flip
    db.executemany("INSERT INTO tiles VALUES (?,?,?,?)", rows)
    total_tiles += len(rows)
    print(f"  z{z}: {len(rows):,}타일")

meta = {
    "name": "dong",
    "description": "Apartment building (dong) labels extracted from OpenStreetMap",
    "format": "pbf",
    "type": "overlay",
    "version": "1",
    "minzoom": str(min(ZOOMS)),
    "maxzoom": str(max(ZOOMS)),
    "bounds": ",".join(f"{v:.5f}" for v in bounds),
    "center": f"{(bounds[0]+bounds[2])/2:.5f},{(bounds[1]+bounds[3])/2:.5f},14",
    "compression": "gzip",
    "json": json.dumps(
        {"vector_layers": [{"id": "dong", "fields": {"dong": "String"},
                            "minzoom": min(ZOOMS), "maxzoom": max(ZOOMS)}]}
    ),
}
db.executemany("INSERT INTO metadata VALUES (?,?)", meta.items())
db.commit()
db.close()
tmp.replace(OUT)
print(f"OK: {OUT.relative_to(ROOT)} (타일 {total_tiles:,}개, 최대 타일 {max_tile_bytes/1024:.1f}KB, "
      f"파일 {OUT.stat().st_size/1048576:.1f}MB)")
