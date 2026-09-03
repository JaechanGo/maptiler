#!/usr/bin/env python3
"""행정구역(시도·시군구·읍면동) 벡터타일 — 국가 원천 → tiles/admin.mbtiles  (ADR-011 / FEAT-009)

왜: 지도의 행정 라벨·경계를 OSM 지명 노드에서 가져오면 행정개편(2026-08 전남광주통합특별시)이 OSM 기여자
    손에 달린다(실측 2026-09-03: 도시 노드만 '광주'로, 광역 노드는 '전라남도' 그대로). 지오코딩은 이미
    법정동코드(신구대응)를 진실원으로 쓰므로(ADR-010) 지도도 같은 원천으로 맞춘다 — 이름·경계·검색이 한 번에 바뀐다.

원천(둘 다 빌드 스튜디오가 매번 수집):
  · 법정동 경계 SHP(읍면동, 10자리 법정동코드 EMD_CD) — sources/boundary/legal
  · 법정동코드(신구대응) CSV — staged/lawd_code_v2/LSCT_LAWDCD.csv (SIDO_NM·SGG_NM·UMD_NM, DEL_DT 로 현행 판별)
방법(외부 의존 = GDAL ogr2ogr + tippecanoe, 둘 다 setup-build-host.sh 의 도커 래퍼로 폴백 가능):
  1) SHP → GeoJSON 한 파일(WGS84, 단순화)      2) SQLite 방언 ST_Union 으로 코드 앞 2/5/8자리 병합 + ST_PointOnSurface 라벨점
  3) 법정동코드 현행 행에서 이름 부착           4) tippecanoe → admin.mbtiles (레이어 sido/sigungu/emd + *_label)
  5) mbtiles metadata 에 cuvia_sido_names 를 남겨 QC 가 법정동코드 현행 시도 집합과 대조한다.
  ★ shapely 를 쓰지 않는 이유: EOL 빌드호스트(CentOS 7, glibc 2.17)에서 pip 휠 해석이 깨져 소스 빌드로 떨어진다(2026-09-03 실측).
    ST_Union 은 GDAL 이미지(alpine-normal)의 spatialite 가 처리한다 — alpine-small 은 GEOS/spatialite 가 없어 실패.

사용: python3 scripts/06b-gen-admin-tiles.py --shp $BUILD_HOME/sources/boundary/legal [--lawd CSV] [--out tiles/admin.mbtiles]
"""
import argparse, csv, glob, json, os, pathlib, re, sqlite3, subprocess, sys, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from _common.region import _lawd_csv_path   # noqa: E402  법정동코드 CSV 기본 경로(BUILD_HOME 기준)

LAYERS = {   # 레이어명: (코드 자릿수, 폴리곤 minzoom, maxzoom, 라벨 minzoom)
    "sido":    (2, 3, 14, 3),
    "sigungu": (5, 6, 14, 7),
    "emd":     (8, 10, 14, 11),
}
# 시도 약칭 — 저줌 라벨용(정식명은 name). 특수형은 표에, 나머지는 접미 제거.
_ABBR = {"충청북도": "충북", "충청남도": "충남", "경상북도": "경북", "경상남도": "경남",
         "전라북도": "전북", "전라남도": "전남", "전북특별자치도": "전북", "강원특별자치도": "강원",
         "제주특별자치도": "제주", "세종특별자치시": "세종", "전남광주통합특별시": "전남광주"}


def short_sido(name):
    if name in _ABBR:
        return _ABBR[name]
    for suf in ("통합특별시", "특별자치시", "특별자치도", "특별시", "광역시", "도"):
        if name.endswith(suf) and len(name) > len(suf):
            return name[:-len(suf)]
    return name


def load_lawd_names(path):
    """현행(DEL_DT 빈) 법정동코드 → {2자리: 시도명}, {5자리: 시군구명('부천시 원미구' 형식)}, {8자리: 읍면동명}."""
    sido, sgg, emd = {}, {}, {}
    with open(path, encoding="cp949", newline="") as f:
        for r in csv.DictReader(f):
            code = (r.get("LAWD_CD") or "").strip()
            if len(code) != 10 or (r.get("DEL_DT") or "").strip():
                continue
            s, g, u = (r.get("SIDO_NM") or "").strip(), (r.get("SGG_NM") or "").strip(), (r.get("UMD_NM") or "").strip()
            if code.endswith("00000000") and s:
                sido[code[:2]] = s
            elif code.endswith("00000") and g:
                sgg[code[:5]] = g
            elif code.endswith("00") and u:
                emd[code[:8]] = u
            if s:
                sido.setdefault(code[:2], s)   # 시도 행이 없는 시도는 하위 행의 SIDO_NM 으로 보강
    return sido, sgg, emd


