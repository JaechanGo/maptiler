#!/usr/bin/env python3
# OSM 원본에서 아파트 "동" 라벨을 추출 → data/dong/dong-labels.geojson.
# 이후 05-gen-dong-tiles.py 가 이 GeoJSON 을 tiles/dong.mbtiles 벡터타일로 굽는다.
#
# OpenMapTiles 스키마는 building 레이어에서 name/ref 를 버리므로(타일에 동 정보 없음),
# 원본 south-korea.osm.pbf 를 직접 파싱해 건물(name/ref)에서 "101동" 류를 뽑아
# 건물 중심점(point) GeoJSON 으로 만든다.
#
# 의존성 없음(zlib/struct/re 표준). 2-pass:
#   pass1) building way 중 "동 라벨" 후보 수집 + 필요한 노드 id 집합
#   pass2) dense 노드에서 그 id들의 좌표만 해석 → 건물 꼭짓점 평균(중심점)
import sys, zlib, struct, re, json, pathlib, collections

ROOT = pathlib.Path(__file__).resolve().parents[1]
PBF = ROOT / "data/osm/south-korea.osm.pbf"
OUT = ROOT / "data/dong/dong-labels.geojson"

# ---- protobuf 최소 디코더 -------------------------------------------------
def read_varint(b, p):
    shift = 0; result = 0
    while True:
        byte = b[p]; p += 1
        result |= (byte & 0x7f) << shift
        if not (byte & 0x80): break
        shift += 7
    return result, p

def iter_fields(b):
    p = 0; n = len(b)
    while p < n:
        tag, p = read_varint(b, p)
        field = tag >> 3; wire = tag & 7
        if wire == 0:
            val, p = read_varint(b, p); yield field, wire, val
        elif wire == 2:
            ln, p = read_varint(b, p); yield field, wire, b[p:p+ln]; p += ln
        elif wire == 1:
            yield field, wire, b[p:p+8]; p += 8
        elif wire == 5:
            yield field, wire, b[p:p+4]; p += 4
        else:
            raise ValueError("bad wire %d" % wire)

def packed_u(b):
    out = []; ap = out.append; p = 0; n = len(b)
    while p < n:
        shift = 0; result = 0
        while True:
            byte = b[p]; p += 1
            result |= (byte & 0x7f) << shift
            if not (byte & 0x80): break
            shift += 7
        ap(result)
    return out

def delta_sint(packed):
    # 패킹된 unsigned varint 리스트 → zigzag 디코드 + 누적(delta) 디코드
    out = []; ap = out.append; cum = 0
    for v in packed:
        cum += (v >> 1) ^ -(v & 1)
        ap(cum)
    return out

def parse_stringtable(b):
    st = []
    for field, wire, val in iter_fields(b):
        if field == 1 and wire == 2: st.append(val)
    return st

def osmdata_blocks(path):
    with open(path, 'rb') as f:
        while True:
            hdr = f.read(4)
            if len(hdr) < 4: break
            (hlen,) = struct.unpack('>I', hdr)
            bh = f.read(hlen)
            btype = None; dsize = 0
            for field, wire, val in iter_fields(bh):
                if field == 1 and wire == 2: btype = val.decode('ascii', 'replace')
                elif field == 3 and wire == 0: dsize = val
            blob = f.read(dsize)
            if btype != 'OSMData': continue
            raw = None; zd = None
            for field, wire, val in iter_fields(blob):
                if field == 1 and wire == 2: raw = val
                elif field == 3 and wire == 2: zd = val
            yield zlib.decompress(zd) if zd is not None else raw

# ---- 동 라벨 판정 ---------------------------------------------------------
dong_re = re.compile(r'(\d+)\s*동')
letter_dong = re.compile(r'^[가-힣A-Za-z]동$')
# 이름에 '행정동(법정동)' 표기가 든 공공·근린시설을 아파트 동으로 오인하지 않도록 제외.
# 예: '부성1동 행정복지센터'→1동, '계양1동우체국'→1동, '중3동성당'→3동 (오탐 차단)
NEG_KW = re.compile(
    r'주민센터|행정복지센터|동사무소|우체국|성당|교회|구청|시청|군청|읍사무소|면사무소'
    r'|보건소|파출소|치안센터|경찰서|소방서|도서관|복지관|문화센터|체육관|주차장')

