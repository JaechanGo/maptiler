#!/usr/bin/env python3
"""T026 통합 테스트 I1~I11 — 인천 자치구 개편(안 A: 응답경계 치환) 라이브 검증.

계획서 §8-3 의 I 표를 그대로 구현한다. **DB 를 바꾸지 않는다**(안 A). 읽기 전용 호출만 한다.

대상 서버 (Conductor 확정 구조 — 계획서 §9-2 의 8092 단일 전제에서 벗어난 부분):
  OURS   = http://127.0.0.1:8093   ← 우리 워크트리를 마운트한 `t026-verify` 컨테이너(after)
  BEFORE = http://127.0.0.1:8092   ← geocode-none-fix 워크트리를 마운트한 기준선(before, 동결)
두 대가 동시에 살아 있으므로 I7·I9 의 "변경 전과 바이트 동일"을 A/B 로 직접 판정할 수 있다.
운영 서버(192.168.102.245 / 112.216.247.186)는 절대 대상이 아니다 — 기본값도 로컬이다.

실행:
  python3 server/test_incheon_sgg_remap_integration.py
  OURS=http://127.0.0.1:8093 BEFORE=http://127.0.0.1:8092 python3 server/…  # 재정의

TDD 순서상 S6 구현 **전에는 대부분 실패**하는 것이 정상이다.
"""
import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request

OURS = os.environ.get("OURS", "http://127.0.0.1:8093").rstrip("/")
BEFORE = os.environ.get("BEFORE", "http://127.0.0.1:8092").rstrip("/")
TIMEOUT = float(os.environ.get("GEOCODE_TIMEOUT", "15"))

# 인천 신설 4구 / 옛 3구
NEW_SGG = ("제물포구", "영종구", "서해구", "검단구")
OLD_SGG = ("중구", "동구", "서구")


# ─────────────────────────────────────────────────────────────────────────────
# HTTP 헬퍼
# ─────────────────────────────────────────────────────────────────────────────
def _get(base, path, **params):
    q = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    url = f"{base}{path}?{q}"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        raw = r.read().decode("utf-8")
    return json.loads(raw), raw


def geocode(base, q):
    d, raw = _get(base, "/geocode", q=q)
    return d, raw


def reverse(base, lon, lat):
    d, raw = _get(base, "/reverse", lon=lon, lat=lat)
    return d, raw


def results_of(d):
    return d.get("results") or []


def _alive(base):
    try:
        _get(base, "/geocode", q="테헤란로 152")
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# I1 좌표 — 옛 3구(28110/28140/28260) 읍면동 11건.
# 선정 근거: VWorld dsId=30505 의 OLD_LAWDCD 대응쌍에서 4개 신설구를 모두 덮도록 고르고,
#           좌표는 로컬 PostGIS `address` 에서 `WHERE bcode LIKE '<old8>%' ORDER BY bcode, jibun LIMIT 1`
#           로 **결정적으로** 뽑았다(임의 표본 아님 — 재현 가능).
#           계획서가 말하는 "T021 목록"은 계획서 본문에 실물이 없어 이 절차로 재구성했다.
# ─────────────────────────────────────────────────────────────────────────────
I1_POINTS = [
    # (옛 8자리 법정동코드, 기대 신 구명, 옛 표기, lon, lat, 신 8자리 코드)
    ("28110102", "제물포구", "중구 중앙동2가", 126.6224470, 37.4726870, "28125109"),
    ("28110124", "제물포구", "중구 신포동", 126.6264650, 37.4714740, "28125131"),
    ("28110132", "제물포구", "중구 도원동", 126.6415760, 37.4679770, "28125139"),
    ("28110145", "영종구", "중구 중산동", 126.5544350, 37.4993210, "28155101"),
    ("28110147", "영종구", "중구 운서동", 126.4926650, 37.4976190, "28155103"),
    ("28140106", "제물포구", "동구 금곡동", 126.6359440, 37.4728140, "28125106"),
    ("28140107", "제물포구", "동구 송림동", 126.6506610, 37.4719080, "28125107"),
    ("28260103", "서해구", "서구 검암동", 126.7018560, 37.5628110, "28275101"),
    ("28260110", "서해구", "서구 석남동", 126.6678480, 37.5115190, "28275108"),
    ("28260117", "검단구", "서구 대곡동", 126.6592680, 37.6357680, "28290106"),
    ("28260119", "검단구", "서구 오류동", 126.6247790, 37.5643620, "28290108"),
]

