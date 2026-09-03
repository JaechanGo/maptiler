#!/usr/bin/env python3
"""geocode.sqlite(09-gen-geocode.py 산출) → PostGIS address + poi 적재 (Phase 1 데이터 / Phase 4 POI).

핵심 재사용: navi DB·OSM·상가·인허가·생활편의의 파싱·좌표변환(EPSG:5179/5174→4326)·시설 지오코딩·
dedup(is_primary)이 이미 geocode.sqlite `places`(lon/lat=4326)에 다 들어있다. 원천을 재파싱하지 말고
이 산출물을 그대로 PostGIS 로 옮긴다 → 기존 품질과 100% parity. (지오코더 질의 전환은 Phase 5.)

  places(kind,name,subtype,sido,sigungu,emd,ri,road,road_norm,main_no,sub_no,bld,postal,
         haeng_dong,bd_mgt_sn,bcode,hcode,phone,opened,jibun,cat1,cat2,source,is_primary,lon,lat)
    → address (전체)          : 검색/표시/역지오코딩 원천
    → poi     (kind in biz/facility, 좌표有) : martin POI 레이어(기존 poi.mbtiles 대체)

연결은 libpq 환경변수(PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSWORD). 기본 cuvia/cuvia@localhost:5433(맵 전용 컨테이너 호스트포트).
  PGPASSWORD=... scripts/postgis/load_geocode.py [--db ~/geocode-build/geocode.sqlite]

[T049] 모드 2종:
  --mode swap (기본)  : 서비스 무중단 테이블 스왑. 기존 테이블을 건드리지 않고 <t>_new 에
                        적재·인덱싱·검증한 뒤 RENAME 한 순간만 잠근다. 실패해도 서비스 무손상.
                        인덱스는 하드코딩이 아니라 **살아있는 pg_indexes/pg_statistic_ext 정의**를
                        읽어 재생성한다(수동 추가 인덱스 5종 소실 사고의 재발 차단).
      --phase address|poi   스왑은 한 번에 한 테이블만(디스크 피크 관리, address 먼저).
      --dry-run             적재·인덱스·검증까지만 하고 스왑하지 않는다(<t>_new 잔류).
      --finalize            스왑 후 검증을 통과한 뒤에만: (선택 --rollback-drill 로 롤백 실증)
                            시퀀스 소유권 이전 → <t>_old DROP → 인덱스/제약/통계 정식명 환원.
      --limit N             (스모크 전용) 앞 N 행만 — 강제로 dry-run 이 된다.
  --mode truncate     : 종전 경로 그대로(TRUNCATE 후 재적재). 회귀 대비로 보존 — 운영에서 쓰지 마라.
"""
import argparse, csv, os, re, subprocess, sys, tempfile, time

COLS = ["kind","name","subtype","sido","sigungu","emd","ri","road","road_norm","main_no","sub_no",
        "bld","postal","haeng_dong","bd_mgt_sn","bcode","hcode","phone","opened","jibun",
        "cat1","cat2","source","is_primary","lon","lat"]


def export_csv(db_path, csv_path):
    import sqlite3
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.execute(f"SELECT {','.join(COLS)} FROM places")
    n = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        while True:
            rows = cur.fetchmany(50000)
            if not rows:
                break
            w.writerows(rows)
            n += len(rows)
    con.close()
    return n