def run(cmd, env=None):
    r = subprocess.run(cmd, env=env, text=True, capture_output=True)
    if r.returncode != 0:
        sys.exit(f"오류: {' '.join(cmd[:3])} … 실패(rc={r.returncode})\n{(r.stderr or r.stdout)[-1500:]}")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shp", required=True, help="법정동 경계 SHP 디렉토리(읍면동, EMD_CD/EMD_NM)")
    ap.add_argument("--srs", default="EPSG:5186")
    ap.add_argument("--code-field", default="EMD_CD")
    ap.add_argument("--encoding", default="CP949")
    ap.add_argument("--simplify", type=float, default=0.0002, help="ogr2ogr 단순화 허용오차(도) — 저줌 경계용")
    ap.add_argument("--lawd", default=None, help="법정동코드 CSV(기본 BUILD_HOME/staged/lawd_code_v2/LSCT_LAWDCD.csv)")
    ap.add_argument("--out", default=str(ROOT / "tiles/admin.mbtiles"))
    ap.add_argument("--tmp", default=None, help="임시 디렉토리(도커 래퍼가 볼 수 있는 경로: BUILD_HOME 아래 또는 /tmp)")
    a = ap.parse_args()
    t0 = time.time()

    lawd = a.lawd or _lawd_csv_path()
    if not os.path.isfile(lawd):
        sys.exit(f"오류: 법정동코드 CSV 없음: {lawd} (수집 항목 lawd_code_v2)")
    sido_nm, sgg_nm, emd_nm = load_lawd_names(lawd)
    names = {"sido": sido_nm, "sigungu": sgg_nm, "emd": emd_nm}
    print(f"[1/4] 법정동코드 현행 — 시도 {len(sido_nm)} · 시군구 {len(sgg_nm)} · 읍면동 {len(emd_nm)}  ← {lawd}", file=sys.stderr)

    shps = sorted(glob.glob(os.path.join(a.shp, "**", "*.shp"), recursive=True))
    if not shps:
        sys.exit(f"오류: SHP 없음: {a.shp}")
    tmp_root = a.tmp or os.path.join(os.environ.get("BUILD_HOME") or tempfile.gettempdir(), "tmp")
    os.makedirs(tmp_root, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="admin-tiles-", dir=tmp_root)
    env = dict(os.environ, SHAPE_ENCODING=a.encoding)
    try:
        # ── 1) SHP 들 → GeoJSON 한 파일(emd.geojson), WGS84 ──
        #   GPKG(SQLite) 중간파일은 도커 래퍼의 바인드 마운트(CentOS 7 커널) 위에서 "disk I/O error" 로 죽는다
        #   ([실측 2026-09-03]). GeoJSON 은 순수 파일이라 안전하고, SQLite 방언은 GeoJSON 소스도 메모리 DB 로 받는다.
        parts = []
        for i, shp in enumerate(shps):
            part = os.path.join(tmpdir, f"part{i}.geojson")
            cmd = ["ogr2ogr", "-f", "GeoJSON", part, shp, "-t_srs", "EPSG:4326", "-s_srs", a.srs, "-nlt", "PROMOTE_TO_MULTI",
                   "-select", a.code_field]
            if a.simplify:
                cmd += ["-simplify", str(a.simplify)]
            run(cmd, env)
            parts.append(part)
        gpkg = os.path.join(tmpdir, "emd_src.geojson")   # SQL 의 FROM emd_src 는 파일명 stem(출력 emd.geojson 과 달라야 함)
        feats = []; dropped = {}
        for part in parts:
            for ft in json.load(open(part, encoding="utf-8")).get("features", []):
                code = str(ft["properties"].get(a.code_field) or "").strip()
                # [실측 2026-09-03] VWorld 202608 법정동 경계에 **필지 레코드 306건**(EMD_CD='??1-84'·'1140-4구', EMD_NM=19자리 PNU)이
                # 읍면동 레이어에 섞여 있다(강원 303·경남 1·전북 2). 코드 형식(8자리 숫자)으로 걸러 경계·병합·라벨을 오염시키지 않는다.
                if not re.fullmatch(r"\d{8}", code):
                    dropped[os.path.basename(part)] = dropped.get(os.path.basename(part), 0) + 1
                    continue
                feats.append(ft)
            os.unlink(part)
        json.dump({"type": "FeatureCollection", "features": feats}, open(gpkg, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"      읍면동 피처 {len(feats):,}" + (f" · ⚠ 원천 오염 제외 {sum(dropped.values())} (코드 형식 불량): {dropped}" if dropped else ""), file=sys.stderr)
        # ── 2) 병합(ST_Union) + 라벨점 — SQLite 방언(spatialite) ──
        outs = {}
        for layer, (nd, zmin, zmax, lzmin) in LAYERS.items():
            poly = os.path.join(tmpdir, f"{layer}.geojson"); pt = os.path.join(tmpdir, f"{layer}_label.geojson")
            sql = (f"SELECT substr(\"{a.code_field}\",1,{nd}) AS code, ST_Union(geometry) AS geometry "
                   f"FROM emd_src GROUP BY substr(\"{a.code_field}\",1,{nd})")
            run(["ogr2ogr", "-f", "GeoJSON", poly, gpkg, "-dialect", "SQLite", "-sql", sql, "-nlt", "PROMOTE_TO_MULTI"], env)
            sql_pt = (f"SELECT substr(\"{a.code_field}\",1,{nd}) AS code, ST_PointOnSurface(ST_Union(geometry)) AS geometry "
                      f"FROM emd_src GROUP BY substr(\"{a.code_field}\",1,{nd})")
            run(["ogr2ogr", "-f", "GeoJSON", pt, gpkg, "-dialect", "SQLite", "-sql", sql_pt], env)
            outs[layer] = (poly, pt)
        print(f"[2/4] 병합 완료 — {time.time()-t0:.0f}s", file=sys.stderr)

        # ── 3) 이름 부착(법정동코드 현행) ──
        counts = {}; missing = {}
        for layer, (poly, pt) in outs.items():
            nm = names[layer]; n_named = 0
            for path, is_pt in ((poly, False), (pt, True)):
                fc = json.load(open(path, encoding="utf-8"))
                keep = []
                for ft in fc.get("features", []):
                    code = str(ft["properties"].get("code") or "").strip()
                    name = nm.get(code)
                    if not name:
                        missing.setdefault(layer, []).append(code)
                        if is_pt:
                            continue          # 이름 없는 라벨은 내지 않는다(경계는 유지)
                    props = {"code": code, "name": name or ""}
                    if layer == "sido":
                        props["name_short"] = short_sido(name) if name else ""
                    if layer == "sigungu":
                        props["sido"] = sido_nm.get(code[:2], "")
                    ft["properties"] = props
                    keep.append(ft)
                    if is_pt: n_named += 1
                fc["features"] = keep
                json.dump(fc, open(path, "w", encoding="utf-8"), ensure_ascii=False)
            counts[layer] = n_named
        for layer, codes in missing.items():
            codes = sorted(set(codes))
            print(f"  ⚠ {layer}: 법정동코드에 현행 이름 없는 코드 {len(codes)}개 — 라벨 생략: {codes[:8]}", file=sys.stderr)
        print(f"[3/4] 라벨 — 시도 {counts['sido']} · 시군구 {counts['sigungu']} · 읍면동 {counts['emd']}", file=sys.stderr)

        # ── 4) tippecanoe ──
        out = pathlib.Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = os.path.join(tmpdir, "admin.mbtiles")
        cmd = ["tippecanoe", "-o", tmp_out, "--force", "--detect-shared-borders", "--no-tile-size-limit",
               "--simplification=4", "--no-feature-limit", "-n", "cuvia-admin",
               "-N", "행정구역(법정동코드 원천) — 시도·시군구·읍면동 경계와 라벨"]
        for layer, (nd, zmin, zmax, lzmin) in LAYERS.items():
            poly, pt = outs[layer]
            cmd += ["-L", json.dumps({"file": poly, "layer": layer, "minzoom": zmin, "maxzoom": zmax}),
                    "-L", json.dumps({"file": pt, "layer": f"{layer}_label", "minzoom": lzmin, "maxzoom": zmax})]
        run(cmd)
        db = sqlite3.connect(tmp_out)
        sido_names = sorted(v for v in sido_nm.values())
        db.executemany("INSERT OR REPLACE INTO metadata(name, value) VALUES(?,?)", [
            ("cuvia_sido_count", str(counts["sido"])), ("cuvia_sido_names", json.dumps(sido_names, ensure_ascii=False)),
            ("cuvia_sigungu_count", str(counts["sigungu"])), ("cuvia_emd_count", str(counts["emd"])),
            ("cuvia_lawd_source", os.path.basename(lawd)), ("cuvia_built_at", time.strftime("%Y-%m-%d %H:%M"))])
        db.commit(); db.close()
        os.replace(tmp_out, out)
        print(f"[4/4] OK: {out} ({out.stat().st_size/1048576:.1f}MB) · 시도 라벨 {counts['sido']}/{len(sido_nm)} · {time.time()-t0:.0f}s", file=sys.stderr)
    finally:
        for f in glob.glob(os.path.join(tmpdir, "*")):
            try: os.unlink(f)
            except OSError: pass
        try: os.rmdir(tmpdir)
        except OSError: pass


if __name__ == "__main__":
    main()
