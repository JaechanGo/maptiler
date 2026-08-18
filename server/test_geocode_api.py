#!/usr/bin/env python3
"""geocode-api-pg.py 단위테스트 (DB 불요 — 순수헬퍼/표기/예외 다단 모의).

대상 파일명이 하이픈(`geocode-api-pg.py`)이라 일반 import 불가 →
importlib.util.spec_from_file_location 로 모듈 핸들 확보(plan 단계0/F8).

골든셋 회귀(DB 필요, 읽기전용)는 test_golden_db.py 에서 분리 수행.
실행:  python3 -m unittest server.test_geocode_api  (또는 컨테이너 내 단독 실행)
"""
import importlib.util
import os
import re
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

    def test_zipcode_5digit_standalone(self):
        # 5자리 단독 = 신우편번호
        p = M.parse("06236")
        self.assertEqual(p["zipcode"], "06236")
        self.assertIsNone(p["house"])
        self.assertEqual(p["terms"], [])

    def test_zipcode_with_road_promoted_to_house(self):
        # 도로 동반 5자리는 우편번호가 아니라 번지로 승격
        p = M.parse("강남대로 10524")
        self.assertEqual(p["road"], "강남대로")
        self.assertEqual(p["house"], (10524, 0))
        self.assertIsNone(p["zipcode"])

    def test_no_zipcode_on_normal_query(self):
        p = M.parse("테헤란로 152")
        self.assertIsNone(p["zipcode"])
        p2 = M.parse("상동 514-8")
        self.assertIsNone(p2["zipcode"])

    # ── task-014 리(里) 토큰 분리 (변경 ③ / 결정 C = R-6 게이팅) ──────
    # dong 은 현행 그대로 '첫 매칭' 이고, ri 는 dong 이 이미 잡힌 뒤에만 채택된다.
    # 이 게이팅이 없으면 '양촌리'(상호)·'투다리'(체인점) 같은 단독 리 토큰이 사전 조회를
    # 유발해 POI 경로를 가로챈다(T-9·T-10 이 그 회귀를 잠근다).

    def test_ri_token_separated_from_emd(self):
        # T-1: 읍면(dong) 이 먼저 잡힌 뒤의 '리' 토큰은 ri 로 분리된다.
        p = M.parse("가평군 청평면 청평리 432")
        self.assertEqual(p["dong"], "청평면")
        self.assertEqual(p["ri"], "청평리")
        self.assertEqual(p["house"], (432, 0))

    def test_ri_alone_keeps_current_dong(self):
        # T-2: 리 단독 질의는 현행대로 dong 에 들어간다(무회귀 — 기준 2).
        p = M.parse("청평리 432-11")
        self.assertEqual(p["dong"], "청평리")
        self.assertIsNone(p["ri"])
        self.assertEqual(p["house"], (432, 11))

    def test_ri_token_with_sido_sigungu(self):
        # T-3: 시도·시군구 토큰이 앞에 붙어도 읍(dong) → 리(ri) 순서로 잡힌다.
        p = M.parse("강원 춘천 신북읍 천전리 300")
        self.assertEqual(p["dong"], "신북읍")
        self.assertEqual(p["ri"], "천전리")
        self.assertEqual(p["house"], (300, 0))

    def test_emd_dong_query_has_no_ri(self):
        # T-4: 도시(동) 질의는 ri 가 붙지 않는다(기준 6 회귀 방어).
        p = M.parse("서울 동대문구 이문동 100")
        self.assertEqual(p["dong"], "이문동")
        self.assertIsNone(p["ri"])

    def test_road_path_unaffected_by_ri(self):
        # T-5: 도로명 경로는 리 변경의 영향을 받지 않는다.
        p = M.parse("테헤란로 152")
        self.assertEqual(p["road"], "테헤란로")
        self.assertIsNone(p["ri"])
        self.assertIsNone(p["dong"])

    def test_parse_keys_are_additive(self):
        # T-6: 기존 7키 보존 + 'ri' 가산(계약 additive).
        p = M.parse("가평군 청평면 청평리 432")
        for k in ("road", "house", "terms", "dong", "san", "bld_dong", "zipcode"):
            self.assertIn(k, p, f"기존 키 {k} 소실")
        self.assertIn("ri", p)

    def test_ri_name_colliding_with_biz_not_hijacked(self):
        # T-9(R-6 오탐 회귀): 상호와 겹치는 리 이름 단독 질의는 ri 를 만들지 않는다
        #                    → 사전 조회 자체가 일어나지 않는다.
        for q in ("양촌리", "용산리", "금곡리"):
            p = M.parse(q)
            self.assertIsNone(p["ri"], f"{q}: ri 오탐")
            self.assertIsNone(p["house"], f"{q}: house 오탐")

    def test_poi_query_unchanged(self):
        # T-10(R-6 오탐 회귀): '투다리'(체인점)도 ri 미생성 — 실서버 baseline
        #                     8건·source=osm 유지의 코드측 전제.
        p = M.parse("투다리")
        self.assertIsNone(p["ri"])
        self.assertIsNone(p["house"])

    # ── task-020 무공백 접미+번지 분해(P2) / 괄호·중복 정규화(P1·P3) ──
    # 번지 regex 가 '순수 숫자 토큰'만 보던 탓에 '성동리263-8' 처럼 접미(동·리·읍·면·가)와
    # 번지가 붙으면 house 가 비고, geocode() 의 두 지번 경로(649·762)가 동시에 skip 돼
    # POI 경로로 낙하했다 — 매칭 POI 가 없으면 무결과(14건), 있으면 자신만만한 오답.

    def test_ri_jibun_no_space(self):
        # T1: 리(里)+번지 무공백 — 무결과 14건의 원형.
        p = M.parse("경기도 김포시 월곶면 성동리263-8")
        self.assertEqual(p["house"], (263, 8))
        self.assertEqual(p["dong"], "월곶면")
        self.assertEqual(p["ri"], "성동리")

    def test_ri_jibun_no_space_main_only(self):
        # T2: 부번 없는 무공백 리+본번.
        p = M.parse("충청남도 논산시 은진면 연서리450")
        self.assertEqual(p["house"], (450, 0))
        self.assertEqual(p["dong"], "은진면")
        self.assertEqual(p["ri"], "연서리")

    def test_dong_jibun_no_space(self):
        # T3: 동(洞)+번지 무공백 — 낙하 후 POI 오답('청운동1' → 카페 청운동 108-5)의 원인.
        p = M.parse("서울특별시 종로구 청운동1")
        self.assertEqual(p["house"], (1, 0))
        self.assertEqual(p["dong"], "청운동")

    def test_ga_jibun_no_space(self):
        # T4: 'N가'(종로1가) 접미 — 접미 앞 숫자가 있어도 분해된다.
        p = M.parse("서울특별시 종로구 종로1가15")
        self.assertEqual(p["house"], (15, 0))
        self.assertEqual(p["dong"], "종로1가")

    def test_road_whole_duplicate_collapsed(self):
        # T5: 원본 엑셀에 도로명이 통짜로 두 번 들어간 행(NO1 15,250m 이탈).
        #     누적 road 가 '방내시장길방내시장길' 이 되어 0건 → 지번경로로 조용히 강등됐다.
        p = M.parse("강원특별자치도 홍천군 내면 방내시장길 32 방내시장길 32")
        self.assertEqual(p["road"], "방내시장길")
        self.assertEqual(p["house"], (32, 0))

    def test_paren_dong_split(self):
        # T6: 괄호 결합 '149(맥금동)' — ct 정규화가 '149맥금동' 을 통째로 dong 에 넣어
        #     house 가 비고 도로명만으로 엉뚱한 번지(장터고개길 9)를 잡았다(683m).
        p = M.parse("경기도 파주시 장터고개길 149(맥금동)")
        self.assertEqual(p["house"], (149, 0))
        self.assertEqual(p["dong"], "맥금동")
        self.assertEqual(p["road"], "장터고개길")

    def test_san_token_preserved(self):
        # T7: 산(山) 명시 질의는 P2/P4 도입 후에도 그대로 산 필지를 가리킨다.
        p = M.parse("경남 창원시 의창구 북면 감계리 산163")
        self.assertIs(p["san"], True)
        self.assertEqual(p["house"], (163, 0))
        self.assertEqual(p["dong"], "북면")
        self.assertEqual(p["ri"], "감계리")


