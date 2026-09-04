#!/usr/bin/env python3
"""VWorld 법정동코드(신구대응) → PostGIS `lawd_code_v2` 적재 (T026 S2).

원본은 CP949 / 쉼표 구분 / 10컬럼 CSV 다 (LSCT_LAWDCD.csv, VWorld dsId=30505):

    LAWD_CD,SIDO_NM,SGG_NM,UMD_NM,RI_NM,CRE_DT,DEL_DT,OLD_LAWDCD,FRST_REGIST_DT,LAST_UPDT_DT
    4790043030,경상북도,예천군,은풍면,시항리,20160201,,,20260708,20260708
    4275038000,강원도,영월군,무릉도원면,,20161116,20230611,,20260708,20260708

■ 이 테이블의 존재 이유 = **OLD_LAWDCD 컬럼** (T026 §3-1)

  OLD_LAWDCD 는 법정동코드가 개편될 때 **행정안전부가 직접 부여한 옛 코드**다.
  즉 "옛 코드 → 새 코드" 대응이 원천 데이터 안에 이미 들어 있다.

  T021 은 이 컬럼의 존재를 몰라서 **명칭 꼬리 조인**(옛 동명과 새 동명이 같으면
  같은 동일 것이다)으로 대응을 역산했다. 그 방식은 동명이 겹치는 순간 무너진다 —
  인천 신설 4구에는 서구/검단구에 같은 이름의 동이 실재한다. OLD_LAWDCD 를 쓰면
  **명칭을 한 번도 보지 않고** 코드-대-코드로 대응이 확정된다. 이것이 T026 이
  T021 을 폐기하고 다시 설계한 유일한 이유이고, 이 적재기가 존재하는 이유다.
  (근거 문서: docs/incheon-sgg-remap.md, 계획서 §3-1·§4-2)

  ⚠ 대응의 조인 축은 **OLD_LAWDCD 하나뿐**이다. 이 테이블을 근거로 명칭 조인 SQL을
    새로 쓰지 말 것(계획서 §0 P1).

■ `lawd_code`(code.go.kr, 53,387행)와의 관계 — **별개이고 대체하지 않는다**

  · lawd_code   : 행정표준코드관리시스템 원본. `lawd_ri`(리 사전)의 유일한 원천.
                  리(RI) 단위까지 폐지 이력을 다 갖고 있어 T018 의 46/29 폴백 근거.
  · lawd_code_v2: VWorld 원본. OLD_LAWDCD 를 가진 **유일한** 원천. 행수가 31,172 로
                  더 적어(리 수록 범위가 다르다) lawd_code 를 덮어쓸 수 없다.

  둘은 병행 존재한다. lawd_code 를 이 테이블로 교체하면 lawd_ri 가 깨진다.

■ 게이트 (계획서 §3-3)

  6종 기대값이 하나라도 어긋나면 원본이 갱신된 것이다. 그 경우 계획서 §1-4 의
  실측 수치(신설 4구 대응 79쌍 등)와 S3 게이트 기대값이 **전부 무효**가 되므로
  즉시 중단한다. 의도적으로 풀려면 --expect-rows 0.

연결은 libpq 환경변수(PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSWORD).
기본 cuvia/cuvia@localhost:5433 — scripts/postgis/_pg-env.sh 와 동일.

  scripts/postgis/load_lawd_code_v2.py [원본.csv|원본.zip]

원본을 생략하면 $BUILD_HOME/staged/lawd_code_v2 에서 찾는다(data-sources.json 의
lawd_code_v2.build_input.dest). 없으면 **적재하지 않고 실패**한다 — 조용히 건너뛰면
lawd_sgg_remap 이 낡은 채 남아 부분 치환이 발생한다(계획서 R9).
수집은 이 스크립트가 하지 않는다: scripts/build-studio.py 의 collect 단계 몫이다.
"""
import argparse, csv, hashlib, io, os, subprocess, sys, tempfile, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_HOME = os.environ.get("BUILD_HOME", os.path.expanduser("~/geocode-build"))
STAGED = os.path.join(BUILD_HOME, "staged", "lawd_code_v2")

# 2026-08-13 갱신본(31,172행). 다르다고 실패시키지는 않고 표시만 한다 — 행수 게이트가 정본.
EXPECT_SHA = "e8562442f5e8efa90a11f3edee54142d783e02bc8908233a574f9676de6fe209"

HEADER = ["LAWD_CD", "SIDO_NM", "SGG_NM", "UMD_NM", "RI_NM",
          "CRE_DT", "DEL_DT", "OLD_LAWDCD", "FRST_REGIST_DT", "LAST_UPDT_DT"]

