#!/usr/bin/env bash
# juso 건물도형 패치 적재 — AL_D010 이 놓친 신축을 증분 보완한다.
#
# 배경: 3D 건물 원천 AL_D010(건물통합정보)은 신개발지구 신축이 통째로 비어 있다
#       (과천지식정보타운·DX타워 등 실측). 도로명주소 건물도형은 20260801 최신이라
#       그 공백을 메운다. 폴리곤엔 층수가 없어 내비DB(match_build_*.txt)에서 가져온다.
#
# ★ 두 레이어를 함께 쓴다 (2026-08-31 실측으로 확정):
#   · TL_SGCO_RNADR_MST (건물군)  — **단지 전체가 폴리곤 하나**다. 아파트에 이걸 쓰면
#     4만㎡ 블록이 개별 동을 통째로 덮어 3D 가 뭉갠다(부천 신중동 실측 40,630㎡).
#   · TL_SGCO_RNADR_DONG (건물군내동) — **개별 동** 폴리곤. BD_MGT_SN(건물관리번호 25자리)
#     을 갖고 있어 내비DB 와 **직접 조인**된다(MST 는 5-튜플 우회가 필요).
#   전략: DONG 을 먼저 넣고, MST 는 **폴리곤 안에 동이 하나도 없는 건물군만** 넣는다.
#   (처음엔 BUL_MAN_NO 매칭으로 걸렀는데 두 레이어에서 그 값의 뜻이 달라 하나도 안 걸러졌다 —
#    부천 중흥마을 단지가 통과해 개별 동 13개를 덮었다. 그래서 공간 포함 판정으로 바꿨다.)
#   DX타워처럼 동이 없는 단일 건물은 DONG 에 없으므로 MST 에서 그대로 들어온다.
#
# dedup: 기존 building 의 bld_mgt_no 는 AL_D010 자체 28자리 키라 juso 25자리와 체계가
#        달라 관리번호 대조가 불가능하다 → **공간 판정**을 쓴다. 대표점이 기존 건물 안에
#        들어가거나(1차), 겹침 면적이 30% 이상이면(2차) 이미 있는 건물로 보고 건너뛴다.
#
# 성능: dedup 의 building 조회는 **시도 리터럴**로 파티션 프루닝을 유지한다. 상관 계산식을
#       쓰면 플래너가 파티션을 못 골라 17개를 전부 훑는다(경기 1건 30분+ 실측).
#       building 이 AL_D010 최신본(광주·전남 통합 12)으로 재적재된 뒤로는 juso 통합분도
#       그대로 12 로 넣으면 되므로 역매핑이 필요 없다.
#
#   사용법: load_building_juso.sh --mst <건물군.shp> --dong <동.shp> --navi <match_build_*.txt>
#                                 --sido 41 [--dry-run]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$HERE/_pg-env.sh"
pg_need ogr2ogr psql iconv

MST="" DONG="" NAVI="" SIDO="" DRY=""
while [ $# -gt 0 ]; do case "$1" in
  --mst)  MST="$2"; shift 2;;
  --dong) DONG="$2"; shift 2;;
  --navi) NAVI="$2"; shift 2;;
  --sido) SIDO="$2"; shift 2;;
  --dry-run) DRY=1; shift;;
  *) echo "알 수 없는 인자: $1" >&2; exit 2;;
esac; done
[ -n "$MST" ] && [ -n "$NAVI" ] && [ -n "$SIDO" ] || { echo "필수: --mst --navi --sido" >&2; exit 2; }
[ -s "$MST" ]  || { echo "✗ 건물군 SHP 없음: $MST" >&2; exit 1; }
[ -s "$NAVI" ] || { echo "✗ 내비DB 없음: $NAVI" >&2; exit 1; }

echo "=== juso 건물 패치 적재 (시도 $SIDO) ==="