class TestParseNoRegression(unittest.TestCase):
    """task-020 P1~P3 오탐 가드 — 무공백 분해가 기존 파스를 건드리지 않음을 잠근다.

    P2 의 접미 regex 는 접미 앞에 한글 1자 이상을 요구한다(`101동5` 같은 건물동+숫자 보호).
    삽입 위치도 계약이다 — 로/길 규칙 **뒤**여야 `검단리1길` 이 도로로 먼저 소비된다.
    """

    def test_g1_spaced_dong_jibun_unchanged(self):
        # G1: 공백 정상형은 무변화(P2 는 무공백 토큰에만 발화).
        p = M.parse("서울특별시 종로구 청운동 1")
        self.assertEqual(p["dong"], "청운동")
        self.assertEqual(p["house"], (1, 0))
        self.assertIsNone(p["ri"])

    def test_g2_ri_shaped_road_not_hijacked(self):
        # G2: '검단리1길' 은 리가 아니라 도로다. P2 가 로/길 규칙보다 앞서면 '검단리'+1 로 쪼개진다.
        p = M.parse("화성시 향남읍 검단리1길 5")
        self.assertEqual(p["road"], "검단리1길")
        self.assertEqual(p["dong"], "향남읍")
        self.assertIsNone(p["ri"])
        self.assertEqual(p["house"], (5, 0))

    def test_g3_bld_dong_preserved(self):
        # G3: 아파트 동번호는 bld_dong 유지 + house 오생성 금지.
        p = M.parse("타워팰리스 101동")
        self.assertEqual(p["bld_dong"], "101동")
        self.assertIsNone(p["house"])
        p2 = M.parse("우동 1435 아이파크 2105동")
        self.assertEqual(p2["bld_dong"], "2105동")
        self.assertEqual(p2["house"], (1435, 0))

    def test_g4_bld_dong_with_number_not_jibun(self):
        # G4: '101동5'(건물동+호수)는 번지가 아니다 — 접미 앞 한글 요구 가드가 잡는다.
        p = M.parse("도곡동 101동5")
        self.assertEqual(p["dong"], "도곡동")
        self.assertIsNone(p["house"])

    def test_g5_compound_road_spaced(self):
        # G5: 복합 도로명 누적(과천대로7나길 계열) 무회귀.
        p = M.parse("판교로 227번길 8")
        self.assertEqual(p["road"], "판교로227번길")
        self.assertEqual(p["house"], (8, 0))

    def test_g6_bare_ri_name_still_poi(self):
        # G6: 상호와 겹치는 단독 리명은 여전히 번지를 만들지 않는다(R-6 게이팅 불변).
        for q in ("양촌리", "투다리 강남점"):
            p = M.parse(q)
            self.assertIsNone(p["house"], f"{q}: house 오탐")
            self.assertIsNone(p["ri"], f"{q}: ri 오탐")

    def test_g7_spaced_jibun_battery_unchanged(self):
        # G7: 공백 정상형 지번 표본 — 기존 파스 결과 완전 불변.
        for q, dong, ri, house in (
            ("경기도 수원시 영통구 매탄동 1-11", "매탄동", None, (1, 11)),
            ("세종특별자치시 조치원읍 죽림리 245-5", "조치원읍", "죽림리", (245, 5)),
            ("청평리 432-11", "청평리", None, (432, 11)),
            ("서울 강남구 역삼동 736-1", "역삼동", None, (736, 1)),
        ):
            p = M.parse(q)
            self.assertEqual(p["dong"], dong, q)
            self.assertEqual(p["ri"], ri, q)
            self.assertEqual(p["house"], house, q)

    def test_g8_trailing_punctuation_token(self):
        # G8: 꼬리 마침표 단독 토큰은 이미 폐기되지만, 토큰 양끝 부호 제거(P1b) 후에도 결과 동일.
        p = M.parse("광주광역시 광산구 신운남길 46 .")
        self.assertEqual(p["road"], "신운남길")
        self.assertEqual(p["house"], (46, 0))

    def test_g9_partial_road_duplicate_not_over_collapsed(self):
        # G9: 정수배 완전 반복만 축약한다 — 비정수배 부분중복은 누적 멱등(D3-R3)이 처리하고,
        #     정상 도로명이 잘려나가지 않아야 한다.
        self.assertEqual(M.parse("한림상로 100")["road"], "한림상로")
        self.assertEqual(M.parse("테헤란로 152")["road"], "테헤란로")


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

    # ── task-020 P5: 리(里) 표시 주입 ─────────────────────────────
    # parcel 경로는 emd(읍·면)까지만 조립해 리를 통째로 떨궜다. '월곶면 263-8' 은
    # 같은 읍·면 안에서 리가 다른 필지와 구별되지 않아 왕복 검증에서 3km 오선택으로 보였다.

    def test_ri_shown_between_emd_and_jibun(self):
        # T8: ri 가 있으면 읍·면과 번지 사이에 들어간다.
        r = {"sido": "경기도", "sigungu": "김포시", "emd": "월곶면", "ri": "성동리",
             "ji_main": 263, "ji_sub": 8, "san": 0, "jibun": "263-8 답"}
        d = M.display_of({"name": "x", "kind": "addr", "subtype": "parcel"}, r)
        self.assertEqual(d["main"], "월곶면 성동리 263-8")
        self.assertEqual(d["full"], "경기도 김포시 월곶면 성동리 263-8")

    def test_ri_absent_key_leaves_output_unchanged(self):
        # T9(회귀 가드): ri 키가 없는 기존 row 는 출력 무변화 — r.get("ri") 는 None.
        d = M.display_of({"name": "x", "kind": "addr", "subtype": "parcel"}, self.row())
        self.assertEqual(d["main"], "매탄동 1-11")
        self.assertEqual(d["full"], "경기도 수원시 영통구 매탄동 1-11")

    def test_ri_none_value_leaves_output_unchanged(self):
        # T9': ri 키가 있으나 None 이어도 동일(빈 토큰 삽입 금지 — 공백 2개 방지).
        r = self.row(); r["ri"] = None
        d = M.display_of({"name": "x", "kind": "addr", "subtype": "parcel"}, r)
        self.assertEqual(d["main"], "매탄동 1-11")
        self.assertEqual(d["full"], "경기도 수원시 영통구 매탄동 1-11")

    def test_parcel_str_untouched_by_ri_injection(self):
        # G8: parcel_str 은 jibun 컬럼 기반이라 이미 리를 포함 — P5 가 건드리지 않음을 증명.
        r = {"sido": "세종특별자치시", "sigungu": None, "emd": "조치원읍",
             "road": "충현1길", "main_no": 60, "sub_no": 0, "bld": None,
             "postal": "30034", "jibun": "조치원읍 죽림리 245-5"}
        self.assertEqual(M.parcel_str(r), "세종특별자치시 조치원읍 죽림리 245-5")


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

    # ── task-014 리(里) 통과 (변경 ② / 결정 A = R-1) ────────────────
    def ri_row(self):
        # 리가 있는 농촌 주소 행(address.ri 는 10-base.sql 에 이미 존재하는 컬럼).
        r = self.row()
        r.update({"sido": "경기도", "sigungu": "가평군", "emd": "청평면",
                  "ri": "청평리", "bcode": "4182032500", "jibun": "청평면 청평리 432-11"})
        return r

    def test_ri_passthrough_from_address_row(self):
        # T-7: address.ri 값이 structure.ri 로 그대로 통과한다(하드코딩 None 제거).
        st = M.addr_obj(self.ri_row())["structure"]
        self.assertEqual(st["ri"], "청평리")

    def test_b_code_unchanged_when_ri_present(self):
        # T-8(R-1 회귀 잠금): 리가 채워져도 b_code 의미는 불변 — 끝 2자리는 '00'.
        st = M.addr_obj(self.ri_row())["structure"]
        self.assertEqual(st["b_code"], "4182032500")
        self.assertTrue(st["b_code"].endswith("00"))

    def test_ri_missing_column_is_none(self):
        # T-11: 'ri' 키가 없는 구스키마 row 도 예외 없이 None(_g fail-safe).
        r = self.row()
        self.assertNotIn("ri", r)
        st = M.addr_obj(r)["structure"]
        self.assertIsNone(st["ri"])