# 모든 컬럼 TEXT 스테이징 → INSERT 에서 캐스팅(빈칸=NULL). lon/lat 있으면 geom 생성.
# 무손실 적재 가속(load_parcel/building 과 동일 전략) — 결과 불변, 속도만 단축:
#  · synchronous_commit=off: 적재 세션 커밋 fsync 대기 제거(벌크 재적재라 안전).
#  · 대량 INSERT(1,600만+) 전 2차 인덱스 DROP → 적재 후 일괄 재생성. 살아있는 trgm GIN·GiST 의
#    행단위 증분이 최대 병목 — 벌크빌드가 훨씬 빠르고 인덱스도 덜 부푼다. PK(bigserial 순차)는 저렴해 유지.
#  · ※스키마(10-base / 11-address-search / 30-poi)와 동기 유지 — 인덱스 추가/변경 시 아래 DROP/CREATE 목록도 갱신.
SQL = r"""
\set ON_ERROR_STOP on
SET synchronous_commit = off;

DROP TABLE IF EXISTS _stg_places;
CREATE UNLOGGED TABLE _stg_places (
  kind text, name text, subtype text, sido text, sigungu text, emd text, ri text,
  road text, road_norm text, main_no text, sub_no text, bld text, postal text,
  haeng_dong text, bd_mgt_sn text, bcode text, hcode text, phone text, opened text,
  jibun text, cat1 text, cat2 text, source text, is_primary text, lon text, lat text
);
\copy _stg_places FROM '__CSV__' WITH (FORMAT csv)

TRUNCATE address;
TRUNCATE poi;

-- 적재용 2차 인덱스 DROP (PK address_pkey/poi_pkey 는 유지)
DROP INDEX IF EXISTS address_geom_gix;
DROP INDEX IF EXISTS address_kind_idx;
DROP INDEX IF EXISTS address_source_idx;
DROP INDEX IF EXISTS address_search_trgm;
DROP INDEX IF EXISTS address_bld_trgm;
DROP INDEX IF EXISTS address_search_prefix_na_idx;
DROP INDEX IF EXISTS address_bld_prefix_na_idx;
DROP INDEX IF EXISTS address_road_addr_idx;
DROP INDEX IF EXISTS address_addr_geom_gix;
DROP INDEX IF EXISTS address_region_idx;
DROP INDEX IF EXISTS address_synth_pnu_idx;
-- 위 인덱스와 같은 표현식에 묶인 확장통계도 반드시 함께 DROP 한다. 아래 재생성이
-- CREATE STATISTICS IF NOT EXISTS 라, 남겨 두면 표현식을 바꿔도 통계만 옛 정의에 묶인 채
-- 살아남는다 — 인덱스는 새 정의로 갱신되므로 결과는 맞는데 선택도 추정만 DEFAULT_EQ_SEL
-- 로 되돌아가 조용히 병렬 스캔이 부활한다. 둘을 한 블록에 두어 동시 재생성을 보장한다.
DROP STATISTICS IF EXISTS address_synth_pnu_stat;
DROP INDEX IF EXISTS poi_geom_gix;
DROP INDEX IF EXISTS poi_kind_idx;
DROP INDEX IF EXISTS poi_primary_idx;

INSERT INTO address(kind,name,subtype,sido,sigungu,emd,ri,road,road_norm,main_no,sub_no,bld,postal,
                    haeng_dong,bd_mgt_sn,bcode,hcode,phone,opened,jibun,cat1,cat2,source,is_primary,geom)
SELECT kind,name,subtype,sido,sigungu,emd,ri,road,road_norm,
       nullif(main_no,'')::int, nullif(sub_no,'')::int, bld, postal,
       haeng_dong,bd_mgt_sn,bcode,hcode,phone,opened,jibun,cat1,cat2,source,
       nullif(is_primary,'')::smallint,
       CASE WHEN nullif(lon,'') IS NOT NULL AND nullif(lat,'') IS NOT NULL
            THEN ST_SetSRID(ST_MakePoint(lon::float8, lat::float8),4326) END
FROM _stg_places;

INSERT INTO poi(kind,name,subtype,cat1,cat2,source,is_primary,phone,geom)
SELECT kind,name,subtype,cat1,cat2,source, nullif(is_primary,'')::smallint, phone,
       ST_SetSRID(ST_MakePoint(lon::float8, lat::float8),4326)
FROM _stg_places
WHERE kind IN ('biz','facility') AND nullif(lon,'') IS NOT NULL AND nullif(lat,'') IS NOT NULL;

DROP TABLE _stg_places;

-- 인덱스 일괄 재생성(대량 적재 후). trgm GIN 은 maintenance_work_mem 에 가장 민감.
SET maintenance_work_mem = '__MWM__';
SET max_parallel_maintenance_workers = __MPW__;   -- btree 한정 가속(GIN/GiST 는 무시)
CREATE INDEX IF NOT EXISTS address_geom_gix      ON address USING gist (geom);
CREATE INDEX IF NOT EXISTS address_kind_idx      ON address (kind);
CREATE INDEX IF NOT EXISTS address_source_idx    ON address (source);
CREATE INDEX IF NOT EXISTS address_search_trgm   ON address USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS address_bld_trgm      ON address USING gin (bld gin_trgm_ops);
CREATE INDEX IF NOT EXISTS address_search_prefix_na_idx ON address (search_text text_pattern_ops) WHERE kind <> 'addr' AND geom IS NOT NULL;
CREATE INDEX IF NOT EXISTS address_bld_prefix_na_idx    ON address (bld text_pattern_ops) WHERE kind <> 'addr' AND geom IS NOT NULL;
CREATE INDEX IF NOT EXISTS address_road_addr_idx ON address (road_norm, main_no, sub_no) WHERE kind = 'addr';
CREATE INDEX IF NOT EXISTS address_addr_geom_gix ON address USING gist (geom) WHERE kind = 'addr';
CREATE INDEX IF NOT EXISTS address_region_idx    ON address (sigungu, emd);
-- 합성 PNU 키조인(역지오 도로명축). 표현식+부분 인덱스 — 근거는 11-address-search.sql 주석 참조.
CREATE INDEX IF NOT EXISTS address_synth_pnu_idx ON address ((bcode || substr(bd_mgt_sn, 11, 9))) WHERE kind = 'addr';
-- 위 인덱스는 '부분'이라 플래너가 그 표현식 통계를 안 쓴다(선택도 0.005 고정 → 불필요한 병렬 스캔).
-- 독립 통계 객체로 보정한다. 아래 ANALYZE address 가 채워 준다. 근거는 11-address-search.sql 주석 참조.
CREATE STATISTICS IF NOT EXISTS address_synth_pnu_stat ON (bcode || substr(bd_mgt_sn, 11, 9)) FROM address;
CREATE INDEX IF NOT EXISTS poi_geom_gix          ON poi USING gist (geom);
CREATE INDEX IF NOT EXISTS poi_kind_idx          ON poi (kind);
CREATE INDEX IF NOT EXISTS poi_primary_idx       ON poi (is_primary);
RESET maintenance_work_mem;
RESET max_parallel_maintenance_workers;

ANALYZE address; ANALYZE poi;
SELECT 'address' AS t, count(*) FROM address UNION ALL SELECT 'poi', count(*) FROM poi;
"""


