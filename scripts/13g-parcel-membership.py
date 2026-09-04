#!/usr/bin/env python3
r"""T029 F3 — 개선 전후 '필지 소속 일치율' 측정 (완료조건 #4).

방법: API 실측 + 코드 원문 SQL 재현 하이브리드.
  · /reverse 응답은 도로명축 주소점의 **좌표를 노출하지 않는다**(ROAD_SIDE_STRUCT_KEYS 에
    b_code 도 없어 역식별도 불가). 그래서 소속판정에 쓸 geom 은 SQL 로 얻어야 한다.
  · 그 SQL 은 근사가 아니라 server/geocode-api-pg.py 의 addr_at() / parcel_at() /
    road_at_parcel() **원문을 그대로 옮긴 것**이다(bbox 0.035° + ST_DWithin 2500m,
    WITH cand AS MATERIALIZED 포함).
  · 어느 경로가 발화했는지는 추정하지 않고 API 의 address_source 를 그대로 따른다.
  · 재현 충실성은 API 응답 도로명 문자열 ↔ 재현 행 도로명 문자열 대조로 실증한다.
    이 대조는 행 동일성의 **필요조건이지 충분조건이 아니다**(같은 서명의 별개 address 행이
    원리적으로 가능하다) — 지위와 한계는 docs/geocode-pnu-join-verification.md §8.2 에 있다.

before = baseline(1782bf4) 컨테이너 :8082 (t029-geocode-pg-base)
after  = 현행 컨테이너 :8082 (t029-geocode-pg)

결과·해석·한계는 docs/geocode-pnu-join-verification.md §8 에 있다.

── 실행 전제 ────────────────────────────────────────────────────────────────
- 컨테이너 2개가 네트워크 server_cuvia 에 붙어 있을 것. 배선은
  docs/geocode-verify-container.md, baseline 컨테이너 추가 절차는
  docs/geocode-pnu-join-verification.md §8.6 에 있다.
      after  t029-geocode-pg      ← 현행 워크트리 server/geocode-api-pg.py
      before t029-geocode-pg-base ← git show 1782bf4:server/geocode-api-pg.py
  이 스크립트는 두 컨테이너를 **이름으로** 부르므로 같은 네트워크 안에서 실행해야 한다.
- DATABASE_URL  필수 (psycopg v3). 예: postgresql://cuvia:cuvia@postgis:5432/cuvia
- F3_SAMPLE     입력 CSV (기본 /tmp/sample-seoul-1000.csv)
- F3_OUT        출력 CSV (기본 /tmp/f3-result.csv). 집계 13h 가 같은 값을 읽는다.

── 표본 추출 SQL ────────────────────────────────────────────────────────────
docs/geocode-pnu-join-verification.md §8.1 의 규칙(좌표원 ST_PointOnSurface, 서울,
ORDER BY md5(pnu), 지목 층화 없음, 1,000건)을 옮긴 것이다. psql 로 한 번 실행해
F3_SAMPLE 을 만든다. **ORDER BY md5(pnu) 는 결정적이라 같은 DB 에서 같은 1,000건이 다시
나온다** — 그래서 결과 CSV 도 표본 CSV 도 리포에 두지 않는다.

    psql "$DATABASE_URL" -c "\copy (
      SELECT pnu,
             jibun,
             right(jibun, 1)               AS jimok,
             ST_X(ST_PointOnSurface(geom)) AS lon,
             ST_Y(ST_PointOnSurface(geom)) AS lat
        FROM parcel
       WHERE sido_cd = '11'
       ORDER BY md5(pnu)
       LIMIT 1000
    ) TO '/tmp/sample-seoul-1000.csv' WITH (FORMAT csv, HEADER)"

  · sido_cd 가 §8.1 이 말한 sido 다 — parcel 의 파티션키(= left(pnu,2))라 parcel_11 로
    프루닝된다(scripts/postgis/schema/20-parcel.sql).
  · parcel 에 지목 컬럼은 없다. jibun 이 '[산 ]본번[-부번]지목' 표기라 끝 한 글자가 지목이다
    (scripts/postgis/schema/21-parcel-jibun.sql).
  · parcel.geom_pt 는 같은 ST_PointOnSurface(geom) 를 미리 채워 둔 컬럼이므로, 백필돼
    있으면 ST_X(geom_pt)/ST_Y(geom_pt) 로 바꿔도 같은 좌표가 나온다.

  ※ 원 추출은 psql 로 따로 실행돼 스크립트에 남지 않았다. 위 SQL 은 §8.1 의 규칙대로
    복원한 것이며, 실제 sample-seoul-1000.csv 와 대조해 정렬키(md5(pnu) 오름차순 1000/1000)·
    시도(pnu 전건 '11' 시작)·지목 파생(jimok == jibun 끝 한 글자 1000/1000)·지목 구성
    (대 736 · 도 164 · 임 22 …)이 모두 일치함을 확인했다.
"""
import csv
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg.rows import dict_row