class TestLegacyAddrStrings(unittest.TestCase):
    """addr_str/road_str/parcel_str — 지역토큰 결측 시 'None' 누출 금지.

    address 적재(load_geocode.py)가 CSV 경유라 빈 시군구가 NULL 로 들어간다(세종 전역
    55,846건). f-string 직결이던 세 함수는 그 NULL 을 'None' 문자열로 박았다
    ('세종특별자치시 None 한누리대로 2130'). display_of 와 동일하게 _s 가드로 토큰을 생략한다.

    road_str 만 계약이 다르다 — 도로명주소 표기 규정상 읍·면은 본문에 넣고 동(洞)은 뺀다(de15349).
    """
    def sejong(self):
        return {"sido": "세종특별자치시", "sigungu": None, "emd": "조치원읍",
                "road": "충현1길", "main_no": 60, "sub_no": 0, "bld": None,
                "postal": "30034", "jibun": "조치원읍 죽림리 245-5"}

    def normal(self):
        return {"sido": "충청북도", "sigungu": "청주시 상당구", "emd": "미원면",
                "road": "가양길", "main_no": 2, "sub_no": 0, "bld": None,
                "postal": "28198", "jibun": "미원면 가양리 332"}

    def dong(self):
        # 동(洞) 지역 — 도로명주소 본문에는 법정동을 넣지 않는다(읍·면과 계약이 다름).
        return {"sido": "서울특별시", "sigungu": "강남구", "emd": "역삼동",
                "road": "강남대로", "main_no": 396, "sub_no": 0, "bld": None,
                "postal": "06236", "jibun": "역삼동 858"}

    def test_sejong_no_none_token(self):
        r = self.sejong()
        for fn in (M.addr_str, M.road_str, M.parcel_str):
            self.assertNotIn("None", fn(r), f"{fn.__name__} 에 'None' 누출")

    def test_sejong_single_space(self):
        r = self.sejong()                                  # 시군구 자리는 생략(공백 2개도 금지)
        self.assertEqual(M.addr_str(r), "세종특별자치시 조치원읍 충현1길 60")
        self.assertEqual(M.road_str(r), "세종특별자치시 조치원읍 충현1길 60")
        self.assertEqual(M.parcel_str(r), "세종특별자치시 조치원읍 죽림리 245-5")

    def test_region_present_unchanged(self):
        r = self.normal()                                  # 회귀 방지 — 시군구 있는 표기(읍·면은 road 에도 포함)
        self.assertEqual(M.addr_str(r), "충청북도 청주시 상당구 미원면 가양길 2")
        self.assertEqual(M.road_str(r), "충청북도 청주시 상당구 미원면 가양길 2")
        self.assertEqual(M.parcel_str(r), "충청북도 청주시 상당구 미원면 가양리 332")

    def test_sub_no_and_bld_rules_kept(self):
        r = self.normal(); r["sub_no"] = 3; r["bld"] = "청주빌딩"
        self.assertEqual(M.addr_str(r), "충청북도 청주시 상당구 미원면 가양길 2-3 (청주빌딩)")
        self.assertEqual(M.road_str(r), "충청북도 청주시 상당구 미원면 가양길 2-3 (청주빌딩)")

    def test_main_no_zero_is_kept(self):
        r = self.normal(); r["main_no"] = 0                # 0 은 유효값 — 생략되면 안 됨
        self.assertEqual(M.road_str(r), "충청북도 청주시 상당구 미원면 가양길 0")

    def test_dong_excluded_from_road_only(self):
        # de15349 회귀 잠금 — road_str 은 동을 빼고 addr_str 은 그대로 넣는다(두 함수 계약 분리).
        # emd 를 무조건 포함하도록 되돌리면 이 테스트만이 그것을 잡는다(픽스처가 전부 읍·면이라서).
        r = self.dong()
        self.assertEqual(M.road_str(r), "서울특별시 강남구 강남대로 396")
        self.assertEqual(M.addr_str(r), "서울특별시 강남구 역삼동 강남대로 396")

    def test_parcel_falls_back_to_emd(self):
        r = self.sejong(); r["jibun"] = None               # jibun 부재 → emd 폴백(기존 계약)
        self.assertEqual(M.parcel_str(r), "세종특별자치시 조치원읍")

    def test_none_string_literal_guarded(self):
        r = self.sejong(); r["sigungu"] = "None"           # 'None' 문자열 값도 차단(_s 계약)
        self.assertNotIn("None", M.road_str(r))

    def test_all_region_tokens_missing(self):
        r = {"sido": None, "sigungu": None, "emd": None, "road": None,
             "main_no": None, "sub_no": None, "bld": None, "jibun": None}
        for fn in (M.addr_str, M.road_str, M.parcel_str):
            self.assertEqual(fn(r), "", f"{fn.__name__} 전결측 시 빈 문자열이어야 함")


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

    # ── task-014 emd 레벨 backfill (변경 ④) ────────────────────────
    def test_area_pip_returns_emd(self):
        # T-12: emd 레벨 경계가 적재돼 있으면 out["emd"] 가 채워진다
        #       (nonaddr_structure 가 이미 pip.get("emd") 를 읽으므로 POI structure 가 살아난다).
        # 목 행은 실제 질의(SELECT level, name, code)와 같은 컬럼 집합을 가져야 한다.
        #   T026(31d3bd5)이 area_pip() 에 out["bcode"] = a["code"] 를 추가했는데 이 목이
        #   T014 시절 형태(code 없음)로 남아 KeyError 로 죽었다. code 값은 로컬 PostGIS 실측
        #   (admin_boundary emd '청평면' = 41820325; emd code 는 5,067/5,370 이 8자리).
        cur = FakeCursor(fetchall_result=[
            {"level": "sido", "name": "경기도", "code": "41000"},
            {"level": "sigungu", "name": "가평군", "code": "41820"},
            {"level": "emd", "name": "청평면", "code": "41820325"},
        ])
        out = M.area_pip(cur, 127.419845, 37.737815)
        self.assertEqual(out.get("emd"), "청평면")
        self.assertEqual(out.get("sido"), "경기도")
        self.assertEqual(out.get("sigungu"), "가평군")
        # T026 의도 고정 — emd 폴리곤의 법정동코드를 bcode 로 싣는다(비-addr 경로 판정키).
        self.assertEqual(out.get("bcode"), "41820325")
        # 질의에 emd 레벨이 포함돼야 admin_boundary 가 emd 행을 돌려줄 수 있다.
        sql = cur.executed[0][0]
        self.assertIn("emd", sql)

    def test_area_pip_without_emd_level_unchanged(self):
        # T-12(후단): emd 경계 미적재 시 기존 동작 불변(fail-open — emd 키 자체가 없음).
        cur = FakeCursor(fetchall_result=[
            {"level": "sido", "name": "서울특별시"},
            {"level": "sigungu", "name": "강남구"},
        ])
        out = M.area_pip(cur, 127.0, 37.5)
        self.assertEqual(out, {"sido": "서울특별시", "sigungu": "강남구"})
        self.assertIsNone(out.get("emd"))


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


