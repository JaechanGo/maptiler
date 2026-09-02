"""_common/region.py — 원천 주소 첫 토큰을 '검증 없이' 시도로 채택하던 결함의 회귀 테스트.

[실측 2026-09-02 .244] localdata_clean.csv 시도명 270종(정규 17), PostGIS address 에 biz 1,188행·
facility 90행이 '장전동'·'***번지'·'전남'·'서울특별시마포구'·'대전광역대전광역시' 같은 값으로 적재.
QC 시도 커버리지는 '< 16 이면 FAIL' 한쪽만 봐서 종류가 늘어난 오염은 통과시켰다.
"""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common.region import parse_region_kr, is_valid_sido, CANON_SIDO, LEGACY_SIDO  # noqa: E402


class ValidSido(unittest.TestCase):
    def test_current_and_legacy_accepted(self):
        for s in ("서울특별시", "전남광주통합특별시", "전북특별자치도", "강원특별자치도"):
            self.assertTrue(is_valid_sido(s), s)
        for s in ("전라남도", "광주광역시", "전라북도", "강원도"):   # 원천 잔존 구명칭 — DB 는 보존, API 가 표시 치환
            self.assertTrue(is_valid_sido(s), s)

    def test_garbage_rejected(self):
        for s in ("장전동", "***번지", "전남", "경기", "성남시", "서울특별시마포구", "대전광역대전광역시", "", None):
            self.assertFalse(is_valid_sido(s), repr(s))

    def test_canon_legacy_disjoint(self):
        self.assertFalse(CANON_SIDO & LEGACY_SIDO)


class ParseRegion(unittest.TestCase):
    def test_normal_keeps_shape(self):
        self.assertEqual(parse_region_kr("경기도 수원시 영통구 매탄동 123"), ("경기도", "수원시 영통구", "매탄동"))
        self.assertEqual(parse_region_kr("서울특별시 마포구 망원동 12-3"), ("서울특별시", "마포구", "망원동"))

    def test_abbreviation_expanded(self):
        self.assertEqual(parse_region_kr("전남 장흥군 회진면 회진리 100-1"), ("전라남도", "장흥군", "회진면"))
        self.assertEqual(parse_region_kr("경북 포항시 북구 죽도동 1"), ("경상북도", "포항시 북구", "죽도동"))

    def test_glued_sido_sigungu_split(self):
        self.assertEqual(parse_region_kr("서울특별시마포구 망원동 12"), ("서울특별시", "마포구", "망원동"))
        self.assertEqual(parse_region_kr("경기도수원시 팔달구 인계동 1"), ("경기도", "수원시 팔달구", "인계동"))

    def test_missing_sido_blank_not_guess(self):
        # 시도가 없으면 동 이름을 시도로 올리지 않는다 — 전부 빈값(API 가 좌표 PIP 로 채운다)
        self.assertEqual(parse_region_kr("장전동 100번지 2호 3층"), ("", "", ""))
        self.assertEqual(parse_region_kr("죽전동 1234번지 5호 건영타운"), ("", "", ""))

    def test_orgcode_fallback_for_typo(self):
        # 원천 오타 '대전광역대전광역시' — 규칙으론 못 살리지만 개방자치단체코드(6300000=대전) 로 복구
        self.assertEqual(parse_region_kr("대전광역대전광역시 서구 갈마1동 378-11", org_code="6300000"),
                         ("대전광역시", "서구", "갈마1동"))
        # 새 통합시 코드(6130000)는 regions.json 에 없어도 알아야 한다
        self.assertEqual(parse_region_kr("이상한값 목포시 용당동 1", org_code="6130000"),
                         ("전남광주통합특별시", "목포시", "용당동"))
        # 시군구 단위 코드(3xxxxxx)는 시도로 못 풀면 빈값
        self.assertEqual(parse_region_kr("장전동 100번지", org_code="3000000"), ("", "", ""))

    def test_sigungu_must_look_like_sigungu(self):
        # 시도는 살아도 두 번째 토큰이 시군구 형태가 아니면 sgg 를 비운다('***번지' 방지)
        self.assertEqual(parse_region_kr("경기도 ***번지 5호"), ("경기도", "", ""))

    def test_multiple_addrs_first_usable(self):
        self.assertEqual(parse_region_kr("", "충청남도 천안시 서북구 성정동 1"), ("충청남도", "천안시 서북구", "성정동"))


if __name__ == "__main__":
    unittest.main()
