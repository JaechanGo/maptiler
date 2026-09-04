#!/usr/bin/env python3
"""server/geocode-api.py(레거시 SQLite 지오코더) 표기 헬퍼 단위테스트 — DB 불요.

geocode.sqlite 는 7GB 이고 CI 에 없다. 그래서 여기서는 실제 DB 대신 :memory: 로
동일 컬럼 구성의 sqlite3.Row 를 만들어 검증한다. dict 로 대체하지 말 것 —
sqlite3.Row 에는 .get() 이 없어서 dict 로만 검증하면 그 회귀를 놓친다.

검증 대상 계약
  - road_str : 도로명주소법 시행령 §3① 3호에 따라 읍·면은 본문에 넣고, 동(洞)은 7호
               참고항목이라 뺀다. emd 한 칸에 읍/면/동이 섞여 들어오므로 접미사로 가른다.
  - addr_str : 표시용(검색결과 name). emd 를 **항상** 넣는다 — road_str 과 계약이 다르며
               본 수정으로 바뀌지 않아야 한다.
  - parcel_str : 지번 계약. 본 수정 범위 밖 — 불변.

실행:  python3 server/test_geocode_api_sqlite.py
"""
import importlib.util, os, sqlite3, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.environ.get("GEOCODE_SQLITE_MODULE", os.path.join(_HERE, "geocode-api.py"))


