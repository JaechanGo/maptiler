#!/usr/bin/env python3
"""T026 인천 신설 4구 치환 단위테스트 U1~U10 (DB 불요 — 치환표를 모의 커서로 주입).

근거: VWorld dsId=30505 OLD_LAWDCD. 이 파일의 픽스처는 전부
`~/maptiler-rescue/vworld-probe/lawdcd/LSCT_LAWDCD.csv` 실측값이며,
**명칭 꼬리 조인으로 유도한 값이 아니다**(P1 회귀 가드).

대상 파일명이 하이픈(`geocode-api-pg.py`)이라 일반 import 불가 →
importlib.util.spec_from_file_location 로 모듈 핸들 확보(test_geocode_api.py 선례).

실행:  python3 -m unittest server.test_incheon_sgg_remap
       (또는 python3 server/test_incheon_sgg_remap.py)
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


# ── 치환표 픽스처 (VWorld 30505 실측 발췌) ────────────────────────
# old_emd8 → (new_emd8, old_sgg_nm, new_sgg_nm)
#   28110102 중구 중앙동2가 → 28125109 제물포구 중앙동2가
#   28110145 중구 중산동     → 28155101 영종구 중산동      ← 같은 옛 구명이 코드에 따라 갈림
#   28260117 서구 대곡동     → 28290106 검단구 대곡동
#   28140106 동구 금곡동     → 28125106 제물포구 금곡동    ← 신 금곡동 2:2 중복의 한 짝
#   28260118 서구 금곡동     → 28290107 검단구 금곡동      ← 나머지 한 짝
SGG_FIXTURE = [
    ("28110102", "28125109", "중구", "제물포구"),
    ("28110145", "28155101", "중구", "영종구"),
    ("28260117", "28290106", "서구", "검단구"),
    ("28140106", "28125106", "동구", "제물포구"),
    ("28260118", "28290107", "서구", "검단구"),
]

# 전남·광주(46/29 → 12) 픽스처 — U4 가 인천 표 도입으로 오염되지 않았음을 보이기 위함.
SIDO_FIXTURE = [("46820430", "12820430")]


class SeqCursor:
    """to_regclass 확인(fetchone) 1회 + 표 적재(fetchall) N회를 순서대로 돌려주는 모의 커서."""

    def __init__(self, ok=True, fetchalls=None, raise_exc=None):
        self._ok = ok
        self._queue = list(fetchalls or [])
        self._raise = raise_exc
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._raise is not None:
            raise self._raise
        return self

    def fetchone(self):
        return {"ok": self._ok}

    def fetchall(self):
        return self._queue.pop(0) if self._queue else []


def _install_sgg(rows=SGG_FIXTURE):
    """lawd_sgg_remap 이 적재된 상태를 만든다. 실제 _load_sgg_remap 경로를 태운다."""
    cur = SeqCursor(ok=True, fetchalls=[
        [{"o": o, "n": n, "g": g} for (o, n, _og, g) in rows],
    ])
    return M._load_sgg_remap(cur)


def _install_sido(rows=SIDO_FIXTURE):
    cur = SeqCursor(ok=True, fetchalls=[
        [{"o": o, "n": n} for (o, n) in rows],
        [],                                    # lawd_ri_remap_exception
    ])
    return M._load_sido_remap(cur)


def _clear_all():
    M._SGG_REMAP, M._HAS_SGG_REMAP = {}, False
    M._SIDO_REMAP, M._RI_REMAP_EXC, M._HAS_SIDO_REMAP = {}, {}, False


class TestRemapBcodeIncheon(unittest.TestCase):
    """U1~U6 — remap_bcode 의 28 분기."""

    def setUp(self):
        _clear_all()

    def tearDown(self):
        _clear_all()

    def test_u1_bcode10_incheon_substituted(self):
        """U1 2811010200(옛 중구 중앙동2가 10자리) → 2812510900."""
        self.assertTrue(_install_sgg())
        self.assertEqual(M.remap_bcode("2811010200"), "2812510900")

    def test_u2_bcode8_incheon_substituted(self):
        """U2 8자리 계약 유지 — 28110102 → 28125109 (길이 8 보존)."""
        _install_sgg()
        got = M.remap_bcode("28110102")
        self.assertEqual(got, "28125109")
        self.assertEqual(len(got), 8)

    def test_u2b_bcode10_tail_preserved(self):
        """U2 보강 — 10자리의 뒤 2자리(리코드)는 보존한다."""
        _install_sgg()
        self.assertEqual(M.remap_bcode("2811010203"), "2812510903")

    def test_u3_gyeyang_untouched(self):
        """U3 2824510100 계양구(비대상, 표에 없음) → 원값."""
        _install_sgg()
        self.assertEqual(M.remap_bcode("2824510100"), "2824510100")

    def test_u4_46_code_follows_sido_rule(self):
        """U4 46체계는 lawd_sido_remap 규칙을 그대로 따른다(인천 표에 영향받지 않음)."""
        _install_sgg()
        # (a) sido 표 미적재 → 원값
        self.assertEqual(M.remap_bcode("4682043001"), "4682043001")
        # (b) sido 표 적재 → 12 로 치환. 인천 표가 붙어 있어도 규칙이 살아 있다.
        _install_sido()
        self.assertEqual(M.remap_bcode("4682043001"), "1282043001")
        # (c) 인천 표는 46 코드를 건드리지 않는다 — sgg 표만 있고 sido 표가 없으면 원값
        M._SIDO_REMAP, M._RI_REMAP_EXC, M._HAS_SIDO_REMAP = {}, {}, False
        self.assertEqual(M.remap_bcode("4682043001"), "4682043001")

    def test_u5_malformed_inputs_return_original(self):
        """U5 None/''/'ABC'/7자리/9자리/숫자아님 → 원값·무예외."""
        _install_sgg()
        for bad in (None, "", "ABC", "2811010", "281101020", 0, [], {"a": 1}):
            with self.subTest(bad=bad):
                self.assertIs(M.remap_bcode(bad), bad)

    def test_u5b_padded_char_column(self):
        """U5 보강 — char(n) 공백 패딩은 btrim 후 치환된다."""
        _install_sgg()
        self.assertEqual(M.remap_bcode("2811010200  "), "2812510900")

    def test_u6_fail_open_when_table_absent(self):
        """U6 표 미적재(_HAS_SGG_REMAP=False) → 전부 원값. 예외 없이 현행 동작 유지."""
        _clear_all()
        self.assertFalse(M._HAS_SGG_REMAP)
        for cd in ("2811010200", "28110102", "2826011700", "2824510100"):
            with self.subTest(cd=cd):
                self.assertEqual(M.remap_bcode(cd), cd)

    def test_u6b_load_failure_is_fail_open(self):
        """U6 보강 — 적재 중 예외가 나면 전역을 비우고 False (부분 적재 금지)."""
        _install_sgg()
        self.assertTrue(M._HAS_SGG_REMAP)
        self.assertFalse(M._load_sgg_remap(SeqCursor(raise_exc=RuntimeError("boom"))))
        self.assertFalse(M._HAS_SGG_REMAP)
        self.assertEqual(M._SGG_REMAP, {})
        self.assertEqual(M.remap_bcode("2811010200"), "2811010200")

    def test_u6c_table_absent_is_fail_open(self):
        """U6 보강 — to_regclass 가 없으면(ok=False) False. 예외 전파 금지."""
        _clear_all()
        self.assertFalse(M._load_sgg_remap(SeqCursor(ok=False)))
        self.assertFalse(M._HAS_SGG_REMAP)


class TestRemapSigungu(unittest.TestCase):
    """U7~U9 — remap_sigungu 표기 치환."""

    def setUp(self):
        _clear_all()
        _install_sgg()

    def tearDown(self):
        _clear_all()

    def test_u7_jung_to_jemulpo(self):
        """U7 2811010200 + '중구' → '제물포구'."""
        self.assertEqual(M.remap_sigungu("2811010200", "중구"), "제물포구")

    def test_u8_same_old_name_splits_by_code(self):
        """U8 같은 '중구' 라도 코드가 영종 계열이면 '영종구'.

        계획서 §8-1 U8 은 코드를 `2811025000` 으로 적었으나 그 코드는 VWorld 30505
        실측(31,172행)에 존재하지 않는다. 영종구로 가는 옛 코드는 28110145~28110152 다.
        취지("같은 옛 구명이 코드에 따라 갈리는 것")를 보존한 채 실측 코드로 정정한다.
        """
        self.assertEqual(M.remap_sigungu("2811014500", "중구"), "영종구")
        # 두 값이 실제로 갈리는지 — 같은 옛 구명, 다른 결과
        self.assertNotEqual(M.remap_sigungu("2811014500", "중구"),
                            M.remap_sigungu("2811010200", "중구"))

    def test_u9_gyeyang_returns_original(self):
        """U9 2824510100 + '계양구' → '계양구'(원값)."""
        self.assertEqual(M.remap_sigungu("2824510100", "계양구"), "계양구")

    def test_u9b_missing_or_malformed_bcode_passthrough(self):
        """bcode 결측/기형이면 시군구 원값을 그대로 통과시킨다(안전 통과)."""
        for bad in (None, "", "ABC", "281101", 0):
            with self.subTest(bad=bad):
                self.assertEqual(M.remap_sigungu(bad, "중구"), "중구")
        self.assertIsNone(M.remap_sigungu("2811010200", None))

    def test_u9c_fail_open(self):
        """표 미적재면 표기 치환도 전면 비활성(코드 치환과 같은 스위치)."""
        _clear_all()
        self.assertEqual(M.remap_sigungu("2811010200", "중구"), "중구")

    def test_u9d_dong_duplicate_splits_correctly(self):
        """신 금곡동 2:2 중복 — 옛 동구/서구 금곡동이 제물포구/검단구로 정확히 갈린다."""
        self.assertEqual(M.remap_sigungu("2814010600", "동구"), "제물포구")
        self.assertEqual(M.remap_sigungu("2826011800", "서구"), "검단구")

    def test_u9e_daegok_is_geomdan_not_seohae(self):
        """I5/I5-b 의 근거 — 대곡동(28260117)은 검단구다. 서해구가 아니다."""
        self.assertEqual(M.remap_sigungu("2826011700", "서구"), "검단구")
        self.assertEqual(M.remap_bcode("2826011700"), "2829010600")


class TestSidoRemapNotPolluted(unittest.TestCase):
    """U10 — 인천이 전남·광주 치환에 오염되지 않음."""

    def setUp(self):
        _clear_all()

    def tearDown(self):
        _clear_all()

    def test_u10_incheon_sido_name_unchanged(self):
        """U10 remap_sido_name('인천광역시') → '인천광역시'."""
        _install_sgg()
        _install_sido()
        self.assertEqual(M.remap_sido_name("인천광역시"), "인천광역시")
        self.assertEqual(M.remap_sido_name("인천"), "인천")

    def test_u10b_merged_old_codes_has_no_28(self):
        """U10 집행 — SIDO_MERGED_OLD_CODES 에 '28' 을 넣으면 인천이 전남광주가 된다."""
        self.assertNotIn("28", M.SIDO_MERGED_OLD_CODES)
        self.assertNotIn("인천광역시", M.SIDO_MERGED_OLD)
        self.assertNotIn("인천", M.SIDO_MERGED_OLD_ABBR)

    def test_u10c_jeonnam_still_substituted(self):
        """전남·광주 치환은 살아 있어야 한다(§6-3: 제거 금지)."""
        _install_sgg()
        _install_sido()
        self.assertEqual(M.remap_sido_name("전라남도"), "전남광주통합특별시")
        self.assertEqual(M.remap_sido_name("광주광역시"), "전남광주통합특별시")

    def test_u10d_switches_are_independent(self):
        """_HAS_SGG_REMAP 과 _HAS_SIDO_REMAP 은 독립 스위치다."""
        _install_sgg()
        self.assertTrue(M._HAS_SGG_REMAP)
        self.assertFalse(M._HAS_SIDO_REMAP)      # 인천 표만 붙어도 전남 표는 꺼진 채
        self.assertEqual(M.remap_sido_name("전라남도"), "전라남도")
        _clear_all()
        _install_sido()
        self.assertTrue(M._HAS_SIDO_REMAP)
        self.assertFalse(M._HAS_SGG_REMAP)       # 전남 표만 붙어도 인천 표는 꺼진 채
        self.assertEqual(M.remap_bcode("2811010200"), "2811010200")


if __name__ == "__main__":
    unittest.main(verbosity=2)
