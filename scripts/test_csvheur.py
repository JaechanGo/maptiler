#!/usr/bin/env python3
"""scripts/_common/csvheur.py — CSV 컬럼 휴리스틱 공유 테스트 (T028 §9 T6).

stdlib unittest 만 사용(pytest 미설치). 실행: python3 scripts/test_csvheur.py
**DB 접속 없음** — psql `\\copy` 를 부르지 않고 함수 단위로만 본다.

배경(§4.5): 공공시설 CSV 는 출처마다 헤더가 제각각이라 두 스크립트가 각자
휴리스틱을 갖고 있었다. 좌표 선정에서는 11b 가, 인코딩 처리에서는
load_facility 가 우수했다. 커밋 8 은 전자를 _common/csvheur.py 로 모아
양쪽이 쓰게 하고, 커밋 9 는 후자를 11b 에 옮긴다.

T6-A 좌표 선정   — 정수 '도' 컬럼 대신 십진 컬럼을 고르는가(양쪽 경로에서)
T6-B 계약 불변   — load_facility.pick() 이 여전히 인덱스를 돌려주고
                   override 분기가 살아 있는가
T6-C 배선        — 11b 가 사본을 지우고 정본을 import 하는가
T6-D 인코딩      — cp949·utf-8 정상 파싱, 손상 파일은 SystemExit (양쪽 경로)
"""
import csv
import importlib.util
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "postgis"))

from _common.csvheur import pick_coord, pick_coord_index  # noqa: E402

import load_facility  # noqa: E402

_11B = os.path.join(HERE, "11b-build-facility.py")


def _load_11b():
    """11b-build-facility.py 를 모듈로 로드 — 파일명이 숫자 시작 + 하이픈이라 import 불가.

    main() 은 `__name__` 가드 안이라 실행되지 않는다. 최상위에 남는 것은 경로 계산과
    facility-catalog.json 로드(예외 삼킴)뿐이라 부작용이 없다.
    SRC/OUT 이 sys.argv 를 보므로 로드 동안만 argv 를 비워 unittest 인자와 섞이지 않게 한다.
    """
    spec = importlib.util.spec_from_file_location("_t028_11b", _11B)
    mod = importlib.util.module_from_spec(spec)
    argv = sys.argv[:]
    sys.argv = [_11B]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod

# 민방위대피시설 형식 — 위/경도가 DMS 분해('도' 정수)와 십진(WGS84) 두 벌로 실린다.
# 이름만 보면 '위도(도)' 가 LAT 힌트에 먼저 걸려 좌표가 정수 격자로 뭉개진다(실사고).
SHELTER_HEADER = ["시설명", "주소", "위도(도)", "WGS84위도", "경도(도)", "WGS84경도"]
SHELTER_ROWS = [
    ["○○지하주차장", "서울 중구 세종대로 110", "37", "37.5776", "126", "126.9770"],
    ["△△학교 지하", "부산 해운대구 우동 1413", "35", "35.1631", "129", "129.1635"],
    ["□□아파트 지하", "대전 서구 둔산로 100", "36", "36.3504", "127", "127.3845"],
]
LAT_RANGE = (33, 39)     # 11b:130 원문 — 한반도 남부 bbox
LON_RANGE = (124, 132)   # 11b:131 원문


def _write_csv(path, header, rows, encoding="utf-8-sig"):
    with open(path, "w", newline="", encoding=encoding) as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


class TestPickCoordShared(unittest.TestCase):
    """T6-A(1/2). 정본 모듈이 값으로 좌표 컬럼을 고르는가 — 11b 계약(컬럼명 반환)."""

    def _dict_rows(self):
        return [dict(zip(SHELTER_HEADER, r)) for r in SHELTER_ROWS]

    def test_decimal_column_wins_lat(self):
        """이름 순서상 앞서는 '위도(도)' 를 제치고 십진 컬럼이 뽑혀야 한다."""
        got = pick_coord(
            SHELTER_HEADER, self._dict_rows(), ["위도", "lat"], *LAT_RANGE
        )
        self.assertEqual(got, "WGS84위도", "정수 '도' 컬럼을 골랐다 — 좌표가 정수 격자로 뭉개진다")

    def test_decimal_column_wins_lon(self):
        got = pick_coord(
            SHELTER_HEADER, self._dict_rows(), ["경도", "lon", "lng"], *LON_RANGE
        )
        self.assertEqual(got, "WGS84경도")

    def test_no_decimal_candidate_returns_none(self):
        """11b 계약: 십진 후보가 없으면 None.

        11b 는 TM 좌표(EPSG:5174) 폴백이 있어 None 이 안전하다. load_facility 는
        폴백이 없어 정책이 다르다(TestLoadFacilityPick 참조) — 계약 차이는 의도된 것.
        """
        header = ["시설명", "위도(도)"]
        rows = [{"시설명": "가", "위도(도)": "37"}]
        self.assertIsNone(pick_coord(header, rows, ["위도"], *LAT_RANGE))

    def test_out_of_range_decimal_rejected(self):
        """십진이어도 bbox 밖이면 점수를 얻지 못한다(EPSG:5174 X/Y 오인 방지)."""
        header = ["위도", "좌표정보(y)"]
        rows = [{"위도": "37.5776", "좌표정보(y)": "445123.7"}]
        self.assertEqual(pick_coord(header, rows, ["위도", "y좌표"], *LAT_RANGE), "위도")

    def test_index_variant_matches_name_variant(self):
        """인덱스판과 이름판이 같은 컬럼을 가리켜야 한다(두 소비자의 결과 일치)."""
        cand = [2, 3]   # 위도(도), WGS84위도
        self.assertEqual(pick_coord_index(cand, SHELTER_ROWS, *LAT_RANGE), 3)