# 원본 10컬럼 무손실 보존. char(n) 은 원본 자릿수 고정을 그대로 표현한다.
#
# ※ NOT NULL 은 의도적이다. CSV COPY 는 **인용되지 않은 빈 필드를 NULL 로 읽는다**.
#   적재기는 QUOTE_ALL 로 써서 빈 문자열을 보존하는데, 그 계약이 깨지면 del_dt 가
#   전부 NULL 이 되어 `btrim(del_dt)=''` 게이트가 0 을 반환한다 — 조용한 오적재다.
#   NOT NULL 이 있으면 그 순간 COPY 가 실패해 시끄럽게 드러난다(계획서 R7 계열).
#
# ※ char(n) 은 비교 시 뒤 공백을 무시하지만, psycopg 로 읽으면 공백이 패딩되어 온다.
#   조회하는 쪽은 반드시 btrim() 을 쓸 것(계획서 §4-3).
SQL = r"""
\set ON_ERROR_STOP on

DROP TABLE IF EXISTS lawd_code_v2;
CREATE TABLE lawd_code_v2 (
  bcode          char(10) PRIMARY KEY,        -- LAWD_CD
  sido_nm        text     NOT NULL,
  sgg_nm         text     NOT NULL,
  umd_nm         text     NOT NULL,
  ri_nm          text     NOT NULL,
  cre_dt         char(8)  NOT NULL,
  del_dt         char(8)  NOT NULL,           -- '' = 현존, 값 = 폐지일
  old_bcode      char(10) NOT NULL,           -- OLD_LAWDCD  ★ 이 테이블의 존재 이유
  frst_regist_dt char(8)  NOT NULL,
  last_updt_dt   char(8)  NOT NULL
);
COMMENT ON TABLE lawd_code_v2 IS
  'VWorld 법정동코드(신구대응) 원본 dsId=30505. OLD_LAWDCD(옛 법정동코드)의 유일한 원천 — T026. lawd_code(code.go.kr)를 대체하지 않고 병행한다.';
COMMENT ON COLUMN lawd_code_v2.old_bcode IS
  'OLD_LAWDCD — 개편 시 행안부가 부여한 옛 법정동코드. 옛→신 대응의 유일한 조인 축(명칭 조인 금지, T026 §0 P1).';

\copy lawd_code_v2 FROM '__CSV__' WITH (FORMAT csv)

CREATE INDEX lawd_code_v2_emd8_idx     ON lawd_code_v2 (left(bcode,8));
CREATE INDEX lawd_code_v2_old_idx      ON lawd_code_v2 (old_bcode);
CREATE INDEX lawd_code_v2_old_emd8_idx ON lawd_code_v2 (left(old_bcode,8));
ANALYZE lawd_code_v2;
"""

# (설명, SQL, 기대값) — 계획서 §3-3 게이트 6종. 하나라도 어긋나면 원본 갱신이다.
CHECKS = [
    ("전체 행수",       "SELECT count(*) FROM lawd_code_v2",                                "31172"),
    ("현존(del_dt='')", "SELECT count(*) FROM lawd_code_v2 WHERE btrim(del_dt)=''",         "20552"),
    ("폐지",            "SELECT count(*) FROM lawd_code_v2 WHERE btrim(del_dt)<>''",        "10620"),
    ("old_bcode 채움",  "SELECT count(*) FROM lawd_code_v2 WHERE btrim(old_bcode)<>''",     "4357"),
    ("12체계 현존",     "SELECT count(*) FROM lawd_code_v2 "
                        "WHERE left(bcode,2)='12' AND btrim(del_dt)=''",                    "3204"),
    ("코드 형식 위반",   r"SELECT count(*) FROM lawd_code_v2 WHERE bcode !~ '^\d{10}$'",     "0"),
]


def find_src():
    """원본을 안 줬을 때 staged 디렉터리에서 찾는다. CSV 우선, 없으면 zip."""
    if os.environ.get("LAWD_CODE_V2_SRC"):
        return os.environ["LAWD_CODE_V2_SRC"]
    if not os.path.isdir(STAGED):
        return None
    csvs, zips = [], []
    for root, _dirs, files in os.walk(STAGED):
        for fn in files:
            p = os.path.join(root, fn)
            low = fn.lower()
            if low.endswith(".csv"):
                csvs.append(p)
            elif low.endswith(".zip"):
                zips.append(p)
    # LSCT_LAWDCD.csv 를 최우선, 그다음 이름순(결정적 선택 — 무작위 금지)
    csvs.sort(key=lambda p: (os.path.basename(p).lower() != "lsct_lawdcd.csv", p))
    zips.sort()
    return (csvs or zips or [None])[0]


