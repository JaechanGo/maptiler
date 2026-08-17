#!/usr/bin/env python3
"""scripts/_common/textnorm.py — 이식 정확성·발산 보존·사본 동기화 테스트 (T028 §9 T1·T2·T3).

stdlib unittest 만 사용(pytest 미설치). 실행: python3 scripts/test_textnorm.py

T1 골든 케이스     — 각 함수의 계약을 표로 고정
T2 이식 등가성     — 이식 전 원본 구현을 _legacy_* 로 복제해 무작위 1,000건 대조.
                     "리팩터 중 로직이 슬쩍 바뀌는" 사고를 원천 차단한다.
T3 사본 동기화     — server/ 2파일은 컨테이너 빌드 컨텍스트 경계(§4.4) 때문에 import 가
                     불가능해 인라인 사본으로 남는다. 문자 단위 비교는 인용부호·공백
                     차이로 즉시 실패하므로, 소스 조각 추출 → exec 행동 등가 +
                     ast 구조 비교로 강제한다.
"""
import ast
import os
import random
import re
import sys
import unicodedata
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)

from _common import textnorm  # noqa: E402
from _common.textnorm import (  # noqa: E402
    biznrm_nfc,
    biznrm_nfkc,
    corenrm,
    norm,
    rnorm,
)

# ── 이식 전 원본 구현 복제(T2 대조군) ────────────────────────────────
# 아래 5개는 커밋 3 이전의 09-gen-geocode.py / dedup_er.py 원문이다. 손대지 말 것.


