#!/usr/bin/env python3
"""기존 geocode.sqlite 의 addr 지번(jibun)을 '빠짐없이' 채우는 백필 — 재빌드 없이.

두 단계:
  1) 대표지번 + 리(里) : navi match_jibun 의 c[4]=리 를 포함해 mgt→대표지번 재구성(기존 59%).
  2) 파생            : 대표지번이 없는 건물(약 41%)은 건물관리번호로 지번 파생 →
                       bd_mgt_sn[:10]=법정동코드 → "법정동 [리]" 이름, [11:15]=본번, [15:19]=부번.
                       (산 표기는 1)의 대표지번 보유분만 정확 — 파생분은 산 접두 생략)
09-gen-geocode.py(load_jibun/add_juso) 도 동일 로직으로 수정됨 → 다음 풀빌드도 ~100% 일관.

  python3 scripts/backfill-jibun-ri.py [--db ~/geocode-build/geocode.sqlite] [--navi ~/geocode-build/staged/navi]
"""
import argparse, io, os, pathlib, sqlite3, sys, time

SIDO = ["seoul","busan","daegu","incheon","gwangju","daejeon","ulsan","sejong","gyunggi",
        "gangwon","chungbuk","chungnam","jeonbuk","jeonnam","gyeongbuk","gyeongnam","jeju"]


def parse_jibun(c):
    # match_jibun: c[3]=법정동(면), c[4]=리, c[5]=산, c[6]=본번, c[7]=부번, c[18]=건물관리번호
    san = "산 " if c[5] == "1" else ""; bu = c[7]
    ri = c[4].strip()
    dong = f"{c[3]} {ri}" if ri else c[3]
    return f"{dong} {san}{c[6]}" + (f"-{bu}" if bu and bu != "0" else ""), dong


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "geocode.sqlite"))
    ap.add_argument("--navi", default=os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "staged/navi"))
    args = ap.parse_args()
    db_path = pathlib.Path(args.db); navi = pathlib.Path(args.navi)
    if not db_path.exists(): sys.exit(f"DB 없음: {db_path}")
    t0 = time.time()
    db = sqlite3.connect(str(db_path))
    for p in ("journal_mode=OFF", "synchronous=OFF", "temp_store=MEMORY", "cache_size=-1048576", "busy_timeout=60000"):
        db.execute(f"PRAGMA {p}")

    db.execute("DROP TABLE IF EXISTS _mgt_jibun"); db.execute("DROP TABLE IF EXISTS _bcode_name")
    db.execute("CREATE TABLE _mgt_jibun(mgt TEXT PRIMARY KEY, jibun TEXT) WITHOUT ROWID")
    db.execute("CREATE TABLE _bcode_name(bcode TEXT PRIMARY KEY, name TEXT) WITHOUT ROWID")

    st = time.time(); n_map = 0
    for s in SIDO:
        p = navi / f"match_jibun_{s}.txt"
        if not p.exists():
            print(f"  (건너뜀) {p.name} 없음", file=sys.stderr); continue
        jb, nb = [], []
        for line in io.open(p, encoding="cp949", errors="replace"):
            c = line.rstrip("\n").split("|")
            if len(c) < 19: continue
            mgt = c[18]
            if not mgt: continue
            jibun, dong = parse_jibun(c)
            jb.append((mgt, jibun)); nb.append((mgt[:10], dong))
            if len(jb) >= 50000:
                db.executemany("INSERT OR IGNORE INTO _mgt_jibun VALUES(?,?)", jb)
                db.executemany("INSERT OR IGNORE INTO _bcode_name VALUES(?,?)", nb)
                n_map += len(jb); jb.clear(); nb.clear()
        if jb:
            db.executemany("INSERT OR IGNORE INTO _mgt_jibun VALUES(?,?)", jb)
            db.executemany("INSERT OR IGNORE INTO _bcode_name VALUES(?,?)", nb)
            n_map += len(jb)
    db.commit()
    nb_cnt = db.execute("SELECT count(*) FROM _bcode_name").fetchone()[0]
    print(f"  mgt→지번 {n_map:,}건 · 법정동코드→이름 {nb_cnt:,}개 적재 ({time.time()-st:.1f}s)", file=sys.stderr)

    # 1) 대표지번(리 포함)
    st = time.time()
    c1 = db.execute(
        "UPDATE places SET jibun=(SELECT jibun FROM _mgt_jibun WHERE mgt=places.bd_mgt_sn) "
        "WHERE kind='addr' AND EXISTS(SELECT 1 FROM _mgt_jibun WHERE mgt=places.bd_mgt_sn)")
    db.commit()
    print(f"  [1] 대표지번 백필: {c1.rowcount:,}건 ({time.time()-st:.1f}s)", file=sys.stderr)

    # 2) 대표지번 없는 건물 → 건물관리번호 파생(법정동코드 이름 + 본번/부번)
    st = time.time()
    c2 = db.execute(
        "UPDATE places SET jibun=("
        "  SELECT n.name || ' ' || CAST(substr(places.bd_mgt_sn,12,4) AS INTEGER) ||"
        "    CASE WHEN CAST(substr(places.bd_mgt_sn,16,4) AS INTEGER)>0"
        "         THEN '-' || CAST(substr(places.bd_mgt_sn,16,4) AS INTEGER) ELSE '' END"
        "  FROM _bcode_name n WHERE n.bcode=substr(places.bd_mgt_sn,1,10)) "
        "WHERE kind='addr' AND (jibun IS NULL OR jibun='') "
        "  AND CAST(substr(bd_mgt_sn,12,4) AS INTEGER)>0 "
        "  AND EXISTS(SELECT 1 FROM _bcode_name n WHERE n.bcode=substr(places.bd_mgt_sn,1,10))")
    db.commit()
    print(f"  [2] 파생 백필: {c2.rowcount:,}건 ({time.time()-st:.1f}s)", file=sys.stderr)

    db.execute("DROP TABLE _mgt_jibun"); db.execute("DROP TABLE _bcode_name"); db.commit()

    print("=" * 56)
    tot, hj = db.execute("SELECT count(*), sum(jibun IS NOT NULL AND jibun<>'') FROM places WHERE kind='addr'").fetchone()
    print(f"addr {tot:,}건 · jibun {hj:,}({hj*100//max(tot,1)}%) · {time.time()-t0:.0f}s")
    print("  면/리 예시:")
    for r in db.execute("SELECT sigungu,jibun FROM places WHERE kind='addr' AND emd LIKE '%면' AND jibun<>'' LIMIT 3"):
        print("   ", " ".join(str(x) for x in r))
    print("  파생분 포함 동 예시:")
    for r in db.execute("SELECT sigungu,jibun FROM places WHERE kind='addr' AND emd LIKE '%동' AND jibun<>'' LIMIT 3"):
        print("   ", " ".join(str(x) for x in r))
    miss = db.execute("SELECT count(*) FROM places WHERE kind='addr' AND (jibun IS NULL OR jibun='')").fetchone()[0]
    print(f"  남은 미채움 {miss:,}건 (법정동코드 이름맵에도 없거나 본번 0)")
    db.close()


if __name__ == "__main__":
    main()