DSN = os.environ["DATABASE_URL"]
BEFORE_BASE = "http://t029-geocode-pg-base:8082"
AFTER_BASE = "http://t029-geocode-pg:8082"
SAMPLE = os.environ.get("F3_SAMPLE", "/tmp/sample-seoul-1000.csv")
OUT = os.environ.get("F3_OUT", "/tmp/f3-result.csv")

# ── 코드 원문 SQL (server/geocode-api-pg.py) ────────────────────────────────
# parcel_at() L853-859. ri_sel/ri_join 은 셀렉트 컬럼과 LEFT JOIN 이라 선택되는 행에
# 영향이 없으므로 생략했다. 행을 좌우하는 lawd_dong INNER JOIN·WHERE·ORDER BY 는 그대로다.
SQL_PARCEL_AT = (
    "SELECT parcel.pnu AS pnu, parcel.jibun AS jibun, parcel.ji_main AS ji_main,"
    " parcel.ji_sub AS ji_sub, parcel.san AS san"
    " FROM parcel JOIN lawd_dong ld ON ld.emd_cd = parcel.emd_cd"
    " WHERE ST_Contains(parcel.geom, ST_SetSRID(ST_MakePoint(%s,%s),4326))"
    " ORDER BY parcel.san, parcel.ji_main, parcel.ji_sub LIMIT 1")

# addr_at() L813-822 원문
SQL_ADDR_AT = (
    "SELECT s.id, s.road, s.main_no, s.sub_no, s.postal, s.bcode, s.bd_mgt_sn,"
    " ST_Distance(s.geom::geography,"
    " ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography) AS knn_dist_m"
    " FROM (SELECT * FROM address"
    "       WHERE kind='addr' AND geom IS NOT NULL"
    "         AND geom && ST_Expand(ST_SetSRID(ST_MakePoint(%s,%s),4326), 0.035)"
    "         AND ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, 2500)"
    "       ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s,%s),4326) LIMIT 1) s")

# road_at_parcel() L917-926 원문
SQL_ROAD_AT_PARCEL = (
    "WITH cand AS MATERIALIZED ("
    " SELECT a.*, ST_Distance(a.geom::geography,"
    " ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography) AS join_dist_m"
    " FROM address a"
    " WHERE a.kind = 'addr'"
    " AND a.bcode || substr(a.bd_mgt_sn, 11, 9) = %s"
    " )"
    " SELECT c.id, c.road, c.main_no, c.sub_no, c.postal, c.join_dist_m FROM cand c"
    " ORDER BY c.geom <-> ST_SetSRID(ST_MakePoint(%s,%s),4326)"
    " LIMIT 1")

# 소속판정: ST_Contains(그 필지 geom, 응답 주소점 geom) — 검수자 정의 그대로.
SQL_CONTAINS = (
    "SELECT ST_Contains(p.geom, a.geom) AS inside,"
    " ST_Distance(p.geom::geography, a.geom::geography) AS gap_m"
    " FROM parcel p, address a WHERE p.pnu = %s AND a.id = %s")