# ════════════════════════════════════════════════════════════════
# T019 — 역지오코딩 필지 PIP (parcel_at / pip_jibun / reverse 병합)
#
# 결함: /reverse 가 address 포인트 테이블 KNN 최근접으로 지번을 골라 원 지번 복원율이 44.9%.
#       parcel 폴리곤을 한 번도 보지 않는다. 해법은 ST_Contains PIP 로 지번 출처를 바꾸는 것.
# 함정: parcel.jibun 은 '825-42구' 처럼 지목 한 글자가 접미된다 → 문자열 금지, 정수컬럼 재조립.
# ════════════════════════════════════════════════════════════════

# 화전동 825-42 — 기준선(8092)이 '화전동 841-4' 를 돌려주던 좌표의 정답 필지.
PIP_ROW = {
    "jibun": "825-42구",                       # ← 지목 '구' 접미. 이 문자열을 그대로 쓰면 오답
    # emd_cd·pnu 는 실물값이다(8099 응답으로 대조 확인: b_code=4128112900).
    "emd_cd": "41281129", "pnu": "4128112900108250042", "ri_cd": "00", "ri_nm": None,
    "ji_main": 825, "ji_sub": 42, "san": 0,
    "sido": "경기도", "sigungu": "고양시 덕양구", "emd": "화전동",
}
# addr_at 이 집어 오는 최근접 포인트 — **다른 필지**(이것이 44.9% 의 정체).
# 읍면동까지 어긋나게 잡아 뒀다: 병합 결과의 지번계열이 정말 PIP 에서 왔는지 판별하려면
# 두 출처의 값이 달라야 한다(같으면 테스트가 아무것도 증명하지 못한다).
KNN_ROW = {
    "sido": "경기도", "sigungu": "고양시 덕양구", "emd": "도내동", "ri": None,
    "jibun": "도내동 148", "road": "권율대로", "main_no": 570, "sub_no": None,
    "bld": "", "postal": "10550", "haeng_dong": "행신3동",
    "bcode": "4128110700", "hcode": "4128163000", "kind": "addr",
}