class TestLoadFacilityPick(unittest.TestCase):
    """T6-A(2/2)·T6-B. load_facility 경로에서도 같은 선택이 나오되 계약은 그대로인가."""

    def test_lat_lon_pick_decimal_column(self):
        """커밋 8 의 본체. 전환 전에는 인덱스 2/4(정수 '도')를 골랐다(실측)."""
        ilat = load_facility.pick(
            SHELTER_HEADER, load_facility.LAT_KW, None, SHELTER_ROWS, LAT_RANGE
        )
        ilon = load_facility.pick(
            SHELTER_HEADER, load_facility.LON_KW, None, SHELTER_ROWS, LON_RANGE
        )
        self.assertEqual(SHELTER_HEADER[ilat], "WGS84위도")
        self.assertEqual(SHELTER_HEADER[ilon], "WGS84경도")

    def test_returns_index_not_name(self):
        """T6-B. 반환 계약은 **인덱스**다 — 호출부가 r[i] 로 소비하므로 바뀌면 즉사한다."""
        ilat = load_facility.pick(
            SHELTER_HEADER, load_facility.LAT_KW, None, SHELTER_ROWS, LAT_RANGE
        )
        self.assertIsInstance(ilat, int)
        self.assertEqual(ilat, 3)

    def test_override_uses_header_index(self):
        """T6-B. --lat-col 지정 시 header.index(override) 경로가 살아 있어야 한다."""
        self.assertEqual(
            load_facility.pick(SHELTER_HEADER, load_facility.LAT_KW, "위도(도)"), 2
        )

    def test_override_missing_exits(self):
        """지정 컬럼이 없으면 조용히 넘기지 않고 중단한다."""
        with self.assertRaises(SystemExit):
            load_facility.pick(SHELTER_HEADER, load_facility.LAT_KW, "없는컬럼")

    def test_name_addr_unaffected(self):
        """좌표가 아닌 컬럼은 값 검증 인자 없이 종전 그대로 동작한다."""
        self.assertEqual(
            load_facility.pick(SHELTER_HEADER, load_facility.NAME_KW, None), 0
        )
        self.assertEqual(
            load_facility.pick(SHELTER_HEADER, load_facility.ADDR_KW, None), 1
        )

    def test_single_candidate_keeps_legacy_choice(self):
        """후보가 하나뿐이면 점수화를 건너뛴다 — 종전 동작 보존(회귀 0).

        값이 정수라 점수 0 이어도 그 컬럼을 준다. 여기서 None 을 주면 지금까지
        적재되던 좌표가 통째로 사라진다.
        """
        header = ["시설명", "위도"]
        rows = [["가", "37"]]
        self.assertEqual(
            load_facility.pick(header, load_facility.LAT_KW, None, rows, LAT_RANGE), 1
        )

    def test_all_candidates_non_decimal_falls_back(self):
        """후보가 복수여도 전부 비십진이면 키워드 1순위로 폴백한다 — 종전 동작 보존."""
        header = ["시설명", "위도", "y좌표"]
        rows = [["가", "37", "445123"]]
        self.assertEqual(
            load_facility.pick(header, load_facility.LAT_KW, None, rows, LAT_RANGE), 1
        )

    def test_no_candidate_returns_none(self):
        self.assertIsNone(
            load_facility.pick(["시설명", "주소"], load_facility.LAT_KW, None)
        )