def call(base, lon, lat):
    url = f"{base}/reverse?lon={lon}&lat={lat}"
    err = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            err = e
    return {"_error": f"{type(err).__name__}: {err}"}


def fetch_api(row):
    lon, lat = row["lon"], row["lat"]
    return call(BEFORE_BASE, lon, lat), call(AFTER_BASE, lon, lat)


def road_sig(resp):
    """응답에서 도로명축 서명(도로명·본번·부번·우편)을 뽑는다. 교차검증용."""
    a = (resp or {}).get("address") or {}
    st = a.get("structure") or {}
    return (st.get("road_name"), st.get("main_no"), st.get("sub_no"), st.get("zipcode"))


def row_sig(r):
    if not r:
        return (None, None, None, None)
    return (r.get("road"), r.get("main_no"), r.get("sub_no"), r.get("postal"))


def main():
    rows = list(csv.DictReader(open(SAMPLE, encoding="utf-8")))
    print(f"표본 {len(rows)}건 — API 호출 시작", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(max_workers=8) as ex:
        api = list(ex.map(fetch_api, rows))
    print("API 호출 완료 — SQL 재현 시작", file=sys.stderr, flush=True)

    out = []
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        conn.execute("SET statement_timeout = '120s'")
        with conn.cursor() as cur:
            for i, (row, (rb, ra)) in enumerate(zip(rows, api)):
                lon, lat = float(row["lon"]), float(row["lat"])
                src_pnu = row["pnu"]

                rec = {
                    "pnu": src_pnu, "jimok": row["jimok"], "lon": lon, "lat": lat,
                    "api_err_before": rb.get("_error", ""), "api_err_after": ra.get("_error", ""),
                    "address_source": ra.get("address_source", ""),
                }

                # PIP 필지
                cur.execute(SQL_PARCEL_AT, (lon, lat))
                pip = cur.fetchone()
                rec["pip_pnu"] = pip["pnu"] if pip else ""
                rec["pip_is_src"] = int(bool(pip) and pip["pnu"] == src_pnu)

                # before 주소점 = KNN (baseline 은 도로명축이 항상 addr_at)
                cur.execute(SQL_ADDR_AT, (lon, lat, lon, lat, lon, lat, lon, lat))
                knn = cur.fetchone()
                rec["knn_id"] = knn["id"] if knn else ""
                rec["knn_dist_m"] = round(float(knn["knn_dist_m"]), 3) if knn else ""

                # after 주소점 = API 가 알려준 경로 그대로
                join = None
                if rec["address_source"] == "pip_key" and pip:
                    cur.execute(SQL_ROAD_AT_PARCEL, (lon, lat, pip["pnu"], lon, lat))
                    join = cur.fetchone()
                rec["join_id"] = join["id"] if join else ""
                rec["join_dist_m"] = round(float(join["join_dist_m"]), 3) if join else ""

                after_row = join if join else knn

                # 소속판정 — 기준 필지는 좌표를 낸 원본 필지(task.md 정의)
                for tag, r in (("before", knn), ("after", after_row)):
                    if not r:
                        rec[f"{tag}_inside"] = ""
                        rec[f"{tag}_gap_m"] = ""
                        continue
                    cur.execute(SQL_CONTAINS, (src_pnu, r["id"]))
                    c = cur.fetchone()
                    rec[f"{tag}_inside"] = int(bool(c and c["inside"]))
                    rec[f"{tag}_gap_m"] = round(float(c["gap_m"]), 3) if c else ""

                # 교차검증 — API 응답 도로명축 ↔ 재현 행 도로명축
                rec["xcheck_before"] = int(road_sig(rb) == row_sig(knn))
                rec["xcheck_after"] = int(road_sig(ra) == row_sig(after_row))
                rec["road_changed"] = int(road_sig(rb) != road_sig(ra))

                out.append(rec)
                if (i + 1) % 100 == 0:
                    print(f"  {i+1}/{len(rows)}", file=sys.stderr, flush=True)

    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    print(f"완료 → {OUT} ({len(out)}행)", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