class SeqCursor(FakeCursor):
    """SQL 내용으로 응답을 고르는 커서.

    reverse() 는 한 커서로 addr_at / parcel_at / nearest / admin_boundary 네 질의를 던진다.
    FakeCursor 는 모든 execute 에 같은 결과를 돌려주므로 병합·폴백 검증이 불가능하다.
    """

    def __init__(self, addr=None, parcel=None, nearest=None, areas=None, parcel_exc=None):
        super().__init__()
        self._addr, self._parcel = addr, parcel
        self._nearest = nearest if nearest is not None else []
        self._areas = areas if areas is not None else []
        self._parcel_exc = parcel_exc
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        s = " ".join(sql.split())
        if "FROM parcel" in s:
            self._last = "parcel"
            if self._parcel_exc is not None:
                raise self._parcel_exc
        elif "FROM admin_boundary" in s:
            self._last = "areas"
        elif "kind='addr'" in s:
            self._last = "addr"
        else:
            self._last = "nearest"
        return self

    def fetchone(self):
        return self._addr if self._last == "addr" else self._parcel

    def fetchall(self):
        return self._areas if self._last == "areas" else self._nearest

    def sql_of(self, tag):
        """저장된 질의 중 tag('parcel'/'addr'/'areas') 에 해당하는 첫 SQL 을 공백정규화해 반환."""
        for sql, _ in self.executed:
            s = " ".join(sql.split())
            if tag == "parcel" and "FROM parcel" in s: return s
            if tag == "areas" and "FROM admin_boundary" in s: return s
            if tag == "addr" and "kind='addr'" in s: return s
        return None


# ── TDD 1: parcel_at() 신설 — PIP 1행 → dict ────────────────────
class TestParcelAt(unittest.TestCase):
    def test_returns_row_dict(self):
        cur = FakeCursor(fetchone_result=dict(PIP_ROW))
        out = M.parcel_at(cur, 126.8713101, 37.6192808)
        self.assertIsNotNone(out)
        self.assertEqual(out["ji_main"], 825)
        self.assertEqual(out["ji_sub"], 42)
        self.assertEqual(out["emd"], "화전동")

    def test_query_is_st_contains_pip_limit1(self):
        cur = FakeCursor(fetchone_result=dict(PIP_ROW))
        M.parcel_at(cur, 126.8713101, 37.6192808)
        sql = " ".join(cur.executed[0][0].split())
        self.assertIn("ST_Contains(parcel.geom", sql)   # KNN(<->) 아님 — 포함관계
        self.assertNotIn("<->", sql)
        self.assertIn("FROM parcel", sql)
        self.assertIn("JOIN lawd_dong", sql)
        self.assertIn("LIMIT 1", sql)
        self.assertEqual(cur.executed[0][1], (126.8713101, 37.6192808))

    def test_zero_rows_returns_none(self):
        cur = FakeCursor(fetchone_result=None)          # 바다·미등록지
        self.assertIsNone(M.parcel_at(cur, 125.0, 33.0))


# ── TDD 2~5: pip_jibun() 재조립 ─────────────────────────────────
class TestPipJibun(unittest.TestCase):
    def test_2_reassembles_from_int_cols_not_jibun_string(self):
        # 지목 '구' 가 붙은 parcel.jibun 을 쓰면 안 된다
        self.assertEqual(M.pip_jibun(dict(PIP_ROW)), "화전동 825-42")

    def test_3_san_prefix(self):
        r = {"emd": "여산면", "ji_main": 59, "ji_sub": 5, "san": 1}
        self.assertEqual(M.pip_jibun(r), "여산면 산 59-5")

    def test_4_ji_sub_zero_omits_dash(self):
        r = {"emd": "도내동", "ji_main": 148, "ji_sub": 0, "san": 0}
        self.assertEqual(M.pip_jibun(r), "도내동 148")

    def test_4b_ji_sub_none_omits_dash(self):
        r = {"emd": "도내동", "ji_main": 148, "ji_sub": None, "san": 0}
        self.assertEqual(M.pip_jibun(r), "도내동 148")

    def test_5_ri_between_emd_and_bunji(self):
        r = {"emd": "여산면", "ri_nm": "두여리", "ji_main": 85, "ji_sub": 69, "san": 0}
        self.assertEqual(M.pip_jibun(r), "여산면 두여리 85-69")

    def test_5b_ri_absent_omitted(self):
        r = {"emd": "여산면", "ri_nm": None, "ji_main": 85, "ji_sub": 69, "san": 0}
        self.assertEqual(M.pip_jibun(r), "여산면 85-69")

    def test_5c_ri_key_alias_accepted(self):
        # 조인 별칭은 ri_nm 이지만 ri 키로 실려 와도 같은 결과여야 한다
        r = {"emd": "여산면", "ri": "두여리", "ji_main": 85, "ji_sub": 69, "san": 0}
        self.assertEqual(M.pip_jibun(r), "여산면 두여리 85-69")

    def test_ji_main_null_returns_none(self):
        # 번지를 만들 수 없으면 None → 호출측이 현행 KNN 지번을 유지한다(무회귀)
        self.assertIsNone(M.pip_jibun({"emd": "화전동", "ji_main": None, "ji_sub": 3}))


# nearest[] 에 실릴 addr 행. PIP 는 address.* 만 바꾸므로 여기엔 새어 들어오면 안 된다.
NEAR_ROW = dict(KNN_ROW, lon=126.8713101, lat=37.6192808, d=12.3, name=None, subtype=None)


def _reverse(addr=KNN_ROW, parcel=PIP_ROW, **kw):
    """reverse() 를 SeqCursor 로 1회 구동해 address 부분을 돌려주는 축약자."""
    cur = SeqCursor(addr=dict(addr) if addr else None,
                    parcel=dict(parcel) if parcel else None, **kw)
    return M.reverse(cur, 126.8713101, 37.6192808, 1), cur


