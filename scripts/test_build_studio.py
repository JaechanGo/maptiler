#!/usr/bin/env python3
"""build-studio.py 수집 무결성 게이트 단위테스트 (네트워크 비의존).

대상 파일명이 하이픈(`build-studio.py`)이라 일반 import 불가 →
importlib.util.spec_from_file_location 로 모듈 핸들 확보(선례 server/test_geocode_api.py 동일 기법).

검증 대상:
  _payload_integrity(path) -> (verdict, detail)  … 신규 순수함수
  _extract_into(src, dest_dir, orig_name=None)   … .csv 폴백 구멍 봉쇄
  _http_download(url, dest, ...)                 … Content-Length 대조(urlopen monkeypatch)

import 안전성: Manager.__init__ 은 데몬 워커 스레드만 기동(포트 바인딩은 __main__ 한정)이라 로드 안전.
BUILD_HOME 을 임시디렉토리로 지정해 실 홈(~/geocode-build) 비오염.

실행:  python3 scripts/test_build_studio.py
       또는  python3 -m unittest scripts.test_build_studio -v
"""
import importlib.util
import io
import os
import shutil
import tempfile
import unittest

# ── 모듈 로드 (하이픈 파일명 대응, BUILD_HOME=tmp 로 실홈 비오염) ──────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(_HERE, "build-studio.py")
_BUILD_HOME_TMP = tempfile.mkdtemp(prefix="build_studio_test_home_")
os.environ.setdefault("BUILD_HOME", _BUILD_HOME_TMP)


