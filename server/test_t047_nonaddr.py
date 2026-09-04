#!/usr/bin/env python3
"""T047 P1 — 부존재 질의 처리 단위시험 (DB 불요).

오염(P0)과 **무관하게 실재함이 합성 질의 278 건으로 확증된** 유일한 기능 결함이다.

  (B) 교차 카테고리 폴백 — 주소 결과가 0 건이 되면 이름 경로가 열려 `biz` 135 점이 top-1 을
      점유한다. 합성 질의에서 92.8~98.2%, **실재 주소 질의에서도 0.08%**(C0-2 전북 1 건).
      사용자에게는 "틀린 주소"가 아니라 **"엉뚱한 가게"** 로 보인다.
  (A) 근사 매칭 부재 — 실재 본번의 없는 부번을 물으면 94.6% 가 근처 지번을 제안하지 못한다.

여기서 고정하는 계약:

  T4 (B) · 주소 의도가 명백한 질의에서
       ① 주소가 하나도 없으면 → **빈 결과 + note**. 상호명을 정답처럼 내지 않는다.
       ② 주소가 있으면 → 비주소는 **주소 뒤로** 밀리고 `fallback:"nonaddr"` 로 표시된다.
       ③ 주소 의도가 없는 질의(상호 검색)는 **종전 그대로**. 이것이 회귀 방어선이다.
  T5 (A) · 없는 **부번** → 같은 **본번**의 다른 부번을 `approx:true` 로.
           없는 **본번** → **빈 결과 유지**(상승은 회귀). 본번 근사는 금지다.

## 왜 DB 없이 되는가

`geocode()` 는 커서 하나로 경로별 SQL 을 순차로 던진다. SQL 지문으로 응답을 고르는 커서를
쓰면 각 경로의 유·무를 정확히 통제할 수 있다(기존 `SeqCursor` 와 같은 수법).
표본 값은 전부 **합성**이다 — 원천 레코드는 싣지 않는다.
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.environ.get("GEOCODE_MODULE", os.path.join(_HERE, "geocode-api-pg.py"))


def _load_module(path=_MOD_PATH):
    spec = importlib.util.spec_from_file_location("geocode_api_pg", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_module()

# ── 합성 표본 ────────────────────────────────────────────────────────────
EMD = "99110250"                      # 가상 읍면동코드(실재 코드 아님)
PNU = EMD + "0" + "0" + "0638" + "0001"          # 19자리: 법정동10+대지1+본번4+부번4

def parcel_row(ji_main=638, ji_sub=1, ri_nm="가상리", san=0):
    return {"jibun": "%d-%d전" % (ji_main, ji_sub), "emd_cd": EMD,
            "pnu": EMD + "0" + "0" + "%04d" % ji_main + "%04d" % ji_sub,
            "ri_cd": "00", "ri_nm": ri_nm,
            "ji_main": ji_main, "ji_sub": ji_sub, "san": san,
            "sido": "가상도", "sigungu": "가상군", "emd": "가상면",
            "lon": 127.0, "lat": 37.0}


def biz_row(name="가상편의점"):
    return {"name": name, "kind": "biz", "subtype": None, "source": "localdata",
            "lon": 127.01, "lat": 37.01, "phone": None,
            "search_text": name, "bld": "", "sido": "가상도", "sigungu": "가상군",
            "emd": "가상면", "haeng_dong": None, "bcode": EMD + "00", "hcode": None,
            "road": None, "main_no": None, "sub_no": None, "jibun": None,
            "postal": "", "category": None}


def addr_row(name_jibun="가상면 가상리 700"):
    return {"kind": "addr", "sido": "가상도", "sigungu": "가상군", "emd": "가상면",
            "ri": "가상리", "jibun": name_jibun, "road": None, "main_no": None,
            "sub_no": None, "bld": "가상아파트 101동", "postal": "12345",
            "haeng_dong": None, "bcode": EMD + "00", "hcode": None,
            "search_text": name_jibun, "lon": 127.02, "lat": 37.02}


class GeoCursor:
    """SQL 지문으로 경로별 응답을 고르는 커서.

    `parcel` 은 (ji_main, ji_sub) → rows 사전이거나 rows 목록이다. 사전이면 WHERE 에
    ji_sub 조건이 있는지로 정확/근사 질의를 구분해 응답을 달리 준다 —
    (A) 의 "정확은 0 건, 근사는 있음" 상황을 만들려면 이 구분이 필요하다.
    """

    def __init__(self, emd_cds=(EMD,), ri_pairs=((EMD, "00"),),
                 parcel_exact=(), parcel_approx=(), jibun_rows=(),
                 bld_rows=(), name_rows=(), road_rows=()):
        self.emd_cds = list(emd_cds)
        self.ri_pairs = list(ri_pairs)
        self.parcel_exact = list(parcel_exact)
        self.parcel_approx = list(parcel_approx)
        self.jibun_rows = list(jibun_rows)
        self.bld_rows = list(bld_rows)
        self.name_rows = list(name_rows)
        self.road_rows = list(road_rows)
        self.executed = []
        self._last = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.executed.append((s, params))
        if "FROM lawd_dong WHERE emd" in s:
            self._last = "emd_cds"
        elif "FROM lawd_sigungu" in s:
            self._last = "empty"
        elif "FROM lawd_sgg_remap" in s:
            self._last = "empty"
        elif "FROM lawd_ri WHERE ri" in s:
            self._last = "ri_pairs"
        elif "FROM parcel JOIN lawd_dong" in s:
            self._last = ("parcel_exact" if "parcel.ji_sub = %s" in s
                          else "parcel_approx")
        elif "FROM admin_boundary" in s:
            self._last = "empty"
        elif "kind <> 'addr'" in s:
            self._last = "name_rows"
        elif "road_norm=%s" in s:
            self._last = "road_rows"
        elif "bld ILIKE" in s:
            self._last = "bld_rows"
        elif "search_text ILIKE" in s or "kind='addr' AND emd = %s" in s:   # 지번 폴백(2026-09-03 emd+jibun 인덱스 경로 포함)
            self._last = "jibun_rows"
        else:
            self._last = "empty"
        return self

    def fetchall(self):
        if self._last == "emd_cds":
            return [{"emd_cd": c} for c in self.emd_cds]
        if self._last == "ri_pairs":
            return [{"emd_cd": e, "ri_cd": r} for e, r in self.ri_pairs]
        return list(getattr(self, self._last, []) if self._last != "empty" else [])

    def fetchone(self):
        return None

    # do_GET 은 `con.cursor() as cur` 로 쓴다 — 실제 psycopg 커서와 같은 프로토콜이 필요하다.
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sql_of(self, tag):
        for s, _ in self.executed:
            if tag == "parcel_exact" and "FROM parcel JOIN" in s and "parcel.ji_sub = %s" in s:
                return s
            if tag == "parcel_approx" and "FROM parcel JOIN" in s and "parcel.ji_sub = %s" not in s:
                return s
        return None

    def count(self, tag):
        return sum(1 for s, _ in self.executed
                   if (tag == "parcel_approx" and "FROM parcel JOIN" in s
                       and "parcel.ji_sub = %s" not in s)
                   or (tag == "parcel_exact" and "FROM parcel JOIN" in s
                       and "parcel.ji_sub = %s" in s))


class _Base(unittest.TestCase):
    GLOBALS = ("_HAS_LAWD_RI", "_HAS_SIDO_REMAP", "_HAS_SGG_REMAP",
               "_SIDO_REMAP", "_RI_REMAP_EXC", "_SGG_REMAP", "_RI_EMDS")

    def setUp(self):
        self._saved = {k: getattr(M, k) for k in self.GLOBALS}
        # 사전은 켜진 정상 런타임을 가정한다(P0 로 보장되는 상태).
        M._HAS_LAWD_RI = True
        M._HAS_SIDO_REMAP = False
        M._HAS_SGG_REMAP = False
        M._SIDO_REMAP, M._RI_REMAP_EXC, M._SGG_REMAP = {}, {}, {}
        M._RI_EMDS = frozenset()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(M, k, v)

    def go(self, cur, q, limit=5):
        meta = {}
        out = M.geocode(cur, q, limit, meta=meta)
        return out, meta


# 주소 의도가 명백한 합성 질의 — C0-5 의 S1/S2 와 같은 형태다.
Q_EXACT = "가상도 가상군 가상면 가상리 638-1"
Q_S1 = "가상도 가상군 가상면 가상리 638-77"        # 실재 본번 · 없는 부번
Q_S2 = "가상도 가상군 가상면 가상리 90638"         # 없는 본번


# ════════════════════════════════════════════════════════════════
# T4 — (B) 교차 카테고리 폴백 차단
# ════════════════════════════════════════════════════════════════
class TestAddrIntent(_Base):

    def test_intent_true_for_dong_plus_house(self):
        p = M.parse(Q_S1)
        self.assertTrue(M.addr_intent(p))

    def test_intent_true_for_road_plus_house(self):
        self.assertTrue(M.addr_intent(M.parse("가상시 가상대로 152")))

    def test_intent_false_for_bare_business_name(self):
        """'투다리'·'양촌리' 같은 상호 단독 질의 — 여기 걸리면 POI 검색이 죽는다."""
        for q in ("투다리", "가상편의점", "강남역", "가상아파트 101동"):
            self.assertFalse(M.addr_intent(M.parse(q)), q)

    def test_intent_false_without_house(self):
        self.assertFalse(M.addr_intent(M.parse("가상면 가상리")))


class TestAddrIntentAdjacency(_Base):
    """★ T047 검수 6-1 의 한계 해소를 고정한다 (T051 — 인접성 술어).

    구 판은 이름이 ' 숫자'로 끝나는 POI 질의에서 그 숫자가 `house` 로 파싱돼 찾던 상호까지
    억제했다(배터리 60 건 중 24 건 소실). 지금 술어는 번지 토큰이 동/리/읍/면/N가/도로명
    토큰의 **직전 인접**일 때만 참이다(`parse()["house_adj"]`). 이 클래스가 그 경계를
    양방향으로 고정한다 — 오탐 해소(거짓)와 회귀 축 보존(참)을 함께.
    """

    def test_poi_name_ending_in_bare_number_no_longer_flips_intent(self):
        """T047 시절 참이었다 — 001 의 직전 토큰이 상호라 이제 거짓이다."""
        p = M.parse("가상도 가상군 가상면 주차장 3")
        self.assertEqual(p["house"], (3, 0))
        self.assertFalse(p["house_adj"])
        self.assertFalse(M.addr_intent(p))

    def test_such_poi_survives_when_no_address(self):
        """T047 검수 6-1 의 실제 피해 시나리오 — 이제 찾던 상호가 살아남는다."""
        cur = GeoCursor(parcel_exact=[], name_rows=[biz_row("주차장 3")])
        out, meta = self.go(cur, "가상도 가상군 가상면 주차장 3")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "biz")
        self.assertNotIn("note", meta)

    def test_adjacent_house_still_true(self):
        """회귀 축 — 인접형(실재 주소·S1/S2 합성 질의 형태)은 전부 참을 유지한다."""
        for q in (Q_EXACT, Q_S1, Q_S2,
                  "가상면 가상리 638-1",       # 리 직전
                  "가상동 123-4",              # 동 직전
                  "가상리 산 25-1",            # 단독 '산' 개재 — 인접성 유지
                  "가상대로 152",              # 도로명 직전
                  "가상동123-4",               # 동+번지 한 토큰
                  "가상대로152"):              # 도로+번지 한 토큰
            p = M.parse(q)
            self.assertTrue(p["house_adj"], q)
            self.assertTrue(M.addr_intent(p), q)

    def test_nonadjacent_house_false(self):
        """상호·건물동 토큰이 끼면 거짓 — 억제 게이트가 열리지 않는다."""
        for q in ("가상도 가상군 가상면 에스테틱 001",
                  "가상면 가상카페 2",
                  "가상동 101동 5"):           # 건물 동번호는 앵커가 아니다
            p = M.parse(q)
            self.assertFalse(M.addr_intent(p), q)

    def test_common_poi_suffixes_are_not_affected(self):
        """층·호점·번출구·동 접미는 house 로 파싱되지 않는다 — 현실 질의 패턴은 안전하다."""
        for q in ("가상도 가상군 가상면 가상빌딩 2층",
                  "가상도 가상군 가상면 가상커피 2호점",
                  "가상도 가상군 가상면 가상역 2번출구",
                  "가상도 가상군 가상면 가상아파트 101동"):
            self.assertFalse(M.addr_intent(M.parse(q)), q)

    def test_business_query_without_region_tokens_is_safe(self):
        """지역 토큰 없이 상호만 치면 dong 이 안 잡혀 술어가 거짓이다."""
        self.assertFalse(M.addr_intent(M.parse("주차장 3")))


class TestCrossCategoryFallback(_Base):

    def test_nonexistent_address_returns_empty_not_a_shop(self):
        """★ 표적 — 주소 0 건인데 상호가 top-1 을 채우던 그 경로."""
        cur = GeoCursor(name_rows=[biz_row(), biz_row("가상치킨")])
        out, meta = self.go(cur, Q_S2)
        self.assertEqual(out, [], "부존재 지번 질의에 상호명이 반환됐다")
        self.assertEqual(meta.get("note"), "no_address_match")
        self.assertEqual(meta.get("suppressed"), 2)

    def test_existing_address_unaffected(self):
        """회귀 방어 — 정상 질의는 아무것도 달라지지 않는다."""
        cur = GeoCursor(parcel_exact=[parcel_row()])
        out, meta = self.go(cur, Q_EXACT)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "addr")
        self.assertNotIn("note", meta)
        self.assertNotIn("fallback", out[0])

    def test_business_query_still_returns_business(self):
        """★ 회귀 방어선 — 주소 의도가 없으면 종전 그대로 상호를 낸다."""
        cur = GeoCursor(emd_cds=[], name_rows=[biz_row("투다리")])
        out, meta = self.go(cur, "투다리")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "biz")
        self.assertNotIn("note", meta)
        self.assertNotIn("fallback", out[0])

    def test_nonaddr_demoted_below_addr_and_flagged(self):
        """주소가 있으면 비주소를 버리지 않는다 — 뒤로 밀고 표시만 한다(B1+B3)."""
        cur = GeoCursor(parcel_exact=[], jibun_rows=[],
                        bld_rows=[addr_row()], name_rows=[biz_row()])
        out, meta = self.go(cur, Q_S1)
        kinds = [it["kind"] for it in out]
        self.assertEqual(kinds[0], "addr", "비주소가 주소보다 앞섰다")
        self.assertIn("biz", kinds)
        biz = [it for it in out if it["kind"] == "biz"][0]
        self.assertEqual(biz.get("fallback"), "nonaddr")
        self.assertNotIn("note", meta)

    def test_empty_result_when_nothing_at_all(self):
        cur = GeoCursor(parcel_exact=[], name_rows=[])
        out, meta = self.go(cur, Q_S2)
        self.assertEqual(out, [])
        self.assertEqual(meta.get("note"), "no_address_match")
        self.assertEqual(meta.get("suppressed"), 0)

    def test_meta_is_optional(self):
        """meta 를 안 넘기는 기존 호출부(_selftest 등)를 깨지 않는다."""
        cur = GeoCursor(name_rows=[biz_row()])
        self.assertEqual(M.geocode(cur, Q_S2, 5), [])

    def test_note_reaches_response_envelope(self):
        """note 가 응답 봉투까지 나가야 소비자가 '빈 결과'의 이유를 안다."""
        rec = _run_geocode_endpoint(GeoCursor(name_rows=[biz_row()]), Q_S2)
        self.assertEqual(rec.code, 200)
        self.assertEqual(rec.obj["results"], [])
        self.assertEqual(rec.obj["note"], "no_address_match")
        # 기존 봉투 키는 그대로다(하위 호환).
        self.assertEqual(rec.obj["contract_version"], M.CONTRACT_VERSION)
        self.assertIn("query", rec.obj)

    def test_envelope_has_no_note_on_normal_query(self):
        rec = _run_geocode_endpoint(GeoCursor(parcel_exact=[parcel_row()]), Q_EXACT)
        self.assertNotIn("note", rec.obj)


# ════════════════════════════════════════════════════════════════
# T5 — (A) 근사 매칭: 부번만, 본번은 절대 금지
# ════════════════════════════════════════════════════════════════
class TestApproxMatch(_Base):

    def test_missing_sub_falls_back_to_same_main(self):
        """★ 표적 — 실재 본번의 없는 부번(S1)."""
        cur = GeoCursor(parcel_exact=[], parcel_approx=[parcel_row(638, 1),
                                                        parcel_row(638, 2)])
        out, meta = self.go(cur, Q_S1)
        self.assertTrue(out, "근사 후보가 있는데 빈 결과를 냈다")
        self.assertEqual(out[0]["kind"], "addr")
        self.assertTrue(out[0].get("approx"), "근사 결과가 정확 매칭과 구분되지 않는다")
        self.assertEqual(meta.get("note"), "approx_jibun")

    def test_approx_constrained_to_same_main_number(self):
        """제약 확인 — 같은 emd/ri 페어 + **본번 정확 일치**. 본번은 근사하지 않는다."""
        cur = GeoCursor(parcel_exact=[], parcel_approx=[parcel_row(638, 2)])
        self.go(cur, Q_S1)
        sql = cur.sql_of("parcel_approx")
        self.assertIsNotNone(sql)
        self.assertIn("parcel.ji_main = %s", sql)
        self.assertNotIn("parcel.ji_sub = %s", sql)
        self.assertIn("parcel.emd_cd = ANY", sql)
        self.assertIn("substr(parcel.pnu,9,2)", sql)      # 리 페어 제약 유지

    def test_missing_main_stays_empty(self):
        """★ S2 는 빈 결과가 정답이다 — 본번은 절대 근사하지 않는다.

        ADR-008(f22af8b) 이후 계약: 부번 미명시 질의에도 근사 조회는 **발행**하되, WHERE 가 본번을
        정확 제약(`parcel.ji_main = %s`)하므로 없는 본번(90638)은 DB 에서 0행 → 빈 결과.
        종전 이 테스트는 '근사 조회를 아예 던지지 않는다'를 가정했고, 가짜 커서가 파라미터를 무시하고
        행을 돌려주는 탓에 운영에선 성립하는 빈 결과를 오답처럼 보였다([실측 2026-09-02] 세션 이전
        커밋 ecab565 에서도 동일 실패 = 코드 결함이 아니라 테스트가 ADR-008 을 반영하지 못한 것).
        그래서 DB 상태를 정직하게 모델링한다: 본번 90638 필지는 없다(parcel_approx=[]).
        """
        cur = GeoCursor(parcel_exact=[], parcel_approx=[])
        out, meta = self.go(cur, Q_S2)
        self.assertEqual(out, [])
        self.assertEqual(cur.count("parcel_approx"), 1, "부번 미명시 질의는 근사 조회를 1회 발행한다(ADR-008)")
        sql, params = next((s, p) for s, p in cur.executed if "parcel.ji_main = %s" in s and "parcel.ji_sub = %s" not in s)
        self.assertTrue(any(str(v) == "90638" for v in (params or ())), f"본번 90638 이 바인딩돼야 한다: {params}")

    def test_no_approx_when_exact_hits(self):
        cur = GeoCursor(parcel_exact=[parcel_row(638, 1)])
        out, _ = self.go(cur, Q_EXACT)
        self.assertEqual(cur.count("parcel_approx"), 0)
        self.assertFalse(out[0].get("approx"))

    def test_exact_address_table_hit_beats_approx(self):
        """근사는 **정확 매칭이 전부 실패한 뒤**에만 연다.

        parcel 에 정확 매칭이 없어도 address 테이블에 있으면 그쪽이 정답이다.
        근사를 앞세우면 실재 주소를 근사로 덮어쓰는 회귀가 된다.
        """
        cur = GeoCursor(parcel_exact=[], parcel_approx=[parcel_row(638, 2)],
                        jibun_rows=[addr_row("가상면 가상리 638-77")])
        out, meta = self.go(cur, Q_S1)
        self.assertEqual(cur.count("parcel_approx"), 0)
        self.assertFalse(out[0].get("approx"))
        self.assertNotIn("note", meta)

    def test_approx_scores_below_exact(self):
        """근사 결과가 정확 매칭보다 앞서면 안 된다."""
        cur = GeoCursor(parcel_exact=[], parcel_approx=[parcel_row(638, 2)],
                        bld_rows=[], name_rows=[])
        out, _ = self.go(cur, Q_S1)
        self.assertEqual(M.APPROX_JIBUN_SCORE, 160)
        self.assertLess(M.APPROX_JIBUN_SCORE, 200)
        self.assertTrue(out[0]["approx"])

    def test_approx_orders_by_distance_in_sql(self):
        """ADDR_CAP 절단은 SQL 단계에서 막아야 한다 — 가까운 부번이 먼저 잘려서는 안 된다."""
        cur = GeoCursor(parcel_exact=[], parcel_approx=[parcel_row(638, 2)])
        self.go(cur, Q_S1)
        sql = cur.sql_of("parcel_approx")
        self.assertIn("abs(parcel.ji_sub", sql)
        self.assertIn("LIMIT %d" % M.ADDR_CAP, sql)

    def test_approx_result_satisfies_addr_intent(self):
        """근사가 주소를 냈으면 (B) 의 '주소 0 건' 이 아니다 — 빈 결과로 지워지면 안 된다."""
        cur = GeoCursor(parcel_exact=[], parcel_approx=[parcel_row(638, 2)],
                        name_rows=[biz_row()])
        out, meta = self.go(cur, Q_S1)
        self.assertTrue(out)
        self.assertEqual(out[0]["kind"], "addr")
        self.assertNotEqual(meta.get("note"), "no_address_match")


# ── /geocode 봉투 구동 헬퍼 ──────────────────────────────────────────
class _Recorder:
    def __init__(self):
        self.obj = None
        self.code = None

    def __call__(self, obj, code=200):
        self.obj = obj
        self.code = code


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Ctx:
    def __init__(self, cur):
        self._cur = cur

    def __enter__(self):
        return _Conn(self._cur)

    def __exit__(self, *a):
        return False


class _Pool:
    def __init__(self, cur):
        self._cur = cur

    def connection(self, timeout=None):
        return _Ctx(self._cur)


def _run_geocode_endpoint(cur, q):
    import urllib.parse
    H = M.Handler.__new__(M.Handler)
    H.path = "/geocode?" + urllib.parse.urlencode({"q": q, "limit": 5})
    rec = _Recorder()
    H._send = rec
    saved = M.POOL
    try:
        M.POOL = _Pool(cur)
        H.do_GET()
    finally:
        M.POOL = saved
    return rec


if __name__ == "__main__":
    unittest.main(verbosity=2)