# ── TDD 6: reverse() 병합 — 지번계열은 PIP, 도로명계열은 KNN ─────
class TestReverseMerge(unittest.TestCase):
    def test_6a_parcel_string_comes_from_pip(self):
        out, _ = _reverse()
        self.assertEqual(out["address"]["parcel"], "경기도 고양시 덕양구 화전동 825-42")

    def test_6b_road_zipcode_bld_come_from_knn(self):
        # addr_at 은 도로명·우편번호·건물명의 유일한 출처다. PIP 가 이 값을 지우면 회귀다.
        out, _ = _reverse()
        base = M.addr_obj(dict(KNN_ROW))
        for k in ("road", "zipcode", "bld"):
            self.assertEqual(out["address"][k], base[k], k)

    def test_6c_region_fields_move_with_the_jibun(self):
        # 지번만 PIP 로 바꾸고 시도·시군구·읍면동을 KNN 것으로 두면
        # '덕양구 도내동 825-42' 같은 잡종이 나온다 — 지번계열은 한 덩어리로 움직여야 한다.
        st = _reverse()[0]["address"]["structure"]
        self.assertEqual(st["emd"], "화전동")
        self.assertEqual(st["sido"], "경기도")
        self.assertEqual(st["sigungu"], "고양시 덕양구")
        self.assertEqual((st["ji_main"], st["ji_sub"]), (825, 42))
        self.assertIsNone(st["ri"])

    def test_6d_road_side_structure_fields_kept_from_knn(self):
        st = _reverse()[0]["address"]["structure"]
        base = M.addr_obj(dict(KNN_ROW))["structure"]
        for k in ("road_name", "main_no", "sub_no", "bld_main_no", "bld_sub_no",
                  "bld_name", "zipcode", "haeng_dong", "h_code"):
            self.assertEqual(st[k], base[k], k)

    def test_6e_nearest_and_areas_untouched(self):
        # PIP 는 address 에만 적용한다. nearest[] 는 종전 KNN 표기를 유지해야 한다.
        areas = [{"name": "화전동", "type": "emd", "code": "4128112900"}]
        cur = SeqCursor(addr=dict(KNN_ROW), parcel=dict(PIP_ROW),
                        nearest=[dict(NEAR_ROW)], areas=areas)
        out = M.reverse(cur, 126.8713101, 37.6192808, 1)
        self.assertEqual(len(out["nearest"]), 1)
        self.assertEqual(out["nearest"][0]["address"]["parcel"], M.parcel_str(dict(NEAR_ROW)))
        self.assertEqual(out["areas"], areas)

    def test_6f_pip_only_when_addr_at_empty(self):
        # 최근접 포인트가 2.5km 안에 없어도 필지는 있다 — 종전엔 address=null 이던 구간의 순증
        out, _ = _reverse(addr=None)
        self.assertIsNotNone(out["address"])
        self.assertEqual(out["address"]["parcel"], "경기도 고양시 덕양구 화전동 825-42")
        self.assertEqual(out["address"]["structure"]["ji_main"], 825)


# ── TDD 7: 폴백 — PIP 가 없거나 죽어도 기준선보다 나빠지지 않는다 ──
class TestReverseFallback(unittest.TestCase):
    def test_7a_zero_parcel_keeps_knn_result(self):
        out, _ = _reverse(parcel=None)
        self.assertEqual(out["address"], M.addr_obj(dict(KNN_ROW)))

    def test_7b_pip_exception_keeps_knn_result(self):
        out, _ = _reverse(parcel_exc=psycopg.errors.AmbiguousColumn("column emd_cd is ambiguous"))
        self.assertEqual(out["address"], M.addr_obj(dict(KNN_ROW)))

    def test_7c_pip_exception_is_not_503_or_500(self):
        # 예외를 삼키기만 하면 psycopg 트랜잭션이 abort 로 남아 이어지는 nearest/areas 질의가
        # InFailedSqlTransaction 으로 죽는다 — 폴백이 아니라 500 이 된다. HTTP 경계에서 확인한다.
        cur = SeqCursor(addr=dict(KNN_ROW), parcel=dict(PIP_ROW),
                        parcel_exc=psycopg.errors.QueryCanceled("statement timeout"))
        rec = _run_do_get("/reverse?lon=126.8713101&lat=37.6192808&limit=1", cur=cur)
        self.assertEqual(rec.code, 200)
        self.assertEqual(rec.obj["address"], M.addr_obj(dict(KNN_ROW)))

    def test_7d_pip_without_bunji_keeps_knn_result(self):
        out, _ = _reverse(parcel=dict(PIP_ROW, ji_main=None, ji_sub=None))
        self.assertEqual(out["address"], M.addr_obj(dict(KNN_ROW)))

    def test_7e_no_addr_no_parcel_stays_null(self):
        out, _ = _reverse(addr=None, parcel=None)
        self.assertIsNone(out["address"])


# ── TDD 8: addr_at() 무수정 (순방향 주소부착이 같이 쓴다) ─────────
class TestAddrAtUnchanged(unittest.TestCase):
    def test_8a_addr_at_still_knn_on_address_table(self):
        cur = FakeCursor(fetchone_result=dict(KNN_ROW))
        M.addr_at(cur, 126.8713101, 37.6192808)
        sql = " ".join(cur.executed[0][0].split())
        self.assertIn("FROM address", sql)
        self.assertIn("<->", sql)              # KNN 유지
        self.assertNotIn("parcel", sql)        # PIP 를 여기에 섞지 않는다
        self.assertNotIn("ST_Contains", sql)

    def test_8b_reverse_still_calls_addr_at(self):
        _, cur = _reverse()
        self.assertIsNotNone(cur.sql_of("addr"))


