#!/usr/bin/env python3
"""[온라인 단계] OSM 추출본 → 역 출구 데이터 (도보 길찾기 출구 안내용) — FEAT-007/ADR-009.
data/osm/south-korea.osm.pbf 의 railway=subway_entrance/train_station_entrance 노드를
무의존 PBF 디코드(osm-from-mbtiles.py 의 MVT 디코더와 같은 스타일)로 추출하고,
최근접 역(railway=station|halt, 500m 이내)에 귀속시켜 demo/data/station-exits.json 으로 쓴다.
korea.mbtiles poi 레이어에도 출구는 있지만 출구 번호(ref)가 스키마에 없어 pbf 직접 추출이 정본.
데모 routing.js 가 도보 프로필에서 역 지점을 최적 출구로 스냅할 때 사용(차량은 미사용).
블록 사전필터(바이트 검색)로 전체 파싱을 회피 — 실측 수십 초 내 완료.
"""
import json, math, os, re, struct, sys, zlib

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PBF = os.environ.get("PBF") or os.path.join(_ROOT, "data/osm/south-korea.osm.pbf")
OUT = os.path.join(_ROOT, "demo/data/station-exits.json")

ENTRANCE_TAGS = {"subway_entrance", "train_station_entrance"}
STATION_TAGS = {"station", "halt"}
ASSOC_RADIUS_M = 500          # 출구→역 귀속 최대 거리


def varint(buf, p):
    s = 0; r = 0
    while True:
        b = buf[p]; p += 1; r |= (b & 0x7f) << s
        if not b & 0x80: return r, p
        s += 7


def zigzag(n):
    return (n >> 1) ^ -(n & 1)


def fields(buf):
    p = 0; n = len(buf)
    while p < n:
        t, p = varint(buf, p); f = t >> 3; w = t & 7
        if w == 0: v, p = varint(buf, p); yield f, w, v
        elif w == 2: ln, p = varint(buf, p); yield f, w, buf[p:p+ln]; p += ln
        elif w == 1: yield f, w, buf[p:p+8]; p += 8
        elif w == 5: yield f, w, buf[p:p+4]; p += 4
        else: raise ValueError(f"wire type {w}")


def packed_sint_cumsum(buf):
    """DenseNodes 의 delta 인코딩 packed sint64 → 누적값 리스트."""
    p = 0; out = []; last = 0
    while p < len(buf):
        x, p = varint(buf, p)
        last += zigzag(x)
        out.append(last)
    return out


def parse_block(raw, want_entrance, want_station):
    """PrimitiveBlock 에서 관심 태그 노드만 (tags, lon, lat) 로 산출."""
    st = []; groups = []; gran = 100; lat_off = 0; lon_off = 0
    for f, w, v in fields(raw):
        if f == 1:
            for f2, w2, v2 in fields(v):
                if f2 == 1: st.append(v2)
        elif f == 2: groups.append(v)
        elif f == 17: gran = v
        elif f == 19: lat_off = v
        elif f == 20: lon_off = v
    for g in groups:
        dense = None
        for f, w, v in fields(g):
            if f == 2: dense = v         # DenseNodes 만 — 출구/역은 전부 노드
        if dense is None: continue
        lats = lons = kv = None
        for f, w, v in fields(dense):
            if f == 8: lats = v
            elif f == 9: lons = v
            elif f == 10: kv = v
        if lats is None or lons is None or kv is None: continue
        LA = packed_sint_cumsum(lats); LO = packed_sint_cumsum(lons)
        kvs = []; p = 0
        while p < len(kv):
            x, p = varint(kv, p); kvs.append(x)
        i = 0; node = 0; tags = {}
        while i < len(kvs):
            if kvs[i] == 0:
                rw = tags.get("railway")
                if (want_entrance and rw in ENTRANCE_TAGS) or (want_station and rw in STATION_TAGS):
                    lat = (lat_off + gran * LA[node]) * 1e-9
                    lon = (lon_off + gran * LO[node]) * 1e-9
                    yield tags, lon, lat
                node += 1; tags = {}; i += 1
            else:
                k = st[kvs[i]].decode("utf-8", "replace")
                v = st[kvs[i + 1]].decode("utf-8", "replace")
                if k in ("railway", "ref", "name", "name:ko", "station", "description", "description:ko"): tags[k] = v
                tags.setdefault("_any", None)
                i += 2