# ═══════════════════════ [T049] 테이블 스왑 모드 ═══════════════════════
# 설계 근거: .team/tasks/049-*/task.md — TRUNCATE 경로는 실패 시 서비스 전멸 + 수동 추가
# 인덱스 5종(sido/sigungu/emd trgm·postal·poi_tier)이 하드코딩 목록에 없어 조용히 소실됐다.
# 스왑 모드는 (a) 원본 보존 (b) 인덱스를 라이브 카탈로그에서 복제 (c) 스왑 전 검증 게이트로
# 세 결함을 구조적으로 막는다.

INT_COLS = {"main_no", "sub_no", "is_primary"}

PHASES = {
    "address": {
        "cols": ["kind","name","subtype","sido","sigungu","emd","ri","road","road_norm",
                 "main_no","sub_no","bld","postal","haeng_dong","bd_mgt_sn","bcode","hcode",
                 "phone","opened","jibun","cat1","cat2","source","is_primary","geom"],
        "select": "SELECT kind,name,subtype,sido,sigungu,emd,ri,road,road_norm,"
                  "main_no,sub_no,bld,postal,haeng_dong,bd_mgt_sn,bcode,hcode,"
                  "phone,opened,jibun,cat1,cat2,source,is_primary,lon,lat FROM places",
        "seq": "address_id_seq",
    },
    "poi": {
        "cols": ["kind","name","subtype","cat1","cat2","source","is_primary","phone","geom"],
        "select": "SELECT kind,name,subtype,cat1,cat2,source,is_primary,phone,lon,lat FROM places "
                  "WHERE kind IN ('biz','facility') AND lon IS NOT NULL AND lat IS NOT NULL",
        "seq": "poi_id_seq",
    },
}