# ── 1) 건물군(MST) → 스테이징 (5179 → 4326) ─────────────────────────
# .prj 미동봉이라 -s_srs 명시(Extent 로 UTM-K 확인). ogr2ogr 는 필드명을 소문자로 접는다.
echo "[1/5] 건물군 도형 → _stg_juso_mst"
PG_USE_COPY=YES ogr2ogr -f PostgreSQL "$PG_OGR" "$MST" \
  -nln _stg_juso_mst -overwrite -lco GEOMETRY_NAME=geom -lco FID=fid \
  -s_srs EPSG:5179 -t_srs EPSG:4326 -nlt MULTIPOLYGON \
  -select SIG_CD,RN_CD,BULD_SE_CD,BULD_MNNM,BULD_SLNO,BUL_MAN_NO,EFFECT_DE
psql -v ON_ERROR_STOP=1 -q -c "CREATE INDEX ON _stg_juso_mst USING GIST(geom);"
psql -v ON_ERROR_STOP=1 -q -c "CREATE INDEX ON _stg_juso_mst(sig_cd, bul_man_no);"
psql -v ON_ERROR_STOP=1 -q -c "ANALYZE _stg_juso_mst;"
echo "      $(psql -tAc 'SELECT count(*) FROM _stg_juso_mst') 건물군"

# ── 2) 건물군내동(DONG) → 스테이징 ──────────────────────────────────
echo "[2/5] 건물군내동 도형 → _stg_juso_dong"
psql -v ON_ERROR_STOP=1 -q -c "DROP TABLE IF EXISTS _stg_juso_dong;"
if [ -n "$DONG" ] && [ -s "$DONG" ]; then
  PG_USE_COPY=YES ogr2ogr -f PostgreSQL "$PG_OGR" "$DONG" \
    -nln _stg_juso_dong -overwrite -lco GEOMETRY_NAME=geom -lco FID=fid \
    -s_srs EPSG:5179 -t_srs EPSG:4326 -nlt MULTIPOLYGON \
    -select BD_MGT_SN,SIG_CD,BUL_MAN_NO,RN_CD,BULD_SE_CD,BULD_MNNM,BULD_SLNO
  psql -v ON_ERROR_STOP=1 -q -c "CREATE INDEX ON _stg_juso_dong USING GIST(geom);"
  psql -v ON_ERROR_STOP=1 -q -c "CREATE INDEX ON _stg_juso_dong(sig_cd, bul_man_no);"
  psql -v ON_ERROR_STOP=1 -q -c "ANALYZE _stg_juso_dong;"
  echo "      $(psql -tAc 'SELECT count(*) FROM _stg_juso_dong') 동"
else
  psql -v ON_ERROR_STOP=1 -q -c \
    "CREATE TABLE _stg_juso_dong(fid int, bd_mgt_sn text, sig_cd text, bul_man_no int,
                                 rn_cd text, buld_se_cd text, buld_mnnm int, buld_slno int,
                                 geom geometry(MultiPolygon,4326));"
  echo "      (동 도형 미제공 — 건물군만 사용)"
fi

# ── 3) 내비DB 건물속성 → 스테이징 ───────────────────────────────────
# 33 필드 파이프구분·CP949. F5(도로명코드12=시군구5+도로명7)·F7(지하)·F8(본번)·F9(부번)·
# F11(건물관리번호 25자리)·F12(건물명)·F13(용도)·F16(지상층수).
echo "[3/5] 내비DB → _stg_navi_bld"
psql -v ON_ERROR_STOP=1 -q <<'SQL'
DROP TABLE IF EXISTS _stg_navi_bld;
CREATE TABLE _stg_navi_bld(
  f1 text, sido text, sigungu text, emd text, rn_full text, rn_nm text,
  ug text, mnnm text, slno text, zipcode text, mgt_no text, bld_nm text,
  use_type text, hcode text, hdong text, levels_up text, levels_dn text,
  apt text, bld_cnt text, f20 text, f21 text, f22 text, f23 text, f24 text,
  cx text, cy text, ex text, ey text, f29 text, f30 text, f31 text, f32 text, f33 text);