def _load_module(path=_MOD_PATH):
    spec = importlib.util.spec_from_file_location("geocode_api_sqlite", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_module()

_COLS = ("sido", "sigungu", "emd", "road", "main_no", "sub_no", "bld", "postal", "jibun")


def row(**kw):
    """실제 sqlite3.Row 를 만든다."""
    con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE p(%s)" % ",".join(_COLS))
    con.execute("INSERT INTO p VALUES(%s)" % ",".join("?" * len(_COLS)),
                [kw.get(c) for c in _COLS])
    return con.execute("SELECT * FROM p").fetchone()


BASE = dict(sido="충청북도", sigungu="청주시 상당구", road="가양길",
            main_no=2, sub_no=0, bld=None, postal="28198", jibun="미원리 332")


class TestRoadStrEmd(unittest.TestCase):
    # (a) 읍 — 도로명주소법 시행령 §3① 3호: 본문에 포함
    def test_eup_included(self):
        self.assertEqual(M.road_str(row(**dict(BASE, emd="조치원읍"))),
                         "충청북도 청주시 상당구 조치원읍 가양길 2")

    # (b) 면
    def test_myeon_included(self):
        self.assertEqual(M.road_str(row(**dict(BASE, emd="미원면"))),
                         "충청북도 청주시 상당구 미원면 가양길 2")

    # (c) 동 — 7호 참고항목이므로 도로명 본문에서 제외
    def test_dong_excluded(self):
        self.assertEqual(M.road_str(row(**dict(BASE, emd="역삼동"))),
                         "충청북도 청주시 상당구 가양길 2")

    # (d) emd 결측 — 구 DB 호환(_g 가 None 반환)
    def test_emd_missing(self):
        self.assertEqual(M.road_str(row(**dict(BASE, emd=None))),
                         "충청북도 청주시 상당구 가양길 2")

    def test_sub_no_and_bld_rules_kept(self):
        self.assertEqual(M.road_str(row(**dict(BASE, emd="미원면", sub_no=3, bld="청주빌딩"))),
                         "충청북도 청주시 상당구 미원면 가양길 2-3 (청주빌딩)")

    def test_main_no_zero_kept(self):
        self.assertEqual(M.road_str(row(**dict(BASE, emd="미원면", main_no=0))),
                         "충청북도 청주시 상당구 미원면 가양길 0")


class TestRoadStrBoundary(unittest.TestCase):
    """경계값 — 접미사 분기가 과잉/과소 포함으로 새지 않는지."""

    # 세종특별자치시: 시군구 없는 단층제(sigungu='' — 실 DB 실측: NULL 아니라 빈 문자열).
    # 본 수정의 실수혜 51,622건이 이 형태다.
    #
    # 기대 문자열의 '세종특별자치시' 뒤 공백은 **2칸이며 의도적이다.**
    # sigungu 가 빈 문자열이라 f-string 이 겹공백을 내는 것으로, 읍·면 누락과는
    # 별개 결함이고 본 태스크 범위 밖이다. 여기서 2칸을 그대로 단언하는 이유는
    # 나중에 누가 _s()/' '.join 정규화를 도입할 때 그것이 '의도적 보존'을 깨는
    # 변경임을 이 테스트가 잡아내게 하기 위해서다. 겹공백을 '버그'로 오인해
    # 여기를 1칸으로 고치지 말 것 — 고치려면 별도 태스크로 계약을 바꿔야 한다.
    def test_sigungu_empty_eup_included(self):
        self.assertEqual(M.road_str(row(**dict(BASE, sido="세종특별자치시", sigungu="",
                                               emd="조치원읍", road="세종로", main_no=2159))),
                         "세종특별자치시  조치원읍 세종로 2159")

    # 리(里) — 읍·면 접미사에 걸리지 않아 제외. 실 DB 의 emd 에는 리가 0건이나
    # (리는 jibun 컬럼에 들어간다) 계약으로 못박아 둔다.
    def test_ri_excluded(self):
        self.assertEqual(M.road_str(row(**dict(BASE, emd="미원리"))),
                         "충청북도 청주시 상당구 가양길 2")

    # sub_no 가 falsy(0) 인데 bld 는 truthy 인 조합 — 두 if 가 독립임을 확인
    def test_bld_kept_with_sub_no_zero(self):
        self.assertEqual(M.road_str(row(**dict(BASE, emd="남면", sub_no=0, bld="A동"))),
                         "충청북도 청주시 상당구 남면 가양길 2 (A동)")

    # emd 가 문자열이 아닐 때 — str() 방어가 없으면 AttributeError 로 터진다
    def test_emd_int_excluded_without_error(self):
        self.assertEqual(M.road_str(row(**dict(BASE, emd=123))),
                         "충청북도 청주시 상당구 가양길 2")

    # 빈 문자열(falsy) / 공백만 있는 문자열(truthy 지만 접미사 불일치) 모두 제외
    def test_emd_empty_or_blank_excluded(self):
        self.assertEqual(M.road_str(row(**dict(BASE, emd=""))),
                         "충청북도 청주시 상당구 가양길 2")
        self.assertEqual(M.road_str(row(**dict(BASE, emd=" "))),
                         "충청북도 청주시 상당구 가양길 2")


class TestUnchangedContracts(unittest.TestCase):
    """본 수정이 건드리면 안 되는 계약 — addr_str / parcel_str.

    addr_str 은 golden QC(13-qc-check.py)·parity(13d-geocode-parity.py) 의 판정축인
    name 을 만든다. 여기가 흔들리면 기준선이 통째로 흔들리므로 읍/면/동/결측 4형태를
    모두 잠근다.
    """

    def test_addr_str_always_has_emd(self):
        self.assertEqual(M.addr_str(row(**dict(BASE, emd="역삼동"))),
                         "충청북도 청주시 상당구 역삼동 가양길 2")

    def test_addr_str_eup_unchanged(self):
        self.assertEqual(M.addr_str(row(**dict(BASE, emd="조치원읍"))),
                         "충청북도 청주시 상당구 조치원읍 가양길 2")

    def test_addr_str_myeon_unchanged(self):
        self.assertEqual(M.addr_str(row(**dict(BASE, emd="미원면"))),
                         "충청북도 청주시 상당구 미원면 가양길 2")

    # emd 가 없으면 addr_str 은 'None' 문자열을 낸다. 이는 addr_str 의 별개 결함이며
    # 여기 적힌 기대값은 **승인된 계약이 아니라 현행 동작의 스냅샷**이다.
    # 목적은 하나 — road_str 수정이 addr_str 로 새지 않았음을 증명하는 것.
    # 이 결함을 고치는 것은 별도 태스크이고, 그때 이 단언도 함께 바꿔야 한다.
    def test_addr_str_emd_missing_unchanged(self):
        self.assertEqual(M.addr_str(row(**dict(BASE, emd=None))),
                         "충청북도 청주시 상당구 None 가양길 2")

    def test_parcel_str_unchanged(self):
        self.assertEqual(M.parcel_str(row(**dict(BASE, emd="미원면"))),
                         "충청북도 청주시 상당구 미원리 332")


if __name__ == "__main__":
    unittest.main(verbosity=2)