def _legacy_norm(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip()


def _legacy_rnorm(s):
    return re.sub(r"[.\s]", "", unicodedata.normalize("NFC", s or ""))


_LEG_BIZ_PUNCT = re.compile(r"[\s()\[\]{}<>（）【】·.,/&-]+")          # 09:139 원문
_LEG_PUNCT = re.compile(r"[\s()\[\]{}<>（）【】·.,/&\-]+")             # dedup_er:24 원문
_LEG_CORP = re.compile(
    r"(주식회사|유한회사|유한책임회사|합자회사|합명회사|재단법인|사단법인|의료법인|\(주\)|㈜|\(유\)|\(재\)|\(사\))"
)
_LEG_BRANCH_TOK = re.compile(r"^(본점|직영점|가맹점|영업소|지점|\d{1,3}호점|.{1,5}점)$")


def _legacy_biznrm_nfc(s):
    return _LEG_BIZ_PUNCT.sub("", unicodedata.normalize("NFC", s or "")).lower()


def _legacy_biznrm_nfkc(s):
    return _LEG_PUNCT.sub("", unicodedata.normalize("NFKC", s or "")).lower()


def _legacy_corenrm(s):
    t = unicodedata.normalize("NFKC", s or "")
    t = _LEG_CORP.sub("", t)
    toks = t.split()
    if len(toks) >= 2 and _LEG_BRANCH_TOK.match(toks[-1]):
        toks = toks[:-1]
    core = _legacy_biznrm_nfkc(" ".join(toks))
    return core or _legacy_biznrm_nfkc(s)


# ── T1. 골든 케이스 ──────────────────────────────────────────────────

_NFD_GA = "가"        # ᄀ + ᅡ (NFD) → NFC "가"
_NFD_AGRAVE = "À"         # A + combining grave → NFC "À"


class TestGoldenCases(unittest.TestCase):
    def test_norm(self):
        for src, want in [
            (None, ""),
            ("", ""),
            ("  가  나 ", "가 나"),
            ("\t줄\n바꿈 ", "줄 바꿈"),
            ("서울특별시   중구", "서울특별시 중구"),
            (_NFD_GA, "가"),
            (_NFD_AGRAVE, "À"),
        ]:
            with self.subTest(src=src):
                self.assertEqual(norm(src), want)

    def test_rnorm(self):
        for src, want in [
            (None, ""),
            ("", ""),
            ("3.1만세로 5", "31만세로5"),
            ("  가  나 ", "가나"),
            (_NFD_GA, "가"),
        ]:
            with self.subTest(src=src):
                self.assertEqual(rnorm(src), want)

    def test_biznrm_nfc(self):
        for src, want in [
            (None, ""),
            ("", ""),
            ("(주)한국", "주한국"),
            ("㈜한국", "㈜한국"),          # NFC 는 ㈜ 를 분해하지 않는다
            ("스타 벅스-강남", "스타벅스강남"),
            ("ＡＢ  ｃ", "ａｂｃ"),          # 전각 유지 + lower
        ]:
            with self.subTest(src=src):
                self.assertEqual(biznrm_nfc(src), want)

    def test_biznrm_nfkc(self):
        for src, want in [
            (None, ""),
            ("", ""),
            ("(주)한국", "주한국"),
            ("㈜한국", "주한국"),          # NFKC 가 ㈜ → (주) 로 분해 후 괄호 제거
            ("스타 벅스-강남", "스타벅스강남"),
            ("ＡＢ  ｃ", "abc"),            # 전각 → 반각
        ]:
            with self.subTest(src=src):
                self.assertEqual(biznrm_nfkc(src), want)

    def test_nfc_nfkc_divergence_is_intentional(self):
        """발산이 의도된 것임을 테스트가 기록한다(§4.2).

        전수 9,061행에서 실제로 갈라진다. 통합하면 재빌드 없이는 발견되지 않는
        회귀가 되므로 두 별칭은 분리 유지한다.
        """
        self.assertNotEqual(biznrm_nfc("㈜한국"), biznrm_nfkc("㈜한국"))
        self.assertNotEqual(biznrm_nfc("ＡＢ  ｃ"), biznrm_nfkc("ＡＢ  ｃ"))
        self.assertEqual(biznrm_nfc("㈜한국"), "㈜한국")
        self.assertEqual(biznrm_nfkc("㈜한국"), "주한국")

    def test_corenrm(self):
        for src, want in [
            (None, ""),
            ("", ""),
            ("주식회사 한국", "한국"),
            ("주식회사 ＡＢ 강남점", "ab"),      # 법인격 + 지점토큰 제거, 전각 → 반각
            ("파리바게뜨신촌점", "파리바게뜨신촌점"),  # 무공백 상호는 깎지 않는다
        ]:
            with self.subTest(src=src):
                self.assertEqual(corenrm(src), want)

    def test_corenrm_is_bound_to_nfkc(self):
        """corenrm 은 반드시 biznrm_nfkc 에 바인딩돼야 한다(§4.3).

        biznrm_nfc 에 잘못 붙이면 dedup_er 의 is_primary 판정이 조용히 바뀐다.
        "㈜" 단독 입력은 _CORP 로 전부 깎여 `core or biznrm(s)` 폴백을 타는데,
        그 폴백만이 **원본** 문자열을 넘기므로 NFC/NFKC 차이가 드러난다.
        """
        self.assertEqual(corenrm("㈜"), biznrm_nfkc("㈜"))
        self.assertEqual(corenrm("㈜"), "주")
        self.assertNotEqual(corenrm("㈜"), biznrm_nfc("㈜"))


# ── T2. 이식 등가성 ──────────────────────────────────────────────────

_POOL = list("가나다라마 ABC abc ＡＢＣ ａｂｃ ()[]{}<>（）【】·.,/&-\t\n0123 점호") + [
    "㈜",
    "(주)",
    "주식회사",
    "강남점",
    "12호점",
    _NFD_GA,
    _NFD_AGRAVE,
    "　",   # 전각 공백
]


class TestPortingEquivalence(unittest.TestCase):
    """이식본 == 원본. 무작위 1,000건 + 골든 입력."""

    PAIRS = (
        ("norm", norm, _legacy_norm),
        ("rnorm", rnorm, _legacy_rnorm),
        ("biznrm_nfc", biznrm_nfc, _legacy_biznrm_nfc),
        ("biznrm_nfkc", biznrm_nfkc, _legacy_biznrm_nfkc),
        ("corenrm", corenrm, _legacy_corenrm),
    )

    def _inputs(self):
        rnd = random.Random(20280818)        # 결정적 시드 — 실패 재현 가능
        out = [None, "", " ", "\t", "㈜", "(주)", "주식회사 한국 강남점", "ＡＢ  ｃ"]
        for _ in range(1000):
            n = rnd.randint(0, 12)
            out.append("".join(rnd.choice(_POOL) for _ in range(n)))
        return out

    def test_all_functions_match_legacy(self):
        inputs = self._inputs()
        for name, new, legacy in self.PAIRS:
            for src in inputs:
                got, want = new(src), legacy(src)
                if got != want:
                    self.fail(
                        "%s(%r) 이식 회귀: %r != 원본 %r" % (name, src, got, want)
                    )

    def test_punct_constants_kept_separate(self):
        """_PUNCT / _BIZ_PUNCT 는 통합하지 않는다(§4.3).

        정규식 의미는 같으나(문자클래스 끝의 '-' 는 리터럴) 문자열이 다르다.
        합치면 "원문 그대로" 원칙이 깨지고 T3 소스 대조와 충돌한다.
        """
        self.assertNotEqual(textnorm._PUNCT.pattern, textnorm._BIZ_PUNCT.pattern)
        self.assertEqual(textnorm._PUNCT.pattern, _LEG_PUNCT.pattern)
        self.assertEqual(textnorm._BIZ_PUNCT.pattern, _LEG_BIZ_PUNCT.pattern)


# ── T3. 사본 동기화 ──────────────────────────────────────────────────

_COPIES = {
    "09-gen-geocode.py": os.path.join(HERE, "09-gen-geocode.py"),
    "geocode-api.py": os.path.join(ROOT, "server", "geocode-api.py"),
    "geocode-api-pg.py": os.path.join(ROOT, "server", "geocode-api-pg.py"),
    "osm-from-mbtiles.py": os.path.join(HERE, "osm-from-mbtiles.py"),
}
_CANON = os.path.join(HERE, "_common", "textnorm.py")

_SYNC_INPUTS = (
    None,
    "",
    "  가  나 ",
    "\t줄\n바꿈 ",
    _NFD_GA,
    _NFD_AGRAVE,
    "ＡＢ  ｃ",
    "3.1만세로 5",
)


def _extract_def(path, fname):
    """파일에서 `def <fname>(s): ...` 한 줄 정의만 뽑는다.

    파일 전체 import 를 피한다 — 모듈 최상단이 DB 경로를 잡을 수 있어
    환경 의존성이 생긴다. 소스 조각 + exec 는 그 의존을 0으로 만든다.
    """
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"^def %s\(s\):.*$" % re.escape(fname), src, re.M)
    return m.group(0) if m else None