# I6 — 중앙동1가. VWorld CSV 에 신코드 행이 아예 없어 S4 수기 보정(2811010100 → 2812510800)으로만
#      덮이는 유일한 행. Conductor 재가 사항(§4-5(b) 채택)이므로 기대값은 "제물포구".
I6_POINT = ("28110101", "제물포구", "중구 중앙동1가", 126.6207330, 37.4736190, "28125108")

# I7 — 전남·광주(12체계) 무회귀 표본. 이미 치환이 끝난 영역이라 우리 변경이 절대 건드리면 안 된다.
I7_POINTS = [
    ("전남 목포 용당동", 126.4012790, 34.8030060),
    ("광주 동구 대인동", 126.9144080, 35.1526820),
]

# I8 — 다중 코드체계. `areas[].adm_dong` 은 법정동코드가 **아니다**.
#      세종 반곡동의 adm_dong 코드는 `29010513` 로 "29"(광주 법정동 접두)와 겹치지만 광주가 아니다.
#      여기에 remap_bcode 를 먹이면 세종이 전남광주통합특별시로 오치환된다.
#      인천 adm_dong 은 "23" 접두(23010560)라 우리 "28" 분기와는 겹치지 않지만 함께 고정한다.
I8_CASES = [
    ("세종 반곡동", 127.3121910, 36.4938120, "29010513"),
    ("인천 옛중구 도원동", 126.6415760, 37.4679770, "23010560"),
    ("인천 옛서구 대곡동", 126.6592680, 37.6357680, "23080810"),
]

# I9 — T020(순방향 지번 파서) 회귀 표본. 결함 1(공백 없는 리+번지)·2(리 토큰 소실)·3(꼬리 중복/잡토큰).
#      인천·전남광주와 무관한 지역이므로 before 와 **바이트 동일**이어야 한다.
I9_QUERIES = [
    "경기도 김포시 월곶면 성동리263-8",
    "충청남도 논산시 은진면 연서리450",
    "경상남도 통영시 한산면 염호리 233",
    "강원특별자치도 홍천군 내면 방내시장길 32 방내시장길 32",
    "충청남도 아산시 영인면 신운남길 46 .",
]


def _structures(d):
    """응답에서 structure 딕셔너리를 전부 긁는다 (address / nearest[] / results[] 중첩 포함)."""
    out = []

    def walk(o):
        if isinstance(o, dict):
            if "sido" in o and ("sigungu" in o or "b_code" in o):
                out.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(d)
    return out


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _alive(OURS):
            raise unittest.SkipTest(f"OURS 미기동: {OURS}")


class TestI1IncheonReverse(_Base):
    """I1 — 인천 11건 좌표가 신 구명을 반환한다 (핵심)."""

    def test_i1_new_sgg_names(self):
        bad = []
        for old8, want, label, lon, lat, new8 in I1_POINTS:
            d, _ = reverse(OURS, lon, lat)
            st = (d.get("address") or {}).get("structure") or {}
            got = st.get("sigungu")
            if got != want:
                bad.append(f"{label}({old8}) sigungu={got!r} 기대={want!r}")
        self.assertEqual(bad, [], "신 구명 미반환:\n  " + "\n  ".join(bad))

    def test_i1b_new_bcode(self):
        """L2-2 — b_code 가 신 코드로 반환된다."""
        bad = []
        for old8, want, label, lon, lat, new8 in I1_POINTS:
            d, _ = reverse(OURS, lon, lat)
            st = (d.get("address") or {}).get("structure") or {}
            b = (st.get("b_code") or "")
            if not b.startswith(new8):
                bad.append(f"{label} b_code={b!r} 기대접두={new8!r}")
        self.assertEqual(bad, [], "신 b_code 미반환:\n  " + "\n  ".join(bad))