# ── TDD 9: 응답 스키마 불변 ──────────────────────────────────────
class TestReverseSchemaStable(unittest.TestCase):
    ADDR_KEYS = {"road", "parcel", "zipcode", "bld", "structure"}

    def _assert_shape(self, address):
        self.assertEqual(set(address), self.ADDR_KEYS)
        self.assertEqual(set(address["structure"]),
                         set(M.addr_obj(dict(KNN_ROW))["structure"]))

    def test_9a_merged(self):
        self._assert_shape(_reverse()[0]["address"])

    def test_9b_pip_only(self):
        self._assert_shape(_reverse(addr=None)[0]["address"])

    def test_9c_fallback(self):
        self._assert_shape(_reverse(parcel=None)[0]["address"])

    def test_9d_san_becomes_real_boolean(self):
        # F4 주석대로 address 경로의 structure.san 은 지금까지 항상 null 이었다.
        # PIP 병합분은 실제 산 여부를 싣는다(demo/guide.html:360 문서와 어긋나는 지점 — 보고 대상).
        self.assertIsNone(M.addr_obj(dict(KNN_ROW))["structure"]["san"])
        self.assertIs(_reverse()[0]["address"]["structure"]["san"], False)
        self.assertIs(_reverse(parcel=dict(PIP_ROW, san=1))[0]["address"]["structure"]["san"], True)

    def test_9e_san_stays_null_on_fallback(self):
        self.assertIsNone(_reverse(parcel=None)[0]["address"]["structure"]["san"])


# ── TDD 10: b_code 도 PIP 출처로 (pnu 앞 10자리) ─────────────────
class TestReverseBcode(unittest.TestCase):
    def test_10a_bcode_from_parcel_pnu(self):
        st = _reverse()[0]["address"]["structure"]
        self.assertEqual(st["b_code"], "4128112900")          # = left(pnu,10) · 8099 실측과 동일
        self.assertEqual(st["b_code"], M.parcel_bcode(dict(PIP_ROW)))
        self.assertNotEqual(st["b_code"], KNN_ROW["bcode"])   # KNN 출처가 아님을 못박는다

    def test_10b_bcode_falls_back_to_knn(self):
        self.assertEqual(_reverse(parcel=None)[0]["address"]["structure"]["b_code"],
                         M.addr_obj(dict(KNN_ROW))["structure"]["b_code"])

    def test_10c_hcode_never_from_parcel(self):
        # parcel 에는 행정동코드가 없다. PIP 가 h_code 를 건드리면 안 된다.
        self.assertEqual(_reverse()[0]["address"]["structure"]["h_code"],
                         M.addr_obj(dict(KNN_ROW))["structure"]["h_code"])


# ── TDD 11: PIP 질의 컬럼 한정 (ambiguous 회귀 가드) ─────────────
class TestParcelSqlQualified(unittest.TestCase):
    #  emd_cd 는 parcel·lawd_dong·lawd_ri 세 군데 모두에 있다. 한정하지 않으면 질의가
    #  ambiguous 로 죽고, 그 예외는 parcel_at 의 폴백에 삼켜져 **조용히 기준선과 같은 값**이 된다.
    #  즉 이 결함은 500 도 로그도 없이 개선 0% 로만 드러난다 — 그래서 SQL 자체를 검사한다.
    SHARED = ("emd_cd", "ri_cd", "geom", "jibun", "pnu", "san", "ji_main", "ji_sub",
              "sido", "sigungu", "emd")

    def _sql(self):
        cur = FakeCursor(fetchone_result=dict(PIP_ROW))
        M.parcel_at(cur, 126.8713101, 37.6192808)
        return " ".join(cur.executed[0][0].split())

    def _assert_all_qualified(self, sql):
        s = re.sub(r"\bAS\s+\w+", "", sql, flags=re.I)   # 출력 별칭은 모호하지 않다 → 제외
        for col in self.SHARED:
            m = re.search(rf"(?<![.\w]){col}\b", s)
            if m:
                self.fail(f"한정되지 않은 컬럼 {col!r} 발견 (…{s[max(0,m.start()-40):m.end()+20]}…)")

    def test_11a_qualified_without_lawd_ri(self):
        saved = M._HAS_LAWD_RI
        M._HAS_LAWD_RI = False
        try:
            self._assert_all_qualified(self._sql())
        finally:
            M._HAS_LAWD_RI = saved

    def test_11b_qualified_with_lawd_ri(self):
        # 운영은 lawd_ri 가 있는 쪽이다(T018 에서 구축). 조인이 붙는 이 경로가 실제 위험 구간.
        saved = M._HAS_LAWD_RI
        M._HAS_LAWD_RI = True
        try:
            sql = self._sql()
            self.assertIn("LEFT JOIN lawd_ri", sql)
            self.assertIn("lr.ri AS ri_nm", sql)
            self._assert_all_qualified(sql)
        finally:
            M._HAS_LAWD_RI = saved

    def test_11c_ri_join_absent_when_dict_missing(self):
        saved = M._HAS_LAWD_RI
        M._HAS_LAWD_RI = False
        try:
            self.assertNotIn("lawd_ri", self._sql())     # fail-open: 사전 없으면 조인도 없다
        finally:
            M._HAS_LAWD_RI = saved


# ── TDD 12 (수정 라운드 1 · D-1): PIP↔KNN 교차검증 가드 ──────────
#  1차 구현은 PIP 가 **1행이라도 나오면** 무조건 KNN 지번을 덮어썼다. 그 결과 산번지 폴리곤이
#  지적도에 없는 좌표는 전부 옆 일반필지로 조용히 치환됐고(NO119 파주 하지석동 산54-1 → 465-9),
#  폴백은 parcel_at 이 None 일 때만 있어 설계상 발동할 수 없었다.
#  가드는 **단방향**이다: KNN=산·PIP=일반 일 때만 KNN 을 지킨다. 반대(PIP=산·KNN=일반)는
#  PIP 가 정답인 상황이므로 건드리면 안 된다(벤치 595건 중 38건이 이 반대 방향이다).
SAN_KNN = {                                    # NO119 재현용 KNN 행(산번지 포인트)
    "sido": "경기도", "sigungu": "파주시", "emd": "하지석동", "ri": None,
    "jibun": "하지석동 산 54-1", "road": "청암로", "main_no": 100, "sub_no": None,
    "bld": "", "postal": "10911", "haeng_dong": "교하동",
    "bcode": "4148011000", "hcode": "4148057000", "kind": "addr",
}
GEN_PIP = {                                    # 그 점을 포함하는 **일반** 필지(산 폴리곤이 없어서)
    "jibun": "465-9 임", "emd_cd": "41480110", "pnu": "4148011000104650009",
    "ri_cd": "00", "ri_nm": None, "ji_main": 465, "ji_sub": 9, "san": 0,
    "sido": "경기도", "sigungu": "파주시", "emd": "하지석동",
}
SAN_PIP = dict(GEN_PIP, san=1, ji_main=54, ji_sub=1,        # 함정B: PIP 가 산인 정상 상황
               pnu="4148011000200540001", jibun="산54-1 임")