def read_rows(path):
    """원본(zip 또는 평문 csv)에서 10컬럼 행 리스트를 뽑는다."""
    if zipfile.is_zipfile(path):
        z = zipfile.ZipFile(path)
        # 목록에는 부수 파일(Z_LURIS_LSCT_LAWDCD.xlsx)이 함께 들어올 수 있다. CSV 만 읽는다.
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            sys.exit(f"✗ zip 안의 CSV 가 1개가 아니다: {names}")
        raw = z.read(names[0])
        shown = f"{path} ({names[0]})"
    else:
        with open(path, "rb") as f:
            raw = f.read()
        shown = path

    txt = raw.decode("cp949")                       # CP949 고정 — UTF-8 로 열면 조용히 깨진다
    rd = csv.reader(io.StringIO(txt, newline=""))
    try:
        head = next(rd)
    except StopIteration:
        sys.exit("✗ 원본이 비어 있다")
    if [h.strip().lstrip("﻿") for h in head] != HEADER:
        sys.exit(f"✗ 헤더가 예상과 다르다: {head}\n  기대: {HEADER}\n  원본 포맷이 바뀌었다.")

    rows = []
    for ln, f in enumerate(rd, start=2):
        if not any(x.strip() for x in f):
            continue                                # 말미 빈 줄
        if len(f) != 10:
            sys.exit(f"✗ {ln}행 컬럼 수 {len(f)} (10 이어야 함): {str(f)[:100]}")
        rows.append([x.strip() for x in f])
    return rows, shown


def psql(sql):
    """psql 한 문장 실행. -t -A 로 값만 받는다."""
    argv = ["psql", "-v", "ON_ERROR_STOP=1", "-t", "-A", "-c", sql]
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"✗ psql 실패:\n{r.stderr}")
    return r.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?",
                    help=f"LSCT_LAWDCD.csv 또는 zip (생략 시 {STAGED} 에서 찾음)")
    ap.add_argument("--expect-rows", type=int, default=31172,
                    help="기대 데이터 행수. 다르면 적재하지 않는다 (0 = 게이트 해제)")
    args = ap.parse_args()

    os.environ.setdefault("PGHOST", "localhost")
    os.environ.setdefault("PGPORT", "5433")
    os.environ.setdefault("PGUSER", "cuvia")
    os.environ.setdefault("PGDATABASE", "cuvia")
    os.environ.setdefault("PGPASSWORD", "cuvia")

    src = args.src or find_src()
    if not src or not os.path.exists(src):
        sys.exit(f"✗ 원본을 찾지 못했다: {src or STAGED}\n"
                 f"  VWorld dsId=30505 수집이 선행돼야 한다:\n"
                 f"    python3 scripts/build-studio.py  → 소스 lawd_code_v2 수집\n"
                 f"  원본 경로를 직접 주려면: load_lawd_code_v2.py <LSCT_LAWDCD.csv>\n"
                 f"  ※ 조용히 건너뛰지 않는다 — 낡은 lawd_sgg_remap 으로 부분 치환이 나기 때문이다.")

    sha = hashlib.sha256(open(src, "rb").read()).hexdigest()
    rows, shown = read_rows(src)
    print(f"원본   : {shown}")
    print(f"크기   : {os.path.getsize(src):,} bytes")
    print(f"SHA256 : {sha}")
    print(f"         {'= T026 기준 원본(2026-08-13)' if sha == EXPECT_SHA else '≠ T026 기준 원본 (' + EXPECT_SHA + ')'}")
    print(f"행수   : {len(rows):,}")
    if args.expect_rows and len(rows) != args.expect_rows:
        sys.exit(f"✗ 행수가 기대({args.expect_rows:,})와 다르다 — 원본이 갱신됐다.\n"
                 f"  계획서 §1-4 의 대응 수치(신설 4구 79쌍)와 S3 게이트 기대값이 전부 무효다.\n"
                 f"  재측정한 뒤 재개할 것. 게이트를 의도적으로 풀려면 --expect-rows 0")

    with tempfile.TemporaryDirectory() as tmp:
        # CP949 → UTF-8 CSV. QUOTE_ALL 필수: 인용 없는 빈 필드는 COPY CSV 가 NULL 로 읽는다.
        csv_path = os.path.join(tmp, "lawd_code_v2.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f, quoting=csv.QUOTE_ALL).writerows(rows)

        sql_path = os.path.join(tmp, "load.sql")
        with open(sql_path, "w", encoding="utf-8") as f:
            f.write(SQL.replace("__CSV__", csv_path))

        r = subprocess.run(["psql", "-v", "ON_ERROR_STOP=1", "-q", "-f", sql_path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"✗ 적재 실패:\n{r.stdout}\n{r.stderr}")

    print("\n검증 (계획서 §3-3 게이트)")
    bad = 0
    for label, sql, want in CHECKS:
        got = psql(sql)
        ok = (got == want)
        bad += (not ok)
        print(f"  {'✓' if ok else '✗'} {label:<16} = {int(got):>7,}   기대 {int(want):>7,}")
    if bad:
        sys.exit(f"\n✗ 게이트 {bad}건 미달 — S3(build_incheon_remap_from_old_lawdcd.sql)로 진행하지 말 것.")
    print("\n✓ S2 통과")


if __name__ == "__main__":
    main()