class TestI2I3Roundtrip(_Base):
    """I2 / I2-b / I3 — 신 구명 질의 왕복, 반증, 옛 이름 폐기 금지."""

    def test_i2_new_sgg_query_hits(self):
        d, _ = geocode(OURS, "인천 제물포구 도원동 33-12")
        self.assertEqual(len(results_of(d)), 1, "신 구명 질의가 1건이어야 한다")

    def test_i2b_nonexistent_sgg_returns_zero(self):
        """S6c(미등록 토큰 차단) 채택 시에만 충족. 미채택이면 이 테스트는 실패로 남기고
        impl-notes.md 에 **미충족**으로 보고한다 — 묵살 금지."""
        d, _ = geocode(OURS, "존재하지않는구 도원동 33-12")
        self.assertEqual(len(results_of(d)), 0,
                         "존재하지 않는 구 토큰을 무시하고 결과를 냈다 (지역 좁힘 미작동)")

    def test_i3_old_sgg_query_still_hits(self):
        d, _ = geocode(OURS, "인천 중구 도원동 33-12")
        self.assertEqual(len(results_of(d)), 1, "옛 구명 입력은 계속 히트해야 한다(폐기 금지)")


class TestI4DuplicateEmd(_Base):
    """I4 — 계양구 오류동. 서구(→검단구) 오류동으로 오히트 금지."""

    def test_i4_gyeyang_oryu(self):
        d, _ = geocode(OURS, "계양구 오류동")
        res = results_of(d)
        self.assertGreaterEqual(len(res), 1, "계양구 오류동이 0건")
        top = res[0]
        blob = json.dumps(top, ensure_ascii=False)
        self.assertIn("계양구", blob, f"1순위가 계양구가 아니다: {blob[:200]}")
        self.assertNotIn("검단구", blob, "검단구 오류동으로 오히트")


class TestI5AliasNotOverreaching(_Base):
    """I5 / I5-b — 별칭 과확장 차단. 대곡동은 검단구 소속이지 서해구가 아니다."""

    def test_i5_seohae_daegok_zero(self):
        d, _ = geocode(OURS, "서해구 대곡동 533-1")
        self.assertEqual(len(results_of(d)), 0,
                         "서해구 대곡동은 0건이어야 한다(대곡동은 검단구 소속)")

    def test_i5b_geomdan_daegok_one(self):
        d, _ = geocode(OURS, "검단구 대곡동 533-1")
        res = results_of(d)
        self.assertEqual(len(res), 1, "검단구 대곡동은 1건이어야 한다")
        blob = json.dumps(res[0], ensure_ascii=False)
        self.assertIn("검단구", blob, f"결과에 신 구명이 없다: {blob[:200]}")
        self.assertNotIn("서구", blob, "옛 구명 잔존")


class TestI6MissingRow(_Base):
    """I6 — 중앙동1가. §4-5(b) 채택이므로 제물포구가 나와야 한다."""

    def test_i6_jungangdong1ga(self):
        old8, want, label, lon, lat, new8 = I6_POINT
        d, _ = reverse(OURS, lon, lat)
        st = (d.get("address") or {}).get("structure") or {}
        self.assertEqual(st.get("sigungu"), want,
                         f"{label} → {st.get('sigungu')!r} (기대 {want!r}, S4 수기보정 미적용?)")
        self.assertTrue((st.get("b_code") or "").startswith(new8),
                        f"b_code={st.get('b_code')!r} 기대접두={new8!r}")