def psql(env, *cmds, capture=False):
    """한 psql 세션에서 -c 를 순차 실행. SET 은 세션에 남는다(각 -c 는 별도 트랜잭션)."""
    argv = ["psql", "-v", "ON_ERROR_STOP=1", "-q"]
    for c in cmds:
        argv += ["-c", c]
    r = subprocess.run(argv, env=env, text=True,
                       capture_output=capture)
    if r.returncode != 0:
        sys.exit(f"✗ psql 실패 (rc={r.returncode}): {cmds[0][:80]}…")
    return r.stdout if capture else None


def psql_q(env, query):
    r = subprocess.run(["psql", "-tAc", query], env=env, text=True, capture_output=True)
    if r.returncode != 0:
        sys.exit(f"✗ psql 질의 실패: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def sqlite_expected(db_path, phase):
    """원천(geocode.sqlite) 기준 기대값 — 게이트의 정답지. 읽기 전용."""
    import sqlite3
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    if phase == "address":
        total = con.execute("SELECT count(*) FROM places").fetchone()[0]
        cross = dict(((k, s), n) for k, s, n in con.execute(
            "SELECT kind, source, count(*) FROM places GROUP BY 1,2"))
        ri = con.execute("SELECT count(*) FROM places WHERE kind='addr' AND ri IS NOT NULL AND ri<>''").fetchone()[0]
        addr = con.execute("SELECT count(*) FROM places WHERE kind='addr'").fetchone()[0]
    else:
        total = con.execute("SELECT count(*) FROM places WHERE kind IN ('biz','facility') "
                            "AND lon IS NOT NULL AND lat IS NOT NULL").fetchone()[0]
        cross = dict(((k, s), n) for k, s, n in con.execute(
            "SELECT kind, source, count(*) FROM places WHERE kind IN ('biz','facility') "
            "AND lon IS NOT NULL AND lat IS NOT NULL GROUP BY 1,2"))
        ri = addr = None
    con.close()
    return {"total": total, "cross": cross, "ri": ri, "addr": addr}


def stream_copy(env, db_path, phase, table, limit=None):
    """sqlite → psql \\copy STDIN 직송. 호스트 임시 CSV 를 만들지 않는다(디스크 피크 절감).

    파리티 계약(기존 truncate 경로와 동일한 결과):
      · text 빈값/NULL → '' (staging text 경로와 동일)
      · int(main_no/sub_no/is_primary) 빈값 → NULL
      · geom: lon·lat 둘 다 있을 때만 EWKT, 아니면 NULL
    """
    import sqlite3
    spec = PHASES[phase]
    sel = spec["select"] + (f" LIMIT {int(limit)}" if limit else "")
    ncols = len(spec["cols"])
    copy = (f"\\copy {table} ({','.join(spec['cols'])}) "
            "FROM STDIN WITH (FORMAT csv, NULL '\\N')")
    proc = subprocess.Popen(
        ["psql", "-v", "ON_ERROR_STOP=1", "-q",
         "-c", "SET synchronous_commit = off", "-c", copy],
        env=env, stdin=subprocess.PIPE, text=True)
    w = csv.writer(proc.stdin)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.execute(sel)
    n = 0
    while True:
        rows = cur.fetchmany(50000)
        if not rows:
            break
        out = []
        for r in rows:
            lon, lat = r[-2], r[-1]
            vals = []
            for name, v in zip(spec["cols"][:-1], r[:-2]):
                if name in INT_COLS:
                    vals.append("\\N" if v in (None, "") else v)
                else:
                    vals.append("" if v is None else v)
            vals.append("\\N" if lon in (None, "") or lat in (None, "")
                        else f"SRID=4326;POINT({lon} {lat})")
            assert len(vals) == ncols
            out.append(vals)
        w.writerows(out)
        n += len(rows)
        if n % 1000000 < 50000:
            print(f"      … {n:,} rows", file=sys.stderr)
    con.close()
    proc.stdin.close()
    if proc.wait() != 0:
        sys.exit("✗ copy(STDIN) 실패")
    return n


def normalize_live_names(env, table):
    """이전 스왑이 finalize [3/3] 를 못 거쳤으면 라이브 테이블의 PK 제약·인덱스·통계가 *_new 이름을 달고 있다.
    그대로 두면 새 스왑이 같은 이름을 만들다 'relation already exists' 로 죽는다
    ([실측 2026-09-03 .244] address 의 PK 가 address_new_pkey 로 남아 phase=address 가 [2/5]에서 실패).
    finalize 와 같은 규칙으로 정식명으로 환원한다(멱등). 정식명이 이미 있으면 _new 쪽은 중복이므로 DROP."""
    fixed = []
    if psql_q(env, f"SELECT count(*) FROM pg_constraint WHERE conrelid='{table}'::regclass "
                   f"AND conname='{table}_new_pkey'") == "1":
        psql(env, f"ALTER TABLE {table} RENAME CONSTRAINT {table}_new_pkey TO {table}_pkey")
        fixed.append(f"{table}_new_pkey→{table}_pkey")
    for line in psql_q(env, f"SELECT indexname FROM pg_indexes WHERE tablename='{table}' "
                            f"AND indexname LIKE '%\\_new'").splitlines():
        nm = line.strip()
        if not nm:
            continue
        if psql_q(env, f"SELECT to_regclass('public.{nm[:-4]}') IS NOT NULL") == "t":
            psql(env, f"DROP INDEX {nm}"); fixed.append(f"{nm}(중복 DROP)")
        else:
            psql(env, f"ALTER INDEX {nm} RENAME TO {nm[:-4]}"); fixed.append(nm)
    for line in psql_q(env, "SELECT stxname FROM pg_statistic_ext WHERE "
                            f"stxrelid='{table}'::regclass AND stxname LIKE '%\\_new'").splitlines():
        nm = line.strip()
        if nm:
            psql(env, f"ALTER STATISTICS {nm} RENAME TO {nm[:-4]}"); fixed.append(nm)
    if fixed:
        print(f"      잔재 정규화({table}): {', '.join(fixed)}", file=sys.stderr)


def live_index_defs(env, table):
    """살아있는 카탈로그에서 2차 인덱스 + 확장통계 DDL 을 뜬다. pkey 는 제약으로 별도 생성.
    PK 는 이름이 아니라 카탈로그 플래그(indisprimary)로 뺀다 — 잔재 이름(address_new_pkey)이면 이름 비교가 놓쳐
    PK 인덱스를 2차 인덱스로 한 번 더 복제한다(2026-09-03 실측: address_new_pkey_new 생성 후 제약 충돌)."""
    idefs = [l for l in psql_q(env,
        f"SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename='{table}' "
        f"AND indexname <> '{table}_pkey' "
        f"AND indexname NOT IN (SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
        f"                      WHERE i.indrelid='{table}'::regclass AND i.indisprimary) "
        f"ORDER BY indexname").splitlines() if l.strip()]
    sdefs = [l for l in psql_q(env,
        f"SELECT pg_get_statisticsobjdef(oid) FROM pg_statistic_ext WHERE stxrelid='{table}'::regclass"
    ).splitlines() if l.strip()]
    return idefs, sdefs


def _to_new(ddl, table):
    """인덱스/통계 DDL 을 <table>_new 대상·<name>_new 명칭으로 변환."""
    m = re.match(r"CREATE (UNIQUE )?INDEX (\S+) ON ", ddl)
    if m:
        ddl = ddl.replace(f"CREATE {m.group(1) or ''}INDEX {m.group(2)} ON ",
                          f"CREATE {m.group(1) or ''}INDEX {m.group(2)}_new ON ", 1)
    m2 = re.match(r"CREATE STATISTICS (\S+) ", ddl)
    if m2:
        ddl = ddl.replace(f"CREATE STATISTICS {m2.group(1)} ",
                          f"CREATE STATISTICS {m2.group(1)}_new ", 1)
    return (ddl.replace(f" ON public.{table} USING ", f" ON public.{table}_new USING ")
               .replace(f" FROM {table}", f" FROM {table}_new")
               .replace(f" FROM public.{table}", f" FROM public.{table}_new"))


def cmd_swap(args, env):
    phase, table = args.phase, args.phase
    new, old = f"{table}_new", f"{table}_old"
    t0 = time.time()

    if psql_q(env, f"SELECT to_regclass('public.{old}') IS NOT NULL") == "t":
        sys.exit(f"✗ {old} 가 이미 있다 — 이전 스왑이 finalize 되지 않았다. 검증 후 --finalize 먼저.")
    if psql_q(env, f"SELECT to_regclass('public.{new}') IS NOT NULL") == "t":
        sys.exit(f"✗ {new} 가 이미 있다 — 잔재를 확인하고 수동 DROP 후 재시도하라.")
    normalize_live_names(env, table)   # 이전 스왑의 *_new 이름 잔재 → 정식명(멱등)

    print(f"[0/5] 기대값 산출(원천 sqlite, 읽기전용) …", file=sys.stderr)
    exp = sqlite_expected(args.db, phase)
    cur_total = int(psql_q(env, f"SELECT count(*) FROM {table}"))
    print(f"      기대 {exp['total']:,} rows · 현행 {table} {cur_total:,} rows", file=sys.stderr)

    print(f"[1/5] {new} 생성(LIKE — 컬럼·기본값·생성컬럼 복제) + 적재 …", file=sys.stderr)
    psql(env, f"CREATE TABLE {new} (LIKE {table} INCLUDING DEFAULTS INCLUDING GENERATED)")
    n = stream_copy(env, args.db, phase, new, args.limit)
    got = int(psql_q(env, f"SELECT count(*) FROM {new}"))
    print(f"      적재 {n:,} → 테이블 {got:,}", file=sys.stderr)
    if got != n:
        sys.exit(f"✗ 적재 행수 불일치: 스트림 {n:,} vs 테이블 {got:,}")

    print(f"[2/5] 인덱스 재생성(라이브 카탈로그 복제) …", file=sys.stderr)
    idefs, sdefs = live_index_defs(env, table)
    mwm = os.environ.get("GEOCODE_MAINT_MEM", "2GB")
    if not re.fullmatch(r"\d+[kKmMgG][bB]?", mwm):
        mwm = "2GB"
    mpw = str(int(os.environ.get("GEOCODE_MAINT_WORKERS", "4")))
    for d in idefs:
        nd = _to_new(d, table)
        print(f"      · {nd.split(' ON ')[0][:70]}", file=sys.stderr)
        psql(env, f"SET maintenance_work_mem = '{mwm}'",
                  f"SET max_parallel_maintenance_workers = {mpw}", nd)
    psql(env, f"ALTER TABLE {new} ADD CONSTRAINT {new}_pkey PRIMARY KEY (id)")
    for d in sdefs:
        psql(env, _to_new(d, table))
    psql(env, f"ANALYZE {new}")

    print(f"[3/5] 스왑 전 검증 게이트 …", file=sys.stderr)
    fails = []
    if args.limit is None:
        if got != exp["total"]:
            fails.append(f"총행수 {got:,} ≠ 기대 {exp['total']:,}")
        if got < int(cur_total * 0.95):
            fails.append(f"총행수 {got:,} < 현행 95% ({int(cur_total*0.95):,})")
        cross_new = {}
        for line in psql_q(env, f"SELECT kind||'|'||source||'|'||count(*) FROM {new} GROUP BY kind, source").splitlines():
            k, s2, c = line.rsplit("|", 2)
            cross_new[(k, s2)] = int(c)
        if cross_new != exp["cross"]:
            miss = set(exp["cross"]) - set(cross_new)
            diff = {k: (exp["cross"].get(k), cross_new.get(k))
                    for k in set(exp["cross"]) | set(cross_new)
                    if exp["cross"].get(k) != cross_new.get(k)}
            fails.append(f"kind×source 교차표 불일치: {dict(list(diff.items())[:4])}" +
                         (f" · 소실 {miss}" if miss else ""))
    n_idx_old = int(psql_q(env, f"SELECT count(*) FROM pg_indexes WHERE tablename='{table}'"))
    n_idx_new = int(psql_q(env, f"SELECT count(*) FROM pg_indexes WHERE tablename='{new}'"))
    if n_idx_new != n_idx_old:
        fails.append(f"인덱스 수 {n_idx_new} ≠ 현행 {n_idx_old}")
    if phase == "address" and args.limit is None:
        ri_new = int(psql_q(env, f"SELECT count(*) FROM {new} WHERE kind='addr' AND ri IS NOT NULL AND ri<>''"))
        print(f"      ri 채움: {ri_new:,}/{exp['addr']:,} ({ri_new/max(exp['addr'],1):.2%}) — 기대 {exp['ri']:,}", file=sys.stderr)
        if ri_new != exp["ri"]:
            fails.append(f"ri 채움 {ri_new:,} ≠ 원천 {exp['ri']:,}")
    if fails:
        print(f"✗ 게이트 실패 — 스왑하지 않는다. {new} 는 진단용으로 남긴다:", file=sys.stderr)
        for f in fails:
            print(f"    · {f}", file=sys.stderr)
        sys.exit(3)
    print(f"      게이트 통과: 행수·교차표·인덱스 {n_idx_new}종 일치", file=sys.stderr)

    if args.dry_run or args.limit is not None:
        print(f"[4/5] --dry-run/--limit — 스왑 생략. {new} 잔류(검증·폐기는 운영자 몫). "
              f"{time.time()-t0:.0f}s", file=sys.stderr)
        return

    print(f"[4/5] 스왑(RENAME 트랜잭션) …", file=sys.stderr)
    ts = time.time()
    psql(env, f"BEGIN; LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE; "
              f"ALTER TABLE {table} RENAME TO {old}; "
              f"ALTER TABLE {new} RENAME TO {table}; COMMIT;")
    dt = time.time() - ts
    now = int(psql_q(env, f"SELECT count(*) FROM {table}"))
    print(f"[5/5] 스왑 완료 — 다운타임(락 구간) {dt*1000:.0f}ms · 현행 {table} {now:,} rows · "
          f"{old} 보존(롤백 경로) · 총 {time.time()-t0:.0f}s", file=sys.stderr)
    print(f"      다음: 외부 검증(§4.2) 통과 후 --finalize 로 {old} 정리", file=sys.stderr)


def cmd_finalize(args, env):
    phase, table = args.phase, args.phase
    old, seq = f"{table}_old", PHASES[phase]["seq"]
    if psql_q(env, f"SELECT to_regclass('public.{old}') IS NOT NULL") != "t":
        sys.exit(f"✗ {old} 가 없다 — finalize 할 것이 없다.")

    if args.rollback_drill:
        print("[드릴] 롤백 실증: 역스왑 → 확인 → 재스왑 …", file=sys.stderr)
        ts = time.time()
        psql(env, f"BEGIN; ALTER TABLE {table} RENAME TO {table}_tmp_drill; "
                  f"ALTER TABLE {old} RENAME TO {table}; COMMIT;")
        back = int(psql_q(env, f"SELECT count(*) FROM {table}"))
        psql(env, f"BEGIN; ALTER TABLE {table} RENAME TO {old}; "
                  f"ALTER TABLE {table}_tmp_drill RENAME TO {table}; COMMIT;")
        fwd = int(psql_q(env, f"SELECT count(*) FROM {table}"))
        print(f"[드릴] 완료 {time.time()-ts:.2f}s — 구판 {back:,} ↔ 신판 {fwd:,} rows. 롤백 경로 실증됨",
              file=sys.stderr)

    print(f"[1/3] 시퀀스 소유권 이전({seq} → {table}.id) — {old} DROP 의 연쇄삭제 차단", file=sys.stderr)
    psql(env, f"ALTER SEQUENCE {seq} OWNED BY {table}.id")
    print(f"[2/3] {old} DROP …", file=sys.stderr)
    psql(env, f"DROP TABLE {old}")
    print(f"[3/3] 인덱스·제약·통계 정식명 환원 …", file=sys.stderr)
    # PK 제약명 {table}_new_pkey 는 접미가 _pkey 라 '%\_new' 에 안 걸린다 — 명시 포함하지
    # 않으면 매 스왑마다 이름 충돌(relation already exists)로 [2/5] 가 죽는다.
    for line in psql_q(env, f"SELECT indexname FROM pg_indexes WHERE tablename='{table}' "
                            f"AND (indexname LIKE '%\\_new' OR indexname = '{table}_new_pkey')").splitlines():
        nm = line.strip()
        if not nm:
            continue
        if nm == f"{table}_new_pkey":
            psql(env, f"ALTER TABLE {table} RENAME CONSTRAINT {table}_new_pkey TO {table}_pkey")
        else:
            psql(env, f"ALTER INDEX {nm} RENAME TO {nm[:-4]}")
    for line in psql_q(env, "SELECT stxname FROM pg_statistic_ext WHERE "
                            f"stxrelid='{table}'::regclass AND stxname LIKE '%\\_new'").splitlines():
        nm = line.strip()
        if nm:
            psql(env, f"ALTER STATISTICS {nm} RENAME TO {nm[:-4]}")
    psql(env, f"ANALYZE {table}")
    n_idx = int(psql_q(env, f"SELECT count(*) FROM pg_indexes WHERE tablename='{table}'"))
    print(f"완료: {table} 인덱스 {n_idx}종 · finalize 종료", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "geocode.sqlite"))
    ap.add_argument("--mode", choices=["swap", "truncate"], default="swap",
                    help="swap(기본)=무중단 테이블 스왑 / truncate=종전 경로(운영 사용 금지)")
    ap.add_argument("--phase", choices=["address", "poi"],
                    help="swap 모드 필수 — 한 번에 한 테이블(디스크 피크 관리, address 먼저)")
    ap.add_argument("--dry-run", action="store_true", help="swap: 적재·인덱스·검증까지만")
    ap.add_argument("--finalize", action="store_true",
                    help="swap 후 검증 통과 시: (--rollback-drill) → <t>_old DROP → 정식명 환원")
    ap.add_argument("--rollback-drill", action="store_true", help="finalize 직전 롤백 실증(역스왑↔재스왑)")
    ap.add_argument("--limit", type=int, default=None, help="스모크 전용 — 강제 dry-run")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        sys.exit(f"geocode.sqlite 없음: {args.db} (09-gen-geocode.py 먼저)")

    env = dict(os.environ)
    env.setdefault("PGHOST", "localhost"); env.setdefault("PGPORT", "5433")
    env.setdefault("PGUSER", "cuvia"); env.setdefault("PGDATABASE", "cuvia")
    env.setdefault("PGPASSWORD", "cuvia")

    if args.mode == "swap":
        if not args.phase:
            sys.exit("✗ --mode swap 은 --phase address|poi 가 필수다 (address 먼저)")
        if args.finalize:
            cmd_finalize(args, env)
        else:
            cmd_swap(args, env)
        return

    # ── 이하 종전 truncate 경로(보존 — 운영에서 쓰지 마라) ──
    t0 = time.time()
    tmpd = os.path.join(os.path.dirname(os.path.abspath(args.db)), "tmp")
    os.makedirs(tmpd, exist_ok=True)
    fd, csv_path = tempfile.mkstemp(suffix=".csv", dir=tmpd); os.close(fd)
    try:
        print(f"[1/2] geocode.sqlite → CSV 내보내기 …", file=sys.stderr)
        n = export_csv(args.db, csv_path)
        print(f"      {n:,} rows → {csv_path} ({os.path.getsize(csv_path)/1e6:.0f}MB)", file=sys.stderr)
        print(f"[2/2] PostGIS 적재(address + poi) …", file=sys.stderr)
        # 인덱스 재생성 튜닝(env). MWM 은 잘못된 값이면 기본값으로(SQL 주입 방지), MPW 는 int 강제.
        mwm = os.environ.get("GEOCODE_MAINT_MEM", "2GB")
        if not re.fullmatch(r"\d+[kKmMgG][bB]?", mwm):
            mwm = "2GB"
        mpw = str(int(os.environ.get("GEOCODE_MAINT_WORKERS", "4")))
        sql = (SQL.replace("__CSV__", csv_path.replace("'", "''"))
                  .replace("__MWM__", mwm)
                  .replace("__MPW__", mpw))
        r = subprocess.run(["psql", "-v", "ON_ERROR_STOP=1"], input=sql, text=True, env=env)
        if r.returncode != 0:
            sys.exit("✗ psql 적재 실패")
    finally:
        try: os.unlink(csv_path)
        except OSError: pass
    print(f"OK: address/poi 적재 완료 · {time.time()-t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