class TestSourceWiring(unittest.TestCase):
    """T6-C. 사본이 되살아나지 않았는가 — T3(test_textnorm) 와 같은 취지의 가드."""

    def _src(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_11b_imports_shared_module(self):
        src = self._src(_11B)
        self.assertIsNone(
            re.search(r"^def pick_coord\(", src, re.M),
            "11b-build-facility.py 에 pick_coord 인라인 사본이 되살아났다",
        )
        m = re.search(r"^from _common\.csvheur import .*$", src, re.M)
        self.assertIsNotNone(m, "11b-build-facility.py 에 csvheur import 문이 없다")
        self.assertIn("pick_coord", m.group(0))

    def test_load_facility_imports_shared_module(self):
        src = self._src(load_facility.__file__)
        m = re.search(r"^from _common\.csvheur import .*$", src, re.M)
        self.assertIsNotNone(m, "load_facility.py 에 csvheur import 문이 없다")
        self.assertIn("pick_coord_index", m.group(0))

    def test_load_facility_has_path_preamble(self):
        """postgis/ 는 scripts/ 의 하위라 sys.path 보강 없이는 _common 을 못 찾는다.

        PYTHONSAFEPATH=1 셸에서도 살아남아야 하므로 명시 삽입이 필요하다(§4.3).
        """
        self.assertIn("sys.path.insert", self._src(load_facility.__file__))


class TestEncoding(unittest.TestCase):
    """T6-D. 인코딩 폴백 — load_facility 가 정본이고 커밋 9 가 11b 로 이식한다(§4.5)."""

    def test_read_rows_utf8(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_csv(os.path.join(d, "u.csv"), SHELTER_HEADER, SHELTER_ROWS)
            header, rows = load_facility.read_rows(p)
        self.assertEqual(header, SHELTER_HEADER)
        self.assertEqual(len(rows), 3)

    def test_read_rows_cp949(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_csv(
                os.path.join(d, "c.csv"), SHELTER_HEADER, SHELTER_ROWS, "cp949"
            )
            header, rows = load_facility.read_rows(p)
        self.assertEqual(header, SHELTER_HEADER)
        self.assertEqual(rows[0][0], "○○지하주차장")

    def test_read_rows_corrupt_exits(self):
        """어떤 후보로도 못 읽으면 깨진 문자로 진행하지 말고 중단해야 한다.

        utf-8 로 쓴 뒤 한 바이트를 잘라 UTF-8·CP949·EUC-KR 모두에서 깨지게 만든다.
        """
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.csv")
            with open(p, "wb") as fh:
                fh.write("시설명,주소\n서울역,중구\n".encode("utf-8")[:-3] + b"\x81\xff\n")
            with self.assertRaises(SystemExit):
                load_facility.read_rows(p)


class Test11bEncoding(unittest.TestCase):
    """T6-D(11b). 커밋 9 — load_facility 의 인코딩 관례를 11b `read_csv()` 로 이식.

    종전 11b 는 후보가 2개뿐이고 최후에 `errors="replace"` 로 **깨진 문자를 그대로
    상호명에 실어 적재**했다. 깨진 이름이 지오코딩 색인에 들어가면 조용히 남으므로,
    중단이 낫다(§4.5). 반환 계약 `(rows, enc)` 는 그대로 둔다 — 호출부가 enc 를 로그에 쓴다.
    """

    def setUp(self):
        self.m = _load_11b()

    def _write(self, d, name, encoding):
        return _write_csv(
            os.path.join(d, name), SHELTER_HEADER, SHELTER_ROWS, encoding
        )

    def test_read_csv_utf8(self):
        with tempfile.TemporaryDirectory() as d:
            rows, enc = self.m.read_csv(self._write(d, "u.csv", "utf-8-sig"))
        self.assertEqual(enc, "utf-8-sig")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["시설명"], "○○지하주차장")

    def test_read_csv_cp949(self):
        with tempfile.TemporaryDirectory() as d:
            rows, enc = self.m.read_csv(self._write(d, "c.csv", "cp949"))
        self.assertEqual(enc, "cp949")
        self.assertEqual(rows[0]["WGS84위도"], "37.5776")

    def test_read_csv_corrupt_exits(self):
        """커밋 9 의 본체. 전에는 'cp949(replace)' 로 깨진 문자를 돌려줬다."""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.csv")
            with open(p, "wb") as fh:
                fh.write("시설명,주소\n서울역,중구\n".encode("utf-8")[:-3] + b"\x81\xff\n")
            with self.assertRaises(SystemExit):
                self.m.read_csv(p)

    def test_read_csv_empty_file_is_not_fatal(self):
        """0바이트 CSV 는 중단 사유가 아니다 — main() 이 `if not rows: continue` 로 넘긴다.

        load_facility.read_rows 의 `if rows:` 재시도는 **이식하지 않는다**. 이식하면
        빈 파일이 4후보를 모두 소진해 sys.exit 로 빌드 전체를 죽인다. 수집 단계가
        0바이트 파일을 남긴 전례가 있어(T027) 실제로 밟는 경로다.
        """
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "empty.csv")
            open(p, "wb").close()
            rows, _ = self.m.read_csv(p)
        self.assertEqual(rows, [])

    def test_encoding_candidates_widened(self):
        """후보 4개 채택 + `errors="replace"` 사본이 되살아나지 않았는가."""
        with open(_11B, encoding="utf-8") as fh:
            src = fh.read()
        for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
            self.assertIn(f'"{enc}"', src, f"인코딩 후보 {enc} 가 없다")
        # open() 호출에 붙은 것만 본다 — docstring 이 폐지 사유로 인용하는 것과 구별해야 한다.
        self.assertIsNone(
            re.search(r'open\([^)]*errors="replace"', src),
            "깨진 문자로 진행하는 폴백이 되살아났다",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
