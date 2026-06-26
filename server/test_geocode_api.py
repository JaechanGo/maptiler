#!/usr/bin/env python3
"""geocode-api-pg.py 단위테스트 (DB 불요 — 순수헬퍼/표기/예외 다단 모의).

대상 파일명이 하이픈(`geocode-api-pg.py`)이라 일반 import 불가 →
importlib.util.spec_from_file_location 로 모듈 핸들 확보(plan 단계0/F8).

골든셋 회귀(DB 필요, 읽기전용)는 test_golden_db.py 에서 분리 수행.
실행:  python3 -m unittest server.test_geocode_api  (또는 컨테이너 내 단독 실행)
"""
import importlib.util
import os
import sys
import unittest

# ── 모듈 로드 (하이픈 파일명 대응) ────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.environ.get("GEOCODE_MODULE", os.path.join(_HERE, "geocode-api-pg.py"))


def _load_module(path=_MOD_PATH):
    spec = importlib.util.spec_from_file_location("geocode_api_pg", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_module()
import psycopg  # 예외 클래스 주입용 (모듈이 import 성공했으면 가용)


# ── 가짜 cursor (예외 주입 / 캔드 결과) ───────────────────────────
class FakeCursor:
    """execute() 에서 지정 예외를 던지거나, 큐된 fetch 결과를 돌려주는 모의 cursor."""

    def __init__(self, raise_exc=None, fetchall_result=None, fetchone_result=None):
        self._raise = raise_exc
        self._fetchall = fetchall_result if fetchall_result is not None else []
        self._fetchone = fetchone_result
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._raise is not None:
            raise self._raise
        return self

    def fetchall(self):
        return self._fetchall

    def fetchone(self):
        return self._fetchone

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakePoolCtx:
    def __init__(self, conn, raise_on_enter=None):
        self._conn = conn
        self._raise = raise_on_enter

    def __enter__(self):
        if self._raise is not None:
            raise self._raise
        return self._conn

    def __exit__(self, *a):
        return False


class FakePool:
    """POOL 대체: connection() 진입 시 예외 또는 FakeConn(FakeCursor) 제공."""

    def __init__(self, cur=None, raise_on_enter=None):
        self._conn = FakeConn(cur or FakeCursor())
        self._raise = raise_on_enter

    def connection(self):
        return FakePoolCtx(self._conn, self._raise)


# ════════════════════════════════════════════════════════════════
# 단계1 — 상수 / 순수 헬퍼
# ════════════════════════════════════════════════════════════════
class TestSidoMaps(unittest.TestCase):
    EXPECT_FULL = {
        "11": "서울특별시", "26": "부산광역시", "27": "대구광역시", "28": "인천광역시",
        "29": "광주광역시", "30": "대전광역시", "31": "울산광역시", "36": "세종특별자치시",
        "41": "경기도", "43": "충청북도", "44": "충청남도", "46": "전라남도",
        "47": "경상북도", "48": "경상남도", "50": "제주특별자치도",
        "51": "강원특별자치도", "52": "전북특별자치도",
    }
    EXPECT_ABBR = {
        "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천",
        "광주광역시": "광주", "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종",
        "경기도": "경기", "충청북도": "충북", "충청남도": "충남", "전라남도": "전남",
        "경상북도": "경북", "경상남도": "경남", "제주특별자치도": "제주",
        "강원특별자치도": "강원", "전북특별자치도": "전북",
    }

    def test_sido_full_all_17(self):
        self.assertEqual(len(self.EXPECT_FULL), 17)
        for code, name in self.EXPECT_FULL.items():
            self.assertEqual(M.SIDO_FULL[code], name, f"SIDO_FULL[{code}]")

    def test_sido_abbr_all_17(self):
        for full, abbr in self.EXPECT_ABBR.items():
            self.assertEqual(M.SIDO_ABBR[full], abbr, f"SIDO_ABBR[{full}]")

    def test_special_self_governing_renames(self):
        # 특별자치 개편명(강원51/전북52 특별자치도, 세종36 특별자치시, 제주50 특별자치도)
        self.assertEqual(M.SIDO_FULL["51"], "강원특별자치도")
        self.assertEqual(M.SIDO_FULL["52"], "전북특별자치도")
        self.assertEqual(M.SIDO_FULL["36"], "세종특별자치시")
        self.assertEqual(M.SIDO_FULL["50"], "제주특별자치도")
        self.assertEqual(M.SIDO_ABBR["강원특별자치도"], "강원")
        self.assertEqual(M.SIDO_ABBR["전북특별자치도"], "전북")


class TestPadBcode(unittest.TestCase):
    def test_pad_8_to_10(self):
        self.assertEqual(M.pad_bcode("41190103"), "4119010300")
        self.assertEqual(M.pad_bcode("41117101"), "4111710100")

    def test_btrim_then_len8(self):
        # char(8) 공백 패딩 방어: btrim 후 len==8 이면 통과
        self.assertEqual(M.pad_bcode(" 41190103 "), "4119010300")

    def test_guard_bad_len_returns_none(self):
        self.assertIsNone(M.pad_bcode("4119010"))      # 7자
        self.assertIsNone(M.pad_bcode("411901033"))    # 9자
        self.assertIsNone(M.pad_bcode(""))
        self.assertIsNone(M.pad_bcode(None))

    def test_guard_nondigit_returns_none(self):
        self.assertIsNone(M.pad_bcode("4119010a"))


class TestJibunParse(unittest.TestCase):
    def test_parse_jibun_nums_basic(self):
        self.assertEqual(M.parse_jibun_nums("1-11 대"), (1, 11, False))
        self.assertEqual(M.parse_jibun_nums("500-1답"), (500, 1, False))
        self.assertEqual(M.parse_jibun_nums("31"), (31, 0, False))

    def test_parse_jibun_nums_san(self):
        self.assertEqual(M.parse_jibun_nums("산 12-3"), (12, 3, True))

    def test_parse_jibun_nums_int4_guard(self):
        # 6자리 초과 본번 → int4 가드로 None (오버플로 예방)
        self.assertEqual(M.parse_jibun_nums("100000-1"), (None, None, False))

    def test_parse_jibun_nums_empty(self):
        self.assertEqual(M.parse_jibun_nums(None), (None, None, False))

    def test_clean_jibun_strips_jimok(self):
        # 지목문자('답','대') 제거
        self.assertEqual(M.clean_jibun("500-1답"), "500-1")
        self.assertEqual(M.clean_jibun("1-11 대"), "1-11")
        self.assertEqual(M.clean_jibun("31 전"), "31")


class TestParse(unittest.TestCase):
    """질의 분해 — 아파트 동번호(bld_dong) 와 법정동(dong) 분리(건물명 검색축)."""

    def test_bld_dong_separated_from_legal_dong(self):
        # 'NNN동'(숫자+동)은 아파트 동번호 → bld_dong. 법정동(dong) 아님. terms 에서도 제외.
        p = M.parse("다정한마을 2105동")
        self.assertEqual(p["bld_dong"], "2105동")
        self.assertIsNone(p["dong"])
        self.assertEqual(p["terms"], ["다정한마을"])
        self.assertIsNone(p["house"])

    def test_bld_dong_short_number(self):
        # '101동' 같은 짧은 동번호도 분리
        p = M.parse("래미안 101동")
        self.assertEqual(p["bld_dong"], "101동")
        self.assertEqual(p["terms"], ["래미안"])
        self.assertIsNone(p["dong"])

    def test_legal_dong_with_jibun_regression(self):
        # 회귀: 한글 법정동 + 지번은 기존대로 (dong/house)
        p = M.parse("상동 514-8")
        self.assertEqual(p["dong"], "상동")
        self.assertEqual(p["house"], (514, 8))
        self.assertIsNone(p["bld_dong"])

    def test_legal_dong_hangul_unchanged(self):
        p = M.parse("역삼동 720")
        self.assertEqual(p["dong"], "역삼동")
        self.assertEqual(p["house"], (720, 0))
        self.assertIsNone(p["bld_dong"])

    def test_complex_name_only(self):
        # 단지명 단독 — bld_dong/dong 없음, terms 에 단지명
        p = M.parse("다정한마을")
        self.assertEqual(p["terms"], ["다정한마을"])
        self.assertIsNone(p["bld_dong"])
        self.assertIsNone(p["dong"])

    def test_road_path_regression(self):
        # 회귀: 도로명 경로 (로/길) 불변
        p = M.parse("테헤란로 152")
        self.assertEqual(p["road"], "테헤란로")
        self.assertEqual(p["house"], (152, 0))
        self.assertIsNone(p["bld_dong"])

    def test_compound_road_spaced(self):
        # 복합 도로명(상위 대로/로 + 하위 N길)을 띄어쓰면 한 도로로 병합돼야 함
        p = M.parse("과천대로 7나길")
        self.assertEqual(p["road"], "과천대로7나길")
        self.assertIsNone(p["house"])

    def test_compound_road_spaced_with_house(self):
        p = M.parse("과천대로 7나길 9")
        self.assertEqual(p["road"], "과천대로7나길")
        self.assertEqual(p["house"], (9, 0))

    def test_compound_road_already_joined(self):
        # 붙여쓴 복합 도로명은 그대로(회귀 없음)
        p = M.parse("과천대로7나길 9")
        self.assertEqual(p["road"], "과천대로7나길")
        self.assertEqual(p["house"], (9, 0))

    def test_road_house_attached_lower(self):
        # 하위도로+번지가 공백없이 붙음 '7나길9' → 도로/번지 분리
        p = M.parse("과천대로 7나길9")
        self.assertEqual(p["road"], "과천대로7나길")
        self.assertEqual(p["house"], (9, 0))

    def test_road_house_fully_attached(self):
        # 전부 붙음 '과천대로7나길9'
        p = M.parse("과천대로7나길9")
        self.assertEqual(p["road"], "과천대로7나길")
        self.assertEqual(p["house"], (9, 0))

    def test_simple_road_house_attached(self):
        # 단일 도로+번지 붙음 '테헤란로152'
        p = M.parse("테헤란로152")
        self.assertEqual(p["road"], "테헤란로")
        self.assertEqual(p["house"], (152, 0))

    def test_road_house_attached_with_sub(self):
        # 부번 포함 붙음 '백범로35-1'
        p = M.parse("백범로35-1")
        self.assertEqual(p["road"], "백범로")
        self.assertEqual(p["house"], (35, 1))


# ════════════════════════════════════════════════════════════════
# 단계2 — display_of / addr_obj / category_of / nonaddr_structure
# ════════════════════════════════════════════════════════════════
class TestDisplayAddrRoad(unittest.TestCase):
    def row(self):
        return {"sido": "서울특별시", "sigungu": "강남구", "emd": "역삼동",
                "road": "강남대로", "road_norm": "강남대로", "main_no": 396, "sub_no": 0,
                "bld": "강남역", "postal": "06236", "bcode": "1168010100",
                "haeng_dong": "역삼1동", "jibun": "역삼동 858"}

    def test_road_main_has_road_num_bld(self):
        item = {"name": "x", "kind": "addr"}
        d = M.display_of(item, self.row())
        self.assertIn("강남대로", d["main"])
        self.assertIn("396", d["main"])
        self.assertIn("강남역", d["main"])
        # sub_no=0 → '-0' 미부착
        self.assertNotIn("396-0", d["main"])

    def test_road_secondary_is_abbr_sido(self):
        d = M.display_of({"name": "x", "kind": "addr"}, self.row())
        self.assertTrue(d["secondary"].startswith("서울"))
        self.assertNotIn("서울특별시", d["secondary"])  # 보조줄은 약칭
        self.assertIn("강남구", d["secondary"])

    def test_road_full_is_official_sido(self):
        d = M.display_of({"name": "x", "kind": "addr"}, self.row())
        self.assertIn("서울특별시", d["full"])
        self.assertIn("강남대로", d["full"])


class TestDisplayAddrParcel(unittest.TestCase):
    def row(self):
        # parcel-테이블 경로 row (road 없음 → parcel 규칙)
        return {"sido": "경기도", "sigungu": "수원시 영통구", "emd": "매탄동",
                "ji_main": 1, "ji_sub": 11, "san": 0, "jibun": "1-11 대"}

    def test_parcel_main_no_jimok(self):
        d = M.display_of({"name": "경기도 수원시 영통구 매탄동 1-11", "kind": "addr", "subtype": "parcel"}, self.row())
        self.assertEqual(d["main"], "매탄동 1-11")  # 지목 '대' 미노출
        self.assertNotIn("대", d["main"])

    def test_parcel_secondary_abbr(self):
        d = M.display_of({"name": "x", "kind": "addr", "subtype": "parcel"}, self.row())
        self.assertIn("경기", d["secondary"])
        self.assertIn("수원시 영통구", d["secondary"])
        self.assertNotIn("경기도", d["secondary"])

    def test_parcel_full_official(self):
        d = M.display_of({"name": "x", "kind": "addr", "subtype": "parcel"}, self.row())
        self.assertIn("경기도", d["full"])
        self.assertIn("매탄동 1-11", d["full"])

    def test_parcel_san_label(self):
        r = self.row(); r["san"] = 1; r["ji_main"] = 12; r["ji_sub"] = 3
        d = M.display_of({"name": "x", "kind": "addr", "subtype": "parcel"}, r)
        self.assertIn("산", d["main"])
        self.assertIn("12-3", d["main"])

    def test_parcel_route_via_addr_obj_when_no_subtype(self):
        # 두 번째 지번경로(address 테이블, subtype 미설정): road/road_name 부재 → parcel 규칙
        r = {"sido": "경기도", "sigungu": "부천시 원미구", "emd": "상동",
             "jibun": "상동 500-1", "main_no": None, "sub_no": None, "road": None}
        d = M.display_of({"name": "x", "kind": "addr"}, r)
        # road 부재이므로 parcel(지번) 규칙 — '상동' 포함, 도로명 형식 아님
        self.assertIn("상동", d["full"])


class TestDisplayNonAddr(unittest.TestCase):
    def test_poi_with_category(self):
        item = {"name": "탐라의원", "kind": "poi", "subtype": "hospital",
                "category": {"primary": "보건의료", "label": "보건의료 > 병원",
                             "sub": "병원", "group": "보건의료", "path": "보건의료>병원"}}
        d = M.display_of(item, {"sido": None, "sigungu": None})
        self.assertEqual(d["main"], "탐라의원")
        # 지역 결측(osm None) → secondary 는 카테고리/유형만, 지역 생략(graceful)
        self.assertIn("병원", d["secondary"])

    def test_station_label(self):
        d = M.display_of({"name": "강남역", "kind": "station", "subtype": "railway"}, {})
        self.assertEqual(d["main"], "강남역")
        self.assertIn("역", d["secondary"] or "")  # 유형라벨(지하철역)

    def test_nonaddr_with_pip_region(self):
        # PIP 모의 주입 시 secondary 에 지역 채움
        r = {"sido": "서울특별시", "sigungu": "강남구"}
        d = M.display_of({"name": "강남역", "kind": "station", "subtype": "railway"}, r)
        self.assertIn("서울", d["secondary"])
        self.assertIn("강남구", d["secondary"])

    def test_undefined_kind_fallback(self):
        d = M.display_of({"name": "무엇", "kind": "weird_kind"}, {})
        self.assertEqual(d["main"], "무엇")
        self.assertIsNone(d["secondary"])
        self.assertEqual(d["full"], "무엇")

    def test_none_string_guard(self):
        # 'None' 문자열/None 결합 방지
        r = {"sido": "None", "sigungu": None}
        d = M.display_of({"name": "x", "kind": "place", "subtype": "village"}, r)
        self.assertNotIn("None", (d["secondary"] or ""))
        self.assertNotIn("None", d["full"])


class TestAddrObj(unittest.TestCase):
    def row(self):
        return {"sido": "서울특별시", "sigungu": "중구", "emd": "태평로1가",
                "road": "세종대로", "main_no": 110, "sub_no": 0, "bld": "서울시청",
                "postal": "04524", "haeng_dong": "명동", "bcode": "1114010300",
                "hcode": "1114055000", "jibun": "태평로1가 31"}

    def test_new_structure_fields_present(self):
        st = M.addr_obj(self.row())["structure"]
        for k in ("ri", "san", "ji_main", "ji_sub", "bld_main_no", "bld_sub_no",
                  "bld_name", "zipcode"):
            self.assertIn(k, st, f"structure missing {k}")

    def test_frozen_main_sub_no_unchanged(self):
        st = M.addr_obj(self.row())["structure"]
        self.assertEqual(st["main_no"], 110)
        self.assertEqual(st["sub_no"], 0)
        # bld_* 는 main_no/sub_no alias
        self.assertEqual(st["bld_main_no"], 110)
        self.assertEqual(st["bld_sub_no"], 0)

    def test_existing_keys_preserved(self):
        o = M.addr_obj(self.row())
        for k in ("road", "parcel", "zipcode", "bld", "structure"):
            self.assertIn(k, o)
        for k in ("sido", "sigungu", "emd", "haeng_dong", "road_name",
                  "main_no", "sub_no", "b_code", "h_code"):
            self.assertIn(k, o["structure"])
        self.assertEqual(o["structure"]["b_code"], "1114010300")

    def test_mirror_fields(self):
        st = M.addr_obj(self.row())["structure"]
        self.assertEqual(st["zipcode"], "04524")
        self.assertEqual(st["bld_name"], "서울시청")


class TestCategoryOf(unittest.TestCase):
    def test_group_path_added_keys_preserved(self):
        cat = M.category_of({"cat1": "보건의료", "cat2": "병원"})
        self.assertEqual(cat["primary"], "보건의료")
        self.assertEqual(cat["sub"], "병원")          # 보존(F15)
        self.assertEqual(cat["label"], "보건의료 > 병원")  # 보존
        self.assertEqual(cat["group"], "보건의료")     # 가산
        self.assertEqual(cat["path"], "보건의료>병원")  # 가산

    def test_subtype_only(self):
        cat = M.category_of({"cat1": None, "cat2": None, "subtype": "bus"})
        self.assertEqual(cat, {"label": "bus"})

    def test_none(self):
        self.assertIsNone(M.category_of({"cat1": None, "cat2": None, "subtype": None}))


class TestNonAddrStructure(unittest.TestCase):
    def test_pip_merge(self):
        r = {"sido": None, "sigungu": None, "emd": None}
        st = M.nonaddr_structure(r, {"sido": "서울특별시", "sigungu": "강남구"})
        s = st["structure"] if "structure" in st else st
        self.assertEqual(s["sido"], "서울특별시")
        self.assertEqual(s["sigungu"], "강남구")

    def test_self_columns_win_over_pip(self):
        r = {"sido": "경기도", "sigungu": "성남시", "emd": "정자동"}
        st = M.nonaddr_structure(r, {"sido": "서울특별시"})
        s = st["structure"] if "structure" in st else st
        self.assertEqual(s["sido"], "경기도")  # 자체 컬럼 우선


# ════════════════════════════════════════════════════════════════
# 단계4 — area_pip (모의 cursor)
# ════════════════════════════════════════════════════════════════
class TestAreaPip(unittest.TestCase):
    def test_pip_returns_region(self):
        cur = FakeCursor(fetchall_result=[
            {"level": "sido", "name": "서울특별시"},
            {"level": "sigungu", "name": "강남구"},
        ])
        out = M.area_pip(cur, 127.0, 37.5)
        self.assertEqual(out.get("sido"), "서울특별시")
        self.assertEqual(out.get("sigungu"), "강남구")

    def test_pip_empty_graceful(self):
        cur = FakeCursor(fetchall_result=[])  # admin_boundary 0행
        out = M.area_pip(cur, 127.0, 37.5)
        self.assertEqual(out, {})


# ════════════════════════════════════════════════════════════════
# 단계6 — C4 예외 다단 + _check_tables + /health degraded
# ════════════════════════════════════════════════════════════════
class TestCheckTables(unittest.TestCase):
    def test_missing_tables_returned(self):
        cur = FakeCursor(fetchall_result=[{"t": "lawd_dong"}, {"t": "parcel"}])
        missing = M._check_tables(cur)
        self.assertEqual(set(missing), {"lawd_dong", "parcel"})

    def test_all_present(self):
        cur = FakeCursor(fetchall_result=[])
        self.assertEqual(M._check_tables(cur), [])

    def test_required_tables_constant(self):
        self.assertIn("address", M.REQUIRED_TABLES)
        self.assertIn("parcel", M.REQUIRED_TABLES)
        self.assertIn("lawd_dong", M.REQUIRED_TABLES)


class _Recorder:
    """Handler._send 캡처용."""

    def __init__(self):
        self.obj = None
        self.code = None

    def __call__(self, obj, code=200):
        self.obj = obj
        self.code = code


def _run_do_get(path, raise_exc=None, cur=None):
    """실제 Handler.do_GET 을 소켓 없이 구동: POOL 모의 + _send 캡처."""
    H = M.Handler.__new__(M.Handler)
    H.path = path
    rec = _Recorder()
    H._send = rec
    saved = M.POOL
    try:
        if raise_exc is not None:
            M.POOL = FakePool(cur=FakeCursor(raise_exc=raise_exc))
        else:
            M.POOL = FakePool(cur=cur or FakeCursor())
        H.do_GET()
    finally:
        M.POOL = saved
    return rec


class TestExceptionLadder(unittest.TestCase):
    def test_operational_error_503(self):
        rec = _run_do_get("/geocode?q=강남역", raise_exc=psycopg.OperationalError("conn down"))
        self.assertEqual(rec.code, 503)
        self.assertIn("error", rec.obj)

    def test_programming_error_undefined_table_500(self):
        exc = psycopg.errors.UndefinedTable("relation parcel does not exist")
        rec = _run_do_get("/geocode?q=매탄동 1-11", raise_exc=exc)
        self.assertEqual(rec.code, 500)
        self.assertIn("error", rec.obj)

    def test_generic_exception_500_json(self):
        rec = _run_do_get("/geocode?q=x", raise_exc=RuntimeError("boom"))
        self.assertEqual(rec.code, 500)
        self.assertIn("error", rec.obj)
        # 빈바디 없음 — error 키 존재 보장
        self.assertTrue(rec.obj.get("error"))

    def test_ladder_order_operational_before_programming(self):
        # OperationalError 는 ProgrammingError 의 형제(상위 아님). 순서상 503 으로 잡혀야 함.
        self.assertFalse(issubclass(psycopg.OperationalError, psycopg.ProgrammingError))
        rec = _run_do_get("/geocode?q=x", raise_exc=psycopg.OperationalError("x"))
        self.assertEqual(rec.code, 503)


class TestHealthDegraded(unittest.TestCase):
    def test_health_degraded_when_missing(self):
        cur = FakeCursor(fetchall_result=[{"t": "admin_boundary"}])  # _check_tables 가 누락 반환
        rec = _run_do_get("/health", cur=cur)
        self.assertEqual(rec.code, 503)
        self.assertFalse(rec.obj.get("ok"))
        self.assertTrue(rec.obj.get("degraded"))
        self.assertIn("admin_boundary", rec.obj.get("missing_tables", []))


class TestContractVersion(unittest.TestCase):
    def test_constant(self):
        self.assertEqual(M.CONTRACT_VERSION, "geocode/2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