def _load_module(path=_MOD_PATH):
    spec = importlib.util.spec_from_file_location("build_studio", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_module()


# ── 합성 픽스처 헬퍼 ──────────────────────────────────────────────
def _make_valid_zip(path, names=("a.csv",), payload=b"name,lon,lat\nfoo,127.0,37.0\n"):
    import zipfile as _zf
    with _zf.ZipFile(path, "w", _zf.ZIP_DEFLATED) as z:
        for nm in names:
            z.writestr(nm, payload)
    return path


def _make_truncated_zip(path):
    """정상 zip 바이트를 만든 뒤 끝쪽을 잘라 EOCD/중앙디렉토리를 제거(=sangga 절단 재현).
    선두는 PK\\x03\\x04 유지, zipfile.is_zipfile()==False 가 되도록 함."""
    import zipfile as _zf
    buf = io.BytesIO()
    with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as z:
        # 본문이 충분히 길도록 — 절단 후에도 local header + 일부 본문이 남게
        z.writestr("big.csv", b"name,lon,lat\n" + b"row,127.0,37.0\n" * 500)
    raw = buf.getvalue()
    truncated = raw[: len(raw) // 2]   # 뒤 절반 제거 → EOCD/중앙디렉토리 소실
    with open(path, "wb") as f:
        f.write(truncated)
    return path


def _make_valid_tar(path):
    import tarfile as _tf
    payload = b"name,lon,lat\nbar,128.0,36.0\n"
    with _tf.open(path, "w") as t:
        info = _tf.TarInfo(name="b.csv")
        info.size = len(payload)
        t.addfile(info, io.BytesIO(payload))
    return path


def _make_empty_zip(path):
    """PK\\x05\\x06 (빈 zip EOCD) — 멤버 없는 정상 zip."""
    import zipfile as _zf
    with _zf.ZipFile(path, "w"):
        pass
    return path


# ── 가짜 HTTP 응답 (urlopen monkeypatch 용) ──────────────────────
class _FakeHeaders:
    def __init__(self, d):
        self._d = {k.lower(): v for k, v in d.items()}

    def get(self, key, default=None):
        return self._d.get(key.lower(), default)


class _FakeResp:
    """_http_download 가 기대하는 응답객체: geturl()/read(n)/headers + 컨텍스트매니저."""

    def __init__(self, body, headers, url="https://example.test/file"):
        self._buf = io.BytesIO(body)
        self.headers = _FakeHeaders(headers)
        self._url = url

    def geturl(self):
        return self._url

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ════════════════════════════════════════════════════════════════
# _payload_integrity — 순수 판정 단위테스트
# ════════════════════════════════════════════════════════════════
class TestPayloadIntegrity(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="pi_")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _p(self, name):
        return os.path.join(self.td, name)

    def test_T1_valid_zip_healthy(self):
        p = _make_valid_zip(self._p("ok.zip"))
        verdict, _ = M._payload_integrity(p)
        self.assertEqual(verdict, "healthy")

    def test_T2_truncated_zip_truncated(self):
        p = _make_truncated_zip(self._p("trunc"))
        import zipfile as _zf
        self.assertFalse(_zf.is_zipfile(p), "픽스처가 절단 zip 이어야 함(is_zipfile False)")
        with open(p, "rb") as f:
            self.assertEqual(f.read(4), b"PK\x03\x04", "선두는 ZIP local header 매직")
        verdict, _ = M._payload_integrity(p)
        self.assertEqual(verdict, "truncated")

    def test_T3_plain_csv_healthy(self):
        p = self._p("plain")
        with open(p, "wb") as f:
            f.write("상호명,경도,위도\n가게,127.0,37.0\n".encode("utf-8"))
        verdict, _ = M._payload_integrity(p)
        self.assertEqual(verdict, "healthy")

    def test_T4_valid_tar_healthy(self):
        p = _make_valid_tar(self._p("ok.tar"))
        verdict, _ = M._payload_integrity(p)
        self.assertEqual(verdict, "healthy")

    def test_T5_zero_byte_corrupt(self):
        p = self._p("empty")
        open(p, "wb").close()
        verdict, _ = M._payload_integrity(p)
        self.assertEqual(verdict, "corrupt")

    def test_T8_empty_zip_healthy(self):
        p = _make_empty_zip(self._p("empty.zip"))
        verdict, _ = M._payload_integrity(p)
        self.assertEqual(verdict, "healthy")

    def test_T6_7z_magic_only(self):
        p = self._p("part7z")
        with open(p, "wb") as f:
            f.write(b"7z\xbc\xaf\x27\x1c")   # 7z 매직 6바이트만(불완전)
        verdict, _ = M._payload_integrity(p)
        tool = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
        if tool:
            self.assertEqual(verdict, "corrupt")
        else:
            self.assertEqual(verdict, "unknown")

    def test_T7_gzip_healthy_and_truncated(self):
        import gzip as _gz
        good = self._p("ok.gz")
        with _gz.open(good, "wb") as g:
            g.write(b"name,lon,lat\n" + b"row,127.0,37.0\n" * 200)
        self.assertEqual(M._payload_integrity(good)[0], "healthy")

        bad = self._p("trunc.gz")
        with open(good, "rb") as f:
            raw = f.read()
        with open(bad, "wb") as f:
            f.write(raw[: len(raw) // 2])   # gzip 절단
        # 경량판이면 unknown 허용 — 단 healthy/corrupt 가 아니어야 함
        self.assertIn(M._payload_integrity(bad)[0], ("truncated", "unknown"))


# ════════════════════════════════════════════════════════════════
# _extract_into — .csv 사일런트 복사 구멍 봉쇄
# ════════════════════════════════════════════════════════════════
class TestExtractInto(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="ei_")
        self.dest = os.path.join(self.td, "staged")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _csv_files(self):
        import glob
        return glob.glob(os.path.join(self.dest, "**", "*.csv"), recursive=True)

    def test_E1_truncated_zip_raises_and_no_csv(self):
        # 확장자 없는 절단 zip (store 의 <sha> 모사)
        src = os.path.join(self.td, "deadbeefsha")
        _make_truncated_zip(src)
        with self.assertRaises(Exception):
            M._extract_into(src, self.dest)
        self.assertEqual(self._csv_files(), [], "절단 zip 이 *.csv 로 staged 에 남으면 안 됨")

    def test_E2_plain_csv_copied_to_csv(self):
        src = os.path.join(self.td, "plainsha")
        with open(src, "wb") as f:
            f.write("상호명,경도,위도\n가게,127.0,37.0\n".encode("utf-8"))
        M._extract_into(src, self.dest)
        self.assertEqual(len(self._csv_files()), 1, "정상 평문은 <name>.csv 로 복사되어야 함")

    def test_E3_valid_zip_extracted(self):
        src = os.path.join(self.td, "okzipsha")
        _make_valid_zip(src, names=("member.csv",))
        M._extract_into(src, self.dest)
        found = []
        for root, _dirs, files in os.walk(self.dest):
            found.extend(files)
        self.assertIn("member.csv", found, "정상 zip 은 멤버가 추출되어야 함")


# ════════════════════════════════════════════════════════════════
# _http_download — Content-Length 대조 (urlopen monkeypatch)
# ════════════════════════════════════════════════════════════════
class TestHttpDownloadCL(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="dl_")
        self._orig_urlopen = M.urllib.request.urlopen

    def tearDown(self):
        M.urllib.request.urlopen = self._orig_urlopen
        shutil.rmtree(self.td, ignore_errors=True)

    def _patch(self, body, headers):
        def fake_urlopen(req, timeout=None, context=None):
            return _FakeResp(body, headers)
        M.urllib.request.urlopen = fake_urlopen

    def test_H1_content_length_match_ok(self):
        body = b"name,lon,lat\n" + b"row,127.0,37.0\n" * 50
        self._patch(body, {"Content-Length": str(len(body))})
        dest = os.path.join(self.td, "h1.part")
        size = M._http_download("https://example.test/f", dest, retries=1)
        self.assertEqual(size, len(body))
        self.assertEqual(os.path.getsize(dest), len(body))

    def test_H2_content_length_short_raises(self):
        body = b"name,lon,lat\n" + b"row,127.0,37.0\n" * 50
        # 서버 선언 CL 이 실수신보다 큼(조기 EOF=절단)
        self._patch(body, {"Content-Length": str(len(body) + 9999)})
        dest = os.path.join(self.td, "h2.part")
        with self.assertRaises(Exception) as ctx:
            M._http_download("https://example.test/f", dest, retries=1)
        self.assertIn("절단", str(ctx.exception))

    def test_H3_no_content_length_ok(self):
        body = b"name,lon,lat\nrow,1,2\n"
        self._patch(body, {})   # CL 헤더 부재(chunked)
        dest = os.path.join(self.td, "h3.part")
        size = M._http_download("https://example.test/f", dest, retries=1)
        self.assertEqual(size, len(body))

    def test_H4_content_encoding_gzip_skips_check(self):
        body = b"compressed-bytes-shorter"
        # transport gzip: CL=압축크기 ≠ 디스크크기 가능 → 대조 생략(오탐 방지)
        self._patch(body, {"Content-Length": str(len(body) + 5000),
                           "Content-Encoding": "gzip"})
        dest = os.path.join(self.td, "h4.part")
        size = M._http_download("https://example.test/f", dest, retries=1)
        self.assertEqual(size, len(body))


if __name__ == "__main__":
    unittest.main(verbosity=2)