SQL
# QUOTE 는 데이터에 없는 제어문자 — 건물명의 따옴표가 파서를 깨지 않게 한다.
psql -v ON_ERROR_STOP=1 -q -c \
  "\copy _stg_navi_bld FROM PROGRAM 'iconv -f CP949 -t UTF-8 -c \"$NAVI\"' WITH (FORMAT csv, DELIMITER '|', QUOTE E'\x01')"
psql -v ON_ERROR_STOP=1 -q -c "CREATE INDEX ON _stg_navi_bld(mgt_no);"
psql -v ON_ERROR_STOP=1 -q -c \
  "CREATE INDEX ON _stg_navi_bld(substr(rn_full,1,5), substr(rn_full,6,7), ug, mnnm, slno);"
psql -v ON_ERROR_STOP=1 -q -c "ANALYZE _stg_navi_bld;"
echo "      $(psql -tAc 'SELECT count(*) FROM _stg_navi_bld') 속성행"

# ── 4) 후보 산출 — DONG(개별 동) + MST(동 없는 건물군) ──────────────
# render_height 는 AL_D010 산식과 통일: 층수>0 이면 층수×3.3, 없으면 6.
# 기하는 판정 전에 정규화한다 — 원본에 winding order 불량이 섞여 geography 면적이 음수로
# 나오고(ogr2ogr 도 경고), MakeValid 는 자기교차에서 GeometryCollection 을 낼 수 있다.
echo "[4/5] 후보 산출 + 공간 dedup (파티션 술어: sido_cd='$SIDO')"
psql -v ON_ERROR_STOP=1 -q <<SQL
DROP TABLE IF EXISTS _stg_juso_cand;
CREATE TABLE _stg_juso_cand AS
-- (a) 개별 동 — BD_MGT_SN 으로 내비DB 직접 조인
SELECT DISTINCT ON (d.fid)
       'dong'::text                                          AS src,
       d.bd_mgt_sn                                           AS mgt_no,
       nullif(n.bld_nm,'')                                   AS name,
       nullif(n.use_type,'')                                 AS use_type,
       nullif(n.levels_up,'')::int                           AS levels,
       CASE WHEN coalesce(nullif(n.levels_up,'')::int,0) > 0
            THEN coalesce(nullif(n.levels_up,'')::int,0) * 3.3 ELSE 6 END::real AS render_height,
       ST_Multi(ST_ForcePolygonCCW(ST_CollectionExtract(ST_MakeValid(d.geom), 3)))
         ::geometry(MultiPolygon,4326)                       AS geom
  FROM _stg_juso_dong d
  LEFT JOIN _stg_navi_bld n ON n.mgt_no = d.bd_mgt_sn
 WHERE NOT ST_IsEmpty(ST_CollectionExtract(ST_MakeValid(d.geom), 3))
