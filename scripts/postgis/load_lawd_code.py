#!/usr/bin/env python3
"""법정동코드 전체자료(code.go.kr) → PostGIS `lawd_code` 적재 (T018 S1).

원본은 CP949 / TAB 구분 / CRLF 의 3컬럼 TSV 다:

    법정동코드   법정동명                        폐지여부
    1100000000  서울특별시                       존재
    4671025900  전라남도 달성군 유가읍            폐지

이 테이블이 `lawd_ri`(리 사전)의 **유일한 진실 원천**이다. 종전 build_ri_dict.sql 은
원본을 적재하지 않고 address(도로명주소 건물 DB)에서 사전을 역산해 만들었기 때문에
건물 없는 리가 통째로 누락됐고, 09-gen-geocode.py:175 의 setdefault 로 같은 읍면의
리 이름이 하나만 살아남아 코드가 뭉개졌다. 원본을 적재하면 두 결함이 함께 사라진다.

**폐지 행을 버리지 않고 exist 플래그로 보존한다.** 전남·광주는 법정동코드가 46/29 →
12 로 개편됐으나 우리 DB(parcel·building 파티션키, lawd_dong)는 아직 46/29 체계다.
사전을 존재 행만으로 만들면 전남 리가 전부 12 로 바뀌어 **지금 동작하는 전남 리
지오코딩이 깨진다.** 폐지 행이 있어야 46/29 폴백을 만들 수 있다(S2 참조).

연결은 libpq 환경변수(PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSWORD).
기본 cuvia/cuvia@localhost:5433 — scripts/postgis/_pg-env.sh 와 동일.

  scripts/postgis/load_lawd_code.py [원본.zip|원본.txt]

원본을 안 주면 fetch_lawd_code.sh 로 임시 디렉터리에 받는다.
행수가 --expect-rows(기본 53,387)와 다르면 **적재하지 않고 종료한다** — 원본이 갱신되면
계획서 §A 의 실측 수치와 S2·S3 게이트 기대값이 전부 무효가 되기 때문이다.
"""
import argparse, csv, hashlib, os, subprocess, sys, tempfile, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
EXPECT_SHA = "7b4b544a6302d26c4f4c89d2c1355beae82e958c786bad8cc8572db0d2e2eb33"

# 원본 3컬럼 그대로. exist 는 폐지여부='존재'.
# left(bcode,8) 인덱스는 S2 의 읍면동 단위 집계·S3 의 조인 키가 그대로 쓴다.
SQL = r"""
\set ON_ERROR_STOP on

DROP TABLE IF EXISTS lawd_code;
CREATE TABLE lawd_code (
  bcode  char(10) PRIMARY KEY,
  name   text     NOT NULL,
  exist  boolean  NOT NULL          -- 폐지여부='존재'
);
COMMENT ON TABLE lawd_code IS
  '행정표준코드관리시스템 법정동코드 전체자료 원본(폐지 포함). lawd_ri 의 유일한 원천 — T018.';

\copy lawd_code (bcode, name, exist) FROM '__CSV__' WITH (FORMAT csv)

CREATE INDEX lawd_code_emd8_idx ON lawd_code (left(bcode,8));
CREATE INDEX lawd_code_name_idx ON lawd_code (name);
ANALYZE lawd_code;
"""

# (설명, SQL, 기대값). 하나라도 어긋나면 원본이 갱신된 것이므로 즉시 중단한다.
CHECKS = [
    ("전체 행수",        "SELECT count(*) FROM lawd_code",                             "53387"),
    ("존재",            "SELECT count(*) FROM lawd_code WHERE exist",                 "20560"),
    ("폐지",            "SELECT count(*) FROM lawd_code WHERE NOT exist",             "32827"),
    ("코드 형식 위반",    r"SELECT count(*) FROM lawd_code WHERE bcode !~ '^\d{10}$'",  "0"),
    ("12체계 존재(전남)", "SELECT count(*) FROM lawd_code WHERE left(bcode,2)='12' AND exist", "3204"),
]