def scan(pbf):
    entrances = []; stations = []
    with open(pbf, "rb") as f:
        while True:
            hdr = f.read(4)
            if len(hdr) < 4: break
            (hlen,) = struct.unpack(">I", hdr)
            bh = f.read(hlen); btype = None; dsize = 0
            for ff, w, v in fields(bh):
                if ff == 1: btype = v.decode()
                elif ff == 3: dsize = v
            blob = f.read(dsize)
            if btype != "OSMData": continue
            raw = None
            for ff, w, v in fields(blob):
                if ff == 1: raw = v
                elif ff == 3: raw = zlib.decompress(v)
            if raw is None: continue
            # 사전필터: 관심 문자열이 stringtable 에 없으면 블록 전체 스킵(성능 핵심)
            we = b"subway_entrance" in raw or b"train_station_entrance" in raw
            ws = b"railway" in raw and b"station" in raw
            if not (we or ws): continue
            for tags, lon, lat in parse_block(raw, we, ws):
                rw = tags["railway"]
                nm = tags.get("name:ko") or tags.get("name") or ""
                if rw in ENTRANCE_TAGS:
                    desc = tags.get("description:ko") or tags.get("description") or ""
                    entrances.append((lon, lat, tags.get("ref") or "", nm, desc))
                else:
                    stations.append((lon, lat, nm))
    return entrances, stations


def haversine_m(lon1, lat1, lon2, lat2):
    r = 6371000.0
    dlat = math.radians(lat2 - lat1); dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def station_display(name):
    """geocode(osm-from-mbtiles) 와 동일 관례 — 역명에 '역' 접미 보장."""
    return name + "역" if (name and not name.endswith("역")) else name


def main():
    if not os.path.getsize(PBF):
        sys.exit(f"오류: OSM 추출본 없음: {PBF} — 01-download-data.sh 먼저 실행")
    entrances, stations = scan(PBF)
    print(f"추출: 출구 {len(entrances):,}개, 역 {len(stations):,}개")
    # 출구→최근접 역 귀속 (그리드 인덱스 — 전수 곱 회피)
    grid = {}
    for lon, lat, nm in stations:
        if not nm: continue
        grid.setdefault((int(lon * 100), int(lat * 100)), []).append((lon, lat, nm))
    out = []
    orphan = 0; by_name = 0
    name_re = re.compile(r"([가-힣A-Za-z0-9]+역)")
    for lon, lat, ref, nm, desc in entrances:
        gx, gy = int(lon * 100), int(lat * 100)
        best = None; best_d = ASSOC_RADIUS_M
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for slon, slat, snm in grid.get((gx + dx, gy + dy), ()):
                    d = haversine_m(lon, lat, slon, slat)
                    if d < best_d: best_d, best = d, (slon, slat, snm)
        if best is not None:
            slon, slat, snm = best
            out.append({"lon": round(lon, 6), "lat": round(lat, 6), "ref": ref, "name": nm,
                        "station": station_display(snm), "slon": round(slon, 6), "slat": round(slat, 6)})
            continue
        # 역이 노드가 아닌 폴리곤/릴레이션으로만 매핑된 경우(예: 부천시청역) — 출구 자신의
        # name/description("부천시청역 4번 출구")에서 역명을 파싱해 귀속(역 좌표는 없음 → 이름 매칭 전용)
        m = name_re.search(nm) or name_re.search(desc)
        if m:
            by_name += 1
            out.append({"lon": round(lon, 6), "lat": round(lat, 6), "ref": ref, "name": nm,
                        "station": m.group(1)})
        else:
            orphan += 1
    with_ref = sum(1 for e in out if e["ref"])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"source": os.path.basename(PBF), "count": len(out), "exits": out},
                  f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    kb = os.path.getsize(OUT) // 1024
    print(f"OK: {OUT} — 귀속 {len(out):,}개(ref 보유 {with_ref:,}, 이름귀속 {by_name}), 미귀속 제외 {orphan}, {kb}KB")
    if not out:
        sys.exit("오류: 출구 0건 — pbf/태깅 확인 필요")


if __name__ == "__main__":
    main()