def dong_label(tags):
    name = tags.get('name', '').strip()
    ref = tags.get('ref', '').strip()
    bv = tags.get('building', '')
    # name/ref 의 'N동' 추출 — 단, 행정/공공시설명에 든 행정동 표기는 제외
    if not (NEG_KW.search(name) or NEG_KW.search(ref)):
        m = dong_re.search(name)
        if m: return m.group(1) + '동'
        m = dong_re.search(ref)
        if m: return m.group(1) + '동'
    if bv == 'apartments':
        if ref.isdigit(): return ref + '동'
        if name.isdigit(): return name + '동'
        if letter_dong.match(name): return name
        if letter_dong.match(ref): return ref
    return None
    # → "동아백화점","안동체육관","생명과학관 동관","계양1동우체국" 등은 모두 None

def tags_from(keys, vals, st):
    t = {}
    for k, v in zip(keys, vals):
        try:
            t[st[k].decode('utf-8', 'replace')] = st[v].decode('utf-8', 'replace')
        except IndexError:
            pass
    return t

# ---- PASS A: 멀티폴리곤(관계)로 그려진 동 건물 + 멤버 way id 수집 ----------
# 건물 외곽이 type=multipolygon 관계로 매핑된 경우 building/name 태그가 관계에 붙어
# Way 스캔만으로는 누락된다. 여기서 관계를 먼저 훑어 멤버 way id 를 모은다.
print("[passA] relation 스캔...", file=sys.stderr)
rel_buildings = []      # (label, [member_way_id,...])
needed_ways = set()
for data in osmdata_blocks(PBF):
    st = None; groups = []
    for field, wire, val in iter_fields(data):
        if field == 1 and wire == 2: st = parse_stringtable(val)
        elif field == 2 and wire == 2: groups.append(val)
    if st is None: continue
    try:
        bidx = st.index(b'building')
    except ValueError:
        continue
    for g in groups:
        for field, wire, val in iter_fields(g):
            if field == 4 and wire == 2:        # Relation
                keys_b = vals_b = memids_b = types_b = None
                for ff, ww, vv in iter_fields(val):
                    if ff == 2 and ww == 2: keys_b = vv
                    elif ff == 3 and ww == 2: vals_b = vv
                    elif ff == 9 and ww == 2: memids_b = vv      # sint64 delta
                    elif ff == 10 and ww == 2: types_b = vv      # 0=node 1=way 2=relation
                keys = packed_u(keys_b) if keys_b else []
                if bidx not in keys: continue
                label = dong_label(tags_from(keys, packed_u(vals_b) if vals_b else [], st))
                if not label or not memids_b or not types_b: continue
                memids = delta_sint(packed_u(memids_b))
                mtypes = packed_u(types_b)
                way_members = [mid for mid, ty in zip(memids, mtypes) if ty == 1]
                if not way_members: continue
                rel_buildings.append((label, way_members))
                needed_ways.update(way_members)
print(f"[passA] 관계 동 건물 {len(rel_buildings):,}개, 참조 멤버 way {len(needed_ways):,}개", file=sys.stderr)

# ---- PASS B: way 동 건물(직접) + 관계 멤버 way 의 노드 refs ----------------
print("[passB] way 스캔...", file=sys.stderr)
way_buildings = []      # (label, [node_ref,...])
member_way_refs = {}    # way_id -> [node_ref,...]  (관계 멤버 way 의 형상)
needed = set()
blocks = 0
for data in osmdata_blocks(PBF):
    st = None; groups = []
    for field, wire, val in iter_fields(data):
        if field == 1 and wire == 2: st = parse_stringtable(val)
        elif field == 2 and wire == 2: groups.append(val)
    if st is None: continue
    try:
        bidx = st.index(b'building')
    except ValueError:
        bidx = -1                    # 멤버 way 는 무태그라 building 없는 블록에도 있을 수 있음 → skip 금지
    for g in groups:
        for field, wire, val in iter_fields(g):
            if field == 3 and wire == 2:        # Way
                wid = None; keys_b = vals_b = refs_b = None
                for ff, ww, vv in iter_fields(val):
                    if ff == 1 and ww == 0: wid = vv             # Way.id (plain int64)
                    elif ff == 2 and ww == 2: keys_b = vv
                    elif ff == 3 and ww == 2: vals_b = vv
                    elif ff == 8 and ww == 2: refs_b = vv
                want_member = wid in needed_ways
                keys = packed_u(keys_b) if (keys_b and bidx >= 0) else []
                is_bld = bidx >= 0 and bidx in keys
                if not want_member and not is_bld: continue
                refs = delta_sint(packed_u(refs_b)) if refs_b else []
                if not refs: continue
                if want_member:
                    member_way_refs[wid] = refs
                    needed.update(refs)
                if is_bld:
                    label = dong_label(tags_from(keys, packed_u(vals_b) if vals_b else [], st))
                    if label:
                        way_buildings.append((label, refs))
                        needed.update(refs)
    blocks += 1
    if blocks % 200 == 0:
        print(f"  ...blocks={blocks} way건물={len(way_buildings)} 멤버해석={len(member_way_refs)} 필요노드={len(needed)}", file=sys.stderr)