def read_rows(path):
    """원본(zip 또는 평문 txt)에서 (bcode, name, exist) 리스트를 뽑는다."""
    if zipfile.is_zipfile(path):
        z = zipfile.ZipFile(path)
        infos = z.infolist()
        if len(infos) != 1:
            sys.exit(f"✗ zip 엔트리가 1개가 아니다: {len(infos)}개")
        raw = z.read(infos[0])
    else:
        with open(path, "rb") as f:
            raw = f.read()

    txt = raw.decode("cp949")                       # CP949 고정 — UTF-8 로 열면 조용히 깨진다
    lines = [l for l in txt.split("\r\n") if l.strip()]
    if not lines:
        sys.exit("✗ 원본이 비어 있다")

    head = lines[0].split("\t")
    if head[:3] != ["법정동코드", "법정동명", "폐지여부"]:
        sys.exit(f"✗ 헤더가 예상과 다르다: {head} — 원본 포맷이 바뀌었다")

    rows = []
    for ln, line in enumerate(lines[1:], start=2):
        f = line.split("\t")
        if len(f) != 3:
            sys.exit(f"✗ {ln}행 컬럼 수 {len(f)} (3 이어야 함): {line[:80]!r}")
        code, name, disuse = (x.strip() for x in f)
        if disuse not in ("존재", "폐지"):
            sys.exit(f"✗ {ln}행 폐지여부 값이 예상 밖: {disuse!r}")
        rows.append((code, name, disuse == "존재"))
    return rows


def psql(sql, quiet=True):
    """psql 한 문장 실행. -t -A 로 값만 받는다."""
    argv = ["psql", "-v", "ON_ERROR_STOP=1", "-t", "-A", "-c", sql]
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"✗ psql 실패:\n{r.stderr}")
    return r.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", help="법정동코드 전체자료 zip 또는 txt (생략 시 새로 받음)")
    ap.add_argument("--expect-rows", type=int, default=53387,
                    help="기대 데이터 행수. 다르면 적재하지 않는다 (0 = 게이트 해제)")
    args = ap.parse_args()

    os.environ.setdefault("PGHOST", "localhost")
    os.environ.setdefault("PGPORT", "5433")
    os.environ.setdefault("PGUSER", "cuvia")
    os.environ.setdefault("PGDATABASE", "cuvia")
    os.environ.setdefault("PGPASSWORD", "cuvia")

    with tempfile.TemporaryDirectory() as tmp:
        src = args.src
        if not src:
            src = os.path.join(tmp, "lawd_full.zip")
            subprocess.run([os.path.join(HERE, "fetch_lawd_code.sh"), src], check=True)

        sha = hashlib.sha256(open(src, "rb").read()).hexdigest()
        size = os.path.getsize(src)
        print(f"원본   : {src}")
        print(f"크기   : {size:,} bytes")
        print(f"SHA256 : {sha}")
        print(f"         {'= T018 기준 원본' if sha == EXPECT_SHA else '≠ T018 기준 원본 (' + EXPECT_SHA + ')'}")

        rows = read_rows(src)
        print(f"행수   : {len(rows):,}")
        if args.expect_rows and len(rows) != args.expect_rows:
            sys.exit(f"✗ 행수가 기대({args.expect_rows:,})와 다르다 — 원본이 갱신됐다.\n"
                     f"  계획서 §A 수치와 S2·S3 게이트 기대값을 전부 재측정한 뒤 재개할 것.\n"
                     f"  게이트를 의도적으로 풀려면 --expect-rows 0")

        # CP949 → UTF-8 CSV. \copy 는 클라이언트(호스트) 파일을 읽으므로 컨테이너 경유가 필요 없다.
        csv_path = os.path.join(tmp, "lawd_code.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)

        sql_path = os.path.join(tmp, "load.sql")
        with open(sql_path, "w", encoding="utf-8") as f:
            f.write(SQL.replace("__CSV__", csv_path))

        r = subprocess.run(["psql", "-v", "ON_ERROR_STOP=1", "-q", "-f", sql_path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"✗ 적재 실패:\n{r.stdout}\n{r.stderr}")

    print("\n검증 (기대값 대조)")
    bad = 0
    for label, sql, want in CHECKS:
        got = psql(sql)
        ok = (got == want)
        bad += (not ok)
        print(f"  {'✓' if ok else '✗'} {label:<16} = {int(got):>7,}   기대 {int(want):>7,}")
    if bad:
        sys.exit(f"\n✗ 게이트 {bad}건 미달 — S2 로 진행하지 말 것.")
    print("\n✓ S1 통과")


if __name__ == "__main__":
    main()