UNION ALL
-- (b) 건물군 중 **동이 없는 것만** — 단지형은 (a) 로 이미 들어갔다
SELECT DISTINCT ON (m.fid)
       'mst'::text, n.mgt_no,
       nullif(n.bld_nm,''), nullif(n.use_type,''),
       nullif(n.levels_up,'')::int,
       CASE WHEN coalesce(nullif(n.levels_up,'')::int,0) > 0
            THEN coalesce(nullif(n.levels_up,'')::int,0) * 3.3 ELSE 6 END::real,
       ST_Multi(ST_ForcePolygonCCW(ST_CollectionExtract(ST_MakeValid(m.geom), 3)))
         ::geometry(MultiPolygon,4326)
  FROM _stg_juso_mst m
  LEFT JOIN _stg_navi_bld n
         ON substr(n.rn_full,1,5) = m.sig_cd AND substr(n.rn_full,6,7) = m.rn_cd
        AND n.ug = m.buld_se_cd AND n.mnnm::int = m.buld_mnnm AND n.slno::int = m.buld_slno
 WHERE NOT ST_IsEmpty(ST_CollectionExtract(ST_MakeValid(m.geom), 3))
   -- 단지형 제외 — BUL_MAN_NO 는 두 레이어에서 뜻이 달라 매칭되지 않았다(실측: 부천
   -- 중흥마을 단지가 그대로 통과해 개별 동 13개를 덮었다). 그래서 **공간 포함**으로 판정한다:
   -- 이 건물군 폴리곤 안에 동 폴리곤이 하나라도 들어 있으면 그 단지는 동 단위로 이미
   -- 적재된 것이므로 건물군을 버린다. 키 의미에 의존하지 않아 원천 변화에도 안전하다.
   AND NOT EXISTS (SELECT 1 FROM _stg_juso_dong d
                    WHERE d.geom && m.geom
                      AND ST_Contains(m.geom, ST_PointOnSurface(d.geom)));
CREATE INDEX ON _stg_juso_cand USING GIST(geom);
ANALYZE _stg_juso_cand;

-- 공간 dedup 2단: (1) 대표점 포함  (2) 겹침 면적 30% 이상
DROP TABLE IF EXISTS _stg_juso_final;
CREATE TABLE _stg_juso_final AS
SELECT c.* FROM _stg_juso_cand c
 WHERE NOT EXISTS (SELECT 1 FROM building b
                    WHERE b.sido_cd = '$SIDO'
                      AND ST_Intersects(b.geom, ST_PointOnSurface(c.geom)))
   AND NOT EXISTS (SELECT 1 FROM building b
                    WHERE b.sido_cd = '$SIDO' AND b.geom && c.geom
                      AND ST_Area(ST_Intersection(b.geom, c.geom)) > 0.3 * ST_Area(c.geom));
CREATE INDEX ON _stg_juso_final USING GIST(geom);
ANALYZE _stg_juso_final;
SQL
CAND=$(psql -tAc "SELECT count(*) FROM _stg_juso_cand")
NEW=$(psql -tAc "SELECT count(*) FROM _stg_juso_final")
BYS=$(psql -tAc "SELECT string_agg(src||' '||c, ' · ') FROM (SELECT src, count(*) c FROM _stg_juso_final GROUP BY src) t")
echo "      후보 $CAND · 신규 $NEW ($BYS)"

if [ -n "$DRY" ]; then
  echo "[5/5] --dry-run — 적재 생략. 스테이징 잔류."
  exit 0
fi

# ── 5) 증분 INSERT ──────────────────────────────────────────────────
echo "[5/5] building 증분 INSERT"
BEFORE=$(psql -tAc "SELECT count(*) FROM building WHERE sido_cd='$SIDO'")
psql -v ON_ERROR_STOP=1 -q <<SQL
INSERT INTO building(sido_cd, bld_mgt_no, name, use_type, levels, render_height, geom)
SELECT '$SIDO', mgt_no, name, use_type, levels, render_height, geom
  FROM _stg_juso_final
 ON CONFLICT (sido_cd, bld_mgt_no) WHERE bld_mgt_no IS NOT NULL DO NOTHING;
SQL
AFTER=$(psql -tAc "SELECT count(*) FROM building WHERE sido_cd='$SIDO'")
echo "      시도 $SIDO: $BEFORE → $AFTER (+$((AFTER-BEFORE)))"
psql -v ON_ERROR_STOP=1 -q -c "ANALYZE building;"
psql -q -c "DROP TABLE IF EXISTS _stg_juso_mst, _stg_juso_dong, _stg_navi_bld, _stg_juso_cand, _stg_juso_final CASCADE;" >/dev/null 2>&1
echo "완료 — martin 재기동 + /dyn 캐시 무효화 필요(ADR-006)."