print(f"[passB] way 동 건물 {len(way_buildings):,}개, 멤버 way 해석 {len(member_way_refs):,}개, 필요 노드 {len(needed):,}개", file=sys.stderr)

# ---- PASS C: 필요한 노드 좌표 해석 ----------------------------------------
print("[passC] dense 노드 좌표 해석...", file=sys.stderr)
coords = {}
blocks = 0
for data in osmdata_blocks(PBF):
    gran = 100; latoff = 0; lonoff = 0; groups = []
    for field, wire, val in iter_fields(data):
        if field == 2 and wire == 2: groups.append(val)
        elif field == 17 and wire == 0: gran = val
        elif field == 19 and wire == 0: latoff = val
        elif field == 20 and wire == 0: lonoff = val
    for g in groups:
        for field, wire, val in iter_fields(g):
            if field == 2 and wire == 2:        # DenseNodes
                id_b = lat_b = lon_b = None
                for ff, ww, vv in iter_fields(val):
                    if ff == 1 and ww == 2: id_b = vv
                    elif ff == 8 and ww == 2: lat_b = vv
                    elif ff == 9 and ww == 2: lon_b = vv
                if not id_b: continue
                ids = delta_sint(packed_u(id_b))
                hits = [(i, nid) for i, nid in enumerate(ids) if nid in needed]
                if not hits: continue           # 이 블록에 필요한 노드 없음 → lat/lon 디코드 생략
                lats = delta_sint(packed_u(lat_b))
                lons = delta_sint(packed_u(lon_b))
                for i, nid in hits:
                    coords[nid] = ((lonoff + gran * lons[i]) / 1e9,
                                   (latoff + gran * lats[i]) / 1e9)
    blocks += 1
    if blocks % 200 == 0:
        print(f"  ...blocks={blocks} 해석된좌표={len(coords):,}/{len(needed):,}", file=sys.stderr)
print(f"[passC] 좌표 해석 완료 {len(coords):,}개", file=sys.stderr)

# ---- 중심점 계산 + GeoJSON ------------------------------------------------
def centroid(refs):
    # 닫힌 링은 refs[0]==refs[-1] 로 시작=끝 노드가 중복 → 중복 제거 후 꼭짓점 평균
    if len(refs) > 1 and refs[0] == refs[-1]:
        refs = refs[:-1]
    pts = [coords[r] for r in refs if r in coords]
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

feats = []
no_geom = 0
grid = collections.Counter()

def emit(label, pt):
    global no_geom
    if pt is None:
        no_geom += 1; return
    lon, lat = pt
    feats.append({"type": "Feature",
                  "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                  "properties": {"dong": label}})
    grid[(round(lon, 2), round(lat, 2))] += 1

for label, refs in way_buildings:
    emit(label, centroid(refs))
for label, way_members in rel_buildings:     # 관계: 멤버 way 들의 노드를 모아 중심점
    allrefs = []
    for w in way_members:
        allrefs.extend(member_way_refs.get(w, []))
    emit(label, centroid(allrefs))

OUT.parent.mkdir(parents=True, exist_ok=True)
with open(OUT, 'w', encoding='utf-8') as f:
    json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False)

lons = [f['geometry']['coordinates'][0] for f in feats]
lats = [f['geometry']['coordinates'][1] for f in feats]
print("=" * 60)
print(f"동 라벨 GeoJSON 생성: {OUT.relative_to(ROOT)}")
print(f"  피처(동 라벨 점): {len(feats):,}개   (지오메트리 없음으로 누락: {no_geom:,})")
if feats:
    print(f"  bbox: lon {min(lons):.4f}~{max(lons):.4f}, lat {min(lats):.4f}~{max(lats):.4f}")
    print("  밀집 상위 셀(0.01°격자) — 데모 카메라 후보 [lon, lat, 동수]:")
    for (clon, clat), cnt in grid.most_common(8):
        print(f"    [{clon}, {clat}]  {cnt}개")