def _compile_def(snippet):
    ns = {"re": re, "unicodedata": unicodedata}
    exec(snippet, ns)                                    # noqa: S102 — 자기 리포 소스만
    return ns


class TestCopySync(unittest.TestCase):
    """server/ 2파일 + 빌드측 사본이 정본과 행동 등가인지 강제한다."""

    def _assert_synced(self, filename, fname):
        snippet = _extract_def(_COPIES[filename], fname)
        self.assertIsNotNone(
            snippet, "%s 에서 def %s 를 찾지 못했다" % (filename, fname)
        )
        canon_snippet = _extract_def(_CANON, fname)
        self.assertIsNotNone(canon_snippet, "정본에서 def %s 를 찾지 못했다" % fname)

        copy_fn = _compile_def(snippet)[fname]
        canon_fn = getattr(textnorm, fname)

        for src in _SYNC_INPUTS:                          # ① 행동 등가
            self.assertEqual(
                copy_fn(src),
                canon_fn(src),
                "%s 의 %s(%r) 가 정본과 다르다" % (filename, fname, src),
            )

        self.assertEqual(                                 # ② 구조 등가(인용부호·공백 차이는 통과)
            ast.dump(ast.parse(snippet), annotate_fields=False),
            ast.dump(ast.parse(canon_snippet), annotate_fields=False),
            "%s 의 %s 가 정본과 구조가 다르다" % (filename, fname),
        )

    def test_09_gen_geocode_norm(self):
        self._assert_synced("09-gen-geocode.py", "norm")

    def test_09_gen_geocode_rnorm(self):
        self._assert_synced("09-gen-geocode.py", "rnorm")

    def test_geocode_api_norm(self):
        self._assert_synced("geocode-api.py", "norm")

    def test_geocode_api_rnorm(self):
        self._assert_synced("geocode-api.py", "rnorm")

    def test_geocode_api_pg_norm(self):
        self._assert_synced("geocode-api-pg.py", "norm")

    def test_geocode_api_pg_rnorm(self):
        self._assert_synced("geocode-api-pg.py", "rnorm")

    def test_osm_from_mbtiles_copy_removed(self):
        """osm-from-mbtiles.py 는 정본을 import 한다 — 사본이 있으면 안 된다.

        커밋 3 시점엔 이 파일에 `s or ''` 가 빠진 사본이 있어 norm(None) 이
        TypeError 였고 expectedFailure 로 기록했다. 커밋 4 에서 임포트로 전환해
        사본 자체를 없앴으므로, 이제는 "사본 대조"가 아니라 "사본 소멸 확인"이다.
        """
        path = _COPIES["osm-from-mbtiles.py"]
        self.assertIsNone(
            _extract_def(path, "norm"),
            "osm-from-mbtiles.py 에 norm 인라인 사본이 되살아났다 — 정본 import 로 되돌릴 것",
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("from _common.textnorm import norm", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
