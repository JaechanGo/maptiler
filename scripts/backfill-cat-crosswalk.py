#!/usr/bin/env python3
"""기존 geocode.sqlite 의 localdata·OSM 카테고리를 canonical(sangga) 로 표준화 — 재빌드 없이.

scripts/cat-crosswalk.json (canonical 크로스워크) 을 읽어 places.cat1/cat2 를 덮어쓴다.
  · localdata : 키 'cat1|subtype' → canonical cat1/cat2  (원래 cat1=식품/생활.. cat2=NULL 이었음)
  · osm       : 키 subtype       → canonical cat1/cat2  (원래 cat1/cat2=NULL)
  · sangga/facility : 손대지 않음(이미 canonical / 별도 체계).  subtype(원본 라벨)은 보존.
09-gen-geocode.py 도 같은 json 으로 빌드 시 적용 → 다음 풀빌드와 일관.

  python3 scripts/backfill-cat-crosswalk.py [--db ~/geocode-build/geocode.sqlite] [--crosswalk scripts/cat-crosswalk.json]
"""
import argparse, json, os, pathlib, sqlite3, sys, time

ROOT = pathlib.Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "geocode.sqlite"))
    ap.add_argument("--crosswalk", default=str(ROOT / "cat-crosswalk.json"))
    args = ap.parse_args()
    if not pathlib.Path(args.db).exists(): sys.exit(f"DB 없음: {args.db}")
    cw = json.load(open(args.crosswalk, encoding="utf-8"))
    t0 = time.time()
    db = sqlite3.connect(args.db)
    for p in ("journal_mode=OFF", "synchronous=OFF", "temp_store=MEMORY", "busy_timeout=60000"):
        db.execute(f"PRAGMA {p}")

    db.execute("DROP TABLE IF EXISTS _cat_ld"); db.execute("DROP TABLE IF EXISTS _cat_osm")
    db.execute("CREATE TABLE _cat_ld(k TEXT PRIMARY KEY, c1 TEXT, c2 TEXT) WITHOUT ROWID")
    db.execute("CREATE TABLE _cat_osm(st TEXT PRIMARY KEY, c1 TEXT, c2 TEXT) WITHOUT ROWID")
    db.executemany("INSERT OR IGNORE INTO _cat_ld VALUES(?,?,?)",
                   [(k, v[0], v[1] or None) for k, v in cw["localdata"].items()])
    db.executemany("INSERT OR IGNORE INTO _cat_osm VALUES(?,?,?)",
                   [(k, v[0], v[1] or None) for k, v in cw["osm"].items()])
    db.commit()
    print(f"  크로스워크 적재: localdata {len(cw['localdata'])} · osm {len(cw['osm'])}", file=sys.stderr)

    st = time.time()
    c1 = db.execute(
        "UPDATE places SET "
        "  cat1=(SELECT c1 FROM _cat_ld WHERE k=places.cat1||'|'||places.subtype), "
        "  cat2=(SELECT c2 FROM _cat_ld WHERE k=places.cat1||'|'||places.subtype) "
        "WHERE source='localdata' AND EXISTS(SELECT 1 FROM _cat_ld WHERE k=places.cat1||'|'||places.subtype)")
    db.commit()
    print(f"  localdata 표준화: {c1.rowcount:,}건 ({time.time()-st:.1f}s)", file=sys.stderr)

    st = time.time()
    c2 = db.execute(
        "UPDATE places SET "
        "  cat1=(SELECT c1 FROM _cat_osm WHERE st=places.subtype), "
        "  cat2=(SELECT c2 FROM _cat_osm WHERE st=places.subtype) "
        "WHERE source='osm' AND EXISTS(SELECT 1 FROM _cat_osm WHERE st=places.subtype)")
    db.commit()
    print(f"  osm 표준화: {c2.rowcount:,}건 ({time.time()-st:.1f}s)", file=sys.stderr)

    db.execute("DROP TABLE _cat_ld"); db.execute("DROP TABLE _cat_osm"); db.commit()

    print("=" * 56)
    print(f"표준화 완료 · {time.time()-t0:.0f}s")
    print("  출처별 cat1 종수(표준화 후 동일 체계여야):")
    for r in db.execute("SELECT source, count(DISTINCT cat1) c1, sum(cat1 IS NOT NULL AND cat1<>'') has "
                        "FROM places WHERE kind IN ('biz','facility') GROUP BY source"):
        print("   ", r)
    print("  교차 검증 — '음식' 대분류 출처 분포:")
    for r in db.execute("SELECT source,count(*) FROM places WHERE cat1='음식' GROUP BY 1"):
        print("   ", r)
    db.close()


if __name__ == "__main__":
    main()