class TestI7JeonnamGwangjuNoRegression(_Base):
    """I7 — 전남·광주(12체계) 표본이 변경 전과 바이트 동일."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not _alive(BEFORE):
            raise unittest.SkipTest(f"BEFORE 미기동: {BEFORE}")

    def test_i7_byte_identical(self):
        diff = []
        for label, lon, lat in I7_POINTS:
            _, a = reverse(BEFORE, lon, lat)
            _, b = reverse(OURS, lon, lat)
            if a != b:
                diff.append(f"{label} reverse 바이트 불일치 ({len(a)}B vs {len(b)}B)")
        self.assertEqual(diff, [], "전남·광주 회귀:\n  " + "\n  ".join(diff))


class TestI8MultiCodeSystem(_Base):
    """I8 — areas[] 의 adm_dong 코드는 법정동코드가 아니므로 치환 금지."""

    def test_i8_adm_dong_code_untouched(self):
        bad = []
        for label, lon, lat, want in I8_CASES:
            d, _ = reverse(OURS, lon, lat)
            codes = [a.get("code") for a in (d.get("areas") or [])
                     if a.get("type") == "adm_dong"]
            if want not in codes:
                bad.append(f"{label} adm_dong={codes} 기대포함={want!r}")
        self.assertEqual(bad, [], "adm_dong 코드 오치환:\n  " + "\n  ".join(bad))


class TestI9T020Regression(_Base):
    """I9 — T020(지번 파서) 수정 상태 유지. 인천·전남광주 무관 표본이라 바이트 동일."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not _alive(BEFORE):
            raise unittest.SkipTest(f"BEFORE 미기동: {BEFORE}")

    def test_i9_byte_identical(self):
        diff = []
        for q in I9_QUERIES:
            _, a = geocode(BEFORE, q)
            _, b = geocode(OURS, q)
            if a != b:
                diff.append(f"{q!r} 바이트 불일치 ({len(a)}B vs {len(b)}B)")
        self.assertEqual(diff, [], "T020 표본 회귀:\n  " + "\n  ".join(diff))


class TestI10Full595(_Base):
    """I10 — 595 전량 회귀. **실행 불가**(계획서 §7-4). §9-4 대체 가드로 갈음한다."""

    def test_i10_not_executed(self):
        raise unittest.SkipTest(
            "I10 미실행 — 595 원본 xlsx 기반 채점기는 이 환경에서 재현 불가(계획서 §7-4). "
            "§9-4 인천외 시도 표본 바이트 차분 가드(scripts/verify-incheon-remap.sh)로 대체하며, "
            "그로 인해 완료기준 6(기존 인천 결과 회귀 없음)의 검증 강도가 낮아진다.")


class TestI11NoOldSggAnywhere(_Base):
    """I11 — 인천 응답 JSON 전문에 옛 구명 문자열이 0곳 (C-1 완료 판정).

    필드를 열거하지 않는다. §4-4 ④ 대응표가 한 지점이라도 빠지면 자동으로 실패한다.
    오탐 방지: `structure.sido == "인천광역시"` 인 응답에 대해서만 단언한다."""

    def _assert_clean(self, d, ctx):
        sidos = {s.get("sido") for s in _structures(d)}
        self.assertIn("인천광역시", sidos, f"{ctx}: 인천 응답이 아님 (sido={sidos})")
        body = json.dumps(d, ensure_ascii=False)
        hits = []
        for old in OLD_SGG:
            n = body.count(f'"{old}"') + body.count(f"{old} ")
            if n:
                hits.append(f"{old}×{n}")
        self.assertEqual(hits, [], f"{ctx}: 옛 구명 잔존 {hits}")

    def test_i11_search_response(self):
        d, _ = geocode(OURS, "인천 중구 도원동 33-12")
        self.assertEqual(len(results_of(d)), 1)
        self._assert_clean(d, "geocode(인천 중구 도원동 33-12)")

    def test_i11_reverse_response(self):
        for old8, want, label, lon, lat, new8 in I1_POINTS:
            d, _ = reverse(OURS, lon, lat)
            with self.subTest(point=label):
                self._assert_clean(d, f"reverse({label})")


if __name__ == "__main__":
    print(f"OURS={OURS}  BEFORE={BEFORE}")
    unittest.main(verbosity=2)
