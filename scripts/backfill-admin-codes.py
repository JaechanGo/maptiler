#!/usr/bin/env python3
"""기존 geocode.sqlite 에 행정코드(bcode·hcode) 백필 — 5GB 전체 재빌드 없이 컬럼만 주입.

- bcode(법정동코드) : 이미 bd_mgt_sn(건물관리번호) 앞 10자리 == 법정동코드 → substr 로 즉시.
- hcode(행정동코드) : navi DB match_build_*.txt 의 c[13]. mgt(c[10])→hcode 매핑표를 만들어 조인.

둘 다 navi 원본에 들어있으므로 외부 행안부 매핑 파일은 불필요.
멱등: 컬럼/값이 이미 있으면 건너뛴다. 09-gen-geocode.py 가 다음 풀빌드부터 같은 컬럼을 직접 채운다.

  python3 scripts/backfill-admin-codes.py [--db ~/geocode-build/geocode.sqlite] [--navi ~/geocode-build/staged/navi]
"""
import argparse, io, os, pathlib, sqlite3, sys, time

SIDO = ["seoul","busan","daegu","incheon","gwangju","daejeon","ulsan","sejong","gyunggi",
        "gangwon","chungbuk","chungnam","jeonbuk","jeonnam","gyeongbuk","gyeongnam","jeju"]


def has_col(db, table, col):
    return any(r[1] == col for r in db.execute(f"PRAGMA table_info({table})"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "geocode.sqlite"))
    ap.add_argument("--navi", default=os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "staged/navi"))
    ap.add_argument("--vacuum", action="store_true", help="끝나고 VACUUM(파일축소, 5GB 재작성이라 느림)")
    args = ap.parse_args()
    db_path = pathlib.Path(args.db); navi = pathlib.Path(args.navi)
    if not db_path.exists(): sys.exit(f"DB 없음: {db_path}")
    t0 = time.time()
    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA journal_mode=OFF"); db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA temp_store=MEMORY"); db.execute("PRAGMA cache_size=-1048576")
    db.execute("PRAGMA busy_timeout=60000")

    # 1) 컬럼 추가(멱등)
    for col in ("bcode", "hcode"):
        if not has_col(db, "places", col):
            db.execute(f"ALTER TABLE places ADD COLUMN {col} TEXT")
            print(f"  + places.{col} 컬럼 추가", file=sys.stderr)

    # 2) bcode = bd_mgt_sn 앞 10자리 (addr 전건, 추가 소스 0)
    st = time.time()
    cur = db.execute("UPDATE places SET bcode=substr(bd_mgt_sn,1,10) "
                     "WHERE kind='addr' AND bd_mgt_sn IS NOT NULL AND length(bd_mgt_sn)>=10 "
                     "AND (bcode IS NULL OR bcode='')")
    db.commit()
    print(f"  bcode 백필: {cur.rowcount:,}건 ({time.time()-st:.1f}s)", file=sys.stderr)

    # 3) hcode = navi c[13]. mgt→hcode 매핑표(_mgt_hcode) 구축 후 조인 업데이트.
    st = time.time()
    db.execute("DROP TABLE IF EXISTS _mgt_hcode")
    db.execute("CREATE TABLE _mgt_hcode(mgt TEXT PRIMARY KEY, hcode TEXT) WITHOUT ROWID")
    n_map = 0
    for s in SIDO:
        p = navi / f"match_build_{s}.txt"
        if not p.exists():
            print(f"  (건너뜀) {p.name} 없음", file=sys.stderr); continue
        buf = []
        for line in io.open(p, encoding="cp949", errors="replace"):
            c = line.rstrip("\n").split("|")
            if len(c) < 14: continue
            mgt = c[10]; hc = c[13]
            if not mgt or not hc: continue
            buf.append((mgt, hc))
            if len(buf) >= 50000:
                db.executemany("INSERT OR IGNORE INTO _mgt_hcode VALUES(?,?)", buf); n_map += len(buf); buf.clear()
        if buf:
            db.executemany("INSERT OR IGNORE INTO _mgt_hcode VALUES(?,?)", buf); n_map += len(buf)
    db.commit()
    print(f"  mgt→hcode 매핑 {n_map:,}건 적재 ({time.time()-st:.1f}s)", file=sys.stderr)

    st = time.time()
    cur = db.execute("UPDATE places SET hcode=(SELECT hcode FROM _mgt_hcode WHERE mgt=places.bd_mgt_sn) "
                     "WHERE kind='addr' AND (hcode IS NULL OR hcode='')")
    db.commit()
    print(f"  hcode 백필: {cur.rowcount:,}건 ({time.time()-st:.1f}s)", file=sys.stderr)
    db.execute("DROP TABLE _mgt_hcode"); db.commit()
    if args.vacuum:
        st = time.time(); db.execute("VACUUM"); db.commit()
        print(f"  VACUUM 완료 ({time.time()-st:.0f}s)", file=sys.stderr)

    # 4) 검증 리포트
    tot, hb, hh = db.execute(
        "SELECT count(*), sum(bcode IS NOT NULL AND bcode<>''), sum(hcode IS NOT NULL AND hcode<>'') "
        "FROM places WHERE kind='addr'").fetchone()
    print("=" * 56)
    print(f"addr {tot:,}건 · bcode {hb:,}({hb*100//max(tot,1)}%) · hcode {hh:,}({hh*100//max(tot,1)}%) · {time.time()-t0:.0f}s")
    for r in db.execute("SELECT sido,sigungu,emd,haeng_dong,bcode,hcode FROM places WHERE kind='addr' AND hcode<>'' LIMIT 3"):
        print("  예:", " ".join(str(x) for x in r))
    db.close()


if __name__ == "__main__":
    main()
