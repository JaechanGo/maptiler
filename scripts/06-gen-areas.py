#!/usr/bin/env python3
"""행정경계 폴리곤 → geocode.sqlite 의 areas/area_rtree 적재 — 역지오코딩 point-in-polygon('이 점이 OO동')용.

공식 경계(SHP/GeoJSON)를 받아 areas(name,type,code,rings) + area_rtree 로 넣는다. WGS84(4326)로 변환(SHP는 ogr2ogr).
geocode-api.py reverse() 가 이미 area_rtree bbox + point_in_ring 으로 판정 → 데이터만 넣으면 동작.

  # 법정동(읍면동) — VWorld 「구역의 도형」 LSMD_ADM_SECT_UMD_*, 시도별 다중 SHP → 폴더 지정:
  python3 scripts/06-gen-areas.py --shp ~/geocode-build/sources/boundary/legal \
      --srs EPSG:5186 --name-field EMD_NM --code-field EMD_CD --type legal-dong
  # 행정동 — 국토부 센서스경계 BND_ADM_DONG_PG(단일 SHP):
  python3 scripts/06-gen-areas.py --shp ~/geocode-build/sources/boundary/admin/BND_ADM_DONG_PG.shp \
      --srs EPSG:5186 --name-field ADM_NM --code-field ADM_CD --type admin-dong
  # GeoJSON(이미 4326)이면 --geojson, --srs 불필요.

--shp 는 파일 또는 폴더(폴더면 내부 *.shp 모두 한 type 으로 합침). --type 별로 기존분을 한 번만 지우고 다시 넣음(멱등).
필드명은 소스마다 다르니 `ogrinfo -so <shp> <레이어>` 로 확인할 것.
"""
import argparse, glob, json, os, pathlib, sqlite3, subprocess, sys, tempfile, time


def ensure_schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS areas(id INTEGER PRIMARY KEY, name TEXT, type TEXT, rings TEXT)")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS area_rtree USING rtree(id,minlon,maxlon,minlat,maxlat)")
    if "code" not in {r[1] for r in db.execute("PRAGMA table_info(areas)")}:
        db.execute("ALTER TABLE areas ADD COLUMN code TEXT")   # 법정동/행정동 코드(b_code/h_code 대응)


def shp_to_geojson(shp, srs, simplify, encoding):
    """SHP → 4326 GeoJSON(tmp 경로)."""
    fd, out = tempfile.mkstemp(suffix=".geojson"); os.close(fd); os.unlink(out)   # 경로만 확보(ogr2ogr 가 직접 생성)
    cmd = ["ogr2ogr", "-f", "GeoJSON", "-t_srs", "EPSG:4326"]
    if srs: cmd += ["-s_srs", srs]
    if simplify: cmd += ["-simplify", str(simplify)]
    cmd += [out, shp]
    subprocess.run(cmd, check=True, env=dict(os.environ, SHAPE_ENCODING=encoding))  # 한글 속성(CP949) 안전
    return out


def geom_rings(geom):
    """Polygon/MultiPolygon → 외곽 링 배열 [[ [lon,lat],... ], ...]. (구멍/내부링은 생략)"""
    if not geom: return []
    t = geom["type"]; cs = geom["coordinates"]; out = []
    if t == "Polygon":
        out.append([[round(x, 6), round(y, 6)] for x, y in cs[0]])
    elif t == "MultiPolygon":
        for poly in cs:
            out.append([[round(x, 6), round(y, 6)] for x, y in poly[0]])
    return out


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--shp", help="SHP 파일 또는 폴더(폴더면 내부 *.shp 모두 합침)")
    g.add_argument("--geojson")
    ap.add_argument("--db", default=os.path.expanduser("~/geocode-build/geocode.sqlite"))
    ap.add_argument("--type", required=True, help="legal-dong | admin-dong | sigungu ...")
    ap.add_argument("--name-field", required=True)
    ap.add_argument("--code-field", default=None)
    ap.add_argument("--srs", default=None, help="SHP 원좌표계(예: EPSG:5186, EPSG:5179). GeoJSON 이면 불필요")
    ap.add_argument("--encoding", default="CP949", help="SHP 속성 인코딩(기본 CP949)")
    ap.add_argument("--simplify", type=float, default=0.0001, help="ogr2ogr 단순화 허용오차(도). 0 이면 끔")
    args = ap.parse_args()
    if not pathlib.Path(args.db).exists(): sys.exit(f"DB 없음: {args.db}")

    # 소스 목록: geojson 1개 / shp 파일 1개 / shp 폴더 → 내부 *.shp 전부
    if args.geojson:
        sources = [(args.geojson, False)]
    elif os.path.isdir(args.shp):
        sources = [(p, True) for p in sorted(glob.glob(os.path.join(args.shp, "**", "*.shp"), recursive=True))]
        if not sources: sys.exit(f"폴더에 .shp 없음: {args.shp}")
    else:
        sources = [(args.shp, True)]

    t0 = time.time()
    db = sqlite3.connect(args.db)
    for p in ("journal_mode=OFF", "synchronous=OFF", "busy_timeout=60000"):
        db.execute(f"PRAGMA {p}")
    ensure_schema(db)
    db.execute("DELETE FROM area_rtree WHERE id IN (SELECT id FROM areas WHERE type=?)", (args.type,))
    db.execute("DELETE FROM areas WHERE type=?", (args.type,))   # 멱등: type 기존분 한 번만 제거
    nid = (db.execute("SELECT COALESCE(MAX(id),0) FROM areas").fetchone()[0]) + 1

    n = skip = 0
    for path, is_shp in sources:
        gj_path = shp_to_geojson(path, args.srs, args.simplify, args.encoding) if is_shp else path
        feats = json.load(open(gj_path, encoding="utf-8")).get("features", [])
        ab, rb = [], []
        for f in feats:
            props = f.get("properties") or {}
            name = (props.get(args.name_field) or "").strip()
            code = (str(props.get(args.code_field)).strip()
                    if args.code_field and props.get(args.code_field) is not None else None)
            rings = geom_rings(f.get("geometry"))
            if not name or not rings: skip += 1; continue
            xs = [pt[0] for r in rings for pt in r]; ys = [pt[1] for r in rings for pt in r]
            ab.append((nid, name, args.type, code, json.dumps(rings, ensure_ascii=False)))
            rb.append((nid, min(xs), max(xs), min(ys), max(ys))); nid += 1; n += 1
        db.executemany("INSERT INTO areas(id,name,type,code,rings) VALUES(?,?,?,?,?)", ab)
        db.executemany("INSERT INTO area_rtree VALUES(?,?,?,?,?)", rb)
        if is_shp:
            try: os.unlink(gj_path)
            except OSError: pass
        print(f"  + {os.path.basename(path)}: {len(ab):,}", file=sys.stderr)
    db.commit()
    tot = dict(db.execute("SELECT type,count(*) FROM areas GROUP BY type").fetchall())
    db.close()
    print("=" * 56)
    print(f"적재 {n:,}건(type={args.type}, 소스 {len(sources)}) · 스킵 {skip} · {time.time()-t0:.0f}s · areas {tot}")


if __name__ == "__main__":
    main()