class TestPipSanGuard(unittest.TestCase):
    def test_12a_knn_san_near_pip_general_keeps_knn(self):
        # NO119 상황: PIP 는 일반번지 · KNN 은 0.0m 거리의 산번지 → 산 폴리곤 결측 신호 → KNN 유지
        addr = dict(SAN_KNN, knn_dist_m=0.0)
        out, _ = _reverse(addr=addr, parcel=GEN_PIP)
        self.assertEqual(out["address"]["parcel"], "경기도 파주시 하지석동 산 54-1")
        self.assertEqual(out["address"], M.addr_obj(dict(addr)))   # 전 필드 KNN 원본 그대로

    def test_12b_pip_san_knn_general_adopts_pip(self):
        # 함정B — 반대 방향은 PIP 가 정답이다. 대칭 가드로 만들면 여기서 회귀한다.
        out, _ = _reverse(addr=dict(KNN_ROW, knn_dist_m=0.0), parcel=SAN_PIP)
        self.assertEqual(out["address"]["parcel"], "경기도 파주시 하지석동 산 54-1")
        self.assertIs(out["address"]["structure"]["san"], True)

    def test_12c_both_general_and_near_adopts_pip(self):
        # 함정A — 1차에서 고쳐진 253건의 대표(화전동 841-4 → 825-42, KNN 거리 1.35m).
        # '가까우면 KNN' 같은 거리 단독 가드였다면 이 개선이 되돌아간다.
        out, _ = _reverse(addr=dict(KNN_ROW, knn_dist_m=1.35), parcel=PIP_ROW)
        self.assertEqual(out["address"]["parcel"], "경기도 고양시 덕양구 화전동 825-42")

    def test_12d_parcel_none_keeps_existing_fallback(self):
        # 기존 폴백 무회귀 — 가드가 붙어도 PIP 0행 경로는 그대로여야 한다.
        out, _ = _reverse(addr=dict(SAN_KNN, knn_dist_m=0.0), parcel=None)
        self.assertEqual(out["address"], M.addr_obj(dict(SAN_KNN, knn_dist_m=0.0)))
        out2, _ = _reverse(parcel=None)
        self.assertEqual(out2["address"], M.addr_obj(dict(KNN_ROW)))

    def test_12e_san_flip_but_knn_far_adopts_pip(self):
        # 벤치 실측: san 뒤집힘 7건 중 NO119 만 0.00m 이고 나머지 최근접은 32.65m 다.
        # 거리 상한이 없으면 개선 5건(NO203·538·532·507·586)이 되돌아간다 → 임계 필수.
        out, _ = _reverse(addr=dict(SAN_KNN, knn_dist_m=32.65), parcel=GEN_PIP)
        self.assertEqual(out["address"]["parcel"], "경기도 파주시 하지석동 465-9")

    def test_12f_missing_distance_adopts_pip(self):
        # 거리를 못 구하면 개입하지 않는다(fail-open). 가드가 새 회귀원이 되지 않게.
        out, _ = _reverse(addr=SAN_KNN, parcel=GEN_PIP)          # knn_dist_m 없음
        self.assertEqual(out["address"]["parcel"], "경기도 파주시 하지석동 465-9")

    def test_12g_no_knn_row_still_uses_pip(self):
        out, _ = _reverse(addr=None, parcel=GEN_PIP)
        self.assertEqual(out["address"]["parcel"], "경기도 파주시 하지석동 465-9")

    def test_12h_threshold_is_five_metres(self):
        self.assertEqual(M.PIP_SAN_GUARD_M, 5.0)
        for d, keeps_knn in ((4.99, True), (5.0, True), (5.01, False)):
            out, _ = _reverse(addr=dict(SAN_KNN, knn_dist_m=d), parcel=GEN_PIP)
            got_san = "산" in out["address"]["parcel"]
            self.assertIs(got_san, keeps_knn, f"{d}m 에서 판정이 뒤집혔다")


class TestAddrAtMeta(unittest.TestCase):
    """addr_at 이 거리·산 여부를 **추가로** 돌려준다 — 기존 반환은 그대로."""

    def test_12i_default_return_unchanged(self):
        cur = FakeCursor(fetchone_result=dict(KNN_ROW))
        self.assertEqual(M.addr_at(cur, 126.87, 37.61), M.addr_obj(dict(KNN_ROW)))

    def test_12j_with_meta_returns_pair(self):
        cur = FakeCursor(fetchone_result=dict(SAN_KNN, knn_dist_m=0.0))
        addr, meta = M.addr_at(cur, 126.738403, 37.760962, with_meta=True)
        self.assertEqual(addr, M.addr_obj(dict(SAN_KNN, knn_dist_m=0.0)))
        self.assertEqual(meta["dist_m"], 0.0)
        self.assertIs(meta["san"], True)

    def test_12k_meta_san_false_for_general_jibun(self):
        cur = FakeCursor(fetchone_result=dict(KNN_ROW, knn_dist_m=12.5))
        _, meta = M.addr_at(cur, 126.87, 37.61, with_meta=True)
        self.assertEqual(meta["dist_m"], 12.5)
        self.assertIs(meta["san"], False)

    def test_12l_meta_when_no_row(self):
        cur = FakeCursor(fetchone_result=None)
        addr, meta = M.addr_at(cur, 125.0, 33.0, with_meta=True)
        self.assertIsNone(addr)
        self.assertIsNone(meta["dist_m"])

    def test_12m_sql_carries_distance_and_stays_knn(self):
        cur = FakeCursor(fetchone_result=dict(KNN_ROW))
        M.addr_at(cur, 126.87, 37.61, with_meta=True)
        sql = " ".join(cur.executed[0][0].split())
        self.assertIn("ST_Distance", sql)
        self.assertIn("<->", sql)               # KNN 정렬 유지
        self.assertIn("LIMIT 1", sql)
        self.assertNotIn("parcel", sql)
        self.assertNotIn("ST_Contains", sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
