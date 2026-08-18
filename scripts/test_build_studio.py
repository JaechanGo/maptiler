#!/usr/bin/env python3
"""build-studio.py 수집 무결성 게이트 단위테스트 (네트워크 비의존).

대상 파일명이 하이픈(`build-studio.py`)이라 일반 import 불가 →
importlib.util.spec_from_file_location 로 모듈 핸들 확보(선례 server/test_geocode_api.py 동일 기법).

검증 대상:
  _payload_integrity(path) -> (verdict, detail)  … 신규 순수함수
  _extract_into(src, dest_dir, orig_name=None)   … .csv 폴백 구멍 봉쇄
  _http_download(url, dest, ...)                 … Content-Length 대조(urlopen monkeypatch)
  IsolatedBuildHome                              … 실홈 파괴 차단 안전망(그 자체를 I1-I15 로 검증)

import 안전성: Manager.__init__ 은 데몬 워커 스레드만 기동(포트 바인딩은 __main__ 한정)이라 로드 안전.
격리: 경로형 환경변수를 import 시점에 무조건 덮어써 BUILD_HOME 을 임시디렉토리로 고정하고,
IsolatedBuildHome 이 파생 상수 전량을 재바인딩한 뒤 파괴 프리미티브 6종을 감시한다.
안전 경계는 `staged/` 가 아니라 **BUILD_HOME** 이다 — 최대 조각(poi-all/sangga 1.4G,
sources/boundary/legal 54M)이 staged/ 밖에 있어 staged/ 를 경계로 삼으면 그대로 뚫린다.

실행:  python3 scripts/test_build_studio.py
       또는  python3 -m unittest scripts.test_build_studio -v
"""
import contextlib
import errno
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock   # `import unittest` 만으로는 unittest.mock 이 보장되지 않는다

# ── 모듈 로드 (하이픈 파일명 대응, BUILD_HOME=tmp 로 실홈 비오염) ──────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(_HERE, "build-studio.py")
_BUILD_HOME_TMP = tempfile.mkdtemp(prefix="build_studio_test_home_")
# setdefault 금지 — 운영자 셸의 `export BUILD_HOME=~/geocode-build` 를 반드시 덮어써야 한다.
# build-studio.py:9 docstring 이 그 export 를 안내하므로, setdefault 로 두면
# 평소 셸에서 테스트를 돌리는 순간 격리가 통째로 사라지고 실홈에 파괴가 일어난다.
os.environ["BUILD_HOME"] = _BUILD_HOME_TMP
# BUILD_HOME 만 덮어써서는 부족하다 — 573-575·20 은 각자 독립 환경변수를 먼저 보므로,
# 운영자가 `export SRC_JUSO=~/geocode-build/staged/navi` 를 해 둔 셸에서는 그 상수만
# 실홈에 남는다(571-572 주석이 그 덮어쓰기를 안내한다). 지워서 BUILD_HOME 파생으로
# 되돌린다. 경로가 아닌 환경변수(PORT·HOST·VWORLD_COOKIE 등)는 건드리지 않는다.
for _k in ("COMPOSE_FILE", "SRC_JUSO", "SRC_LOCALDATA", "SRC_GIS"):
    os.environ.pop(_k, None)

# 실 프리미티브 보존 — 파괴 감시 스파이가 활성인 동안에도 샌드박스 밖 픽스처를 정리해야 한다.
_REAL_RMTREE = shutil.rmtree


def _load_module(path=_MOD_PATH):
    spec = importlib.util.spec_from_file_location("build_studio", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_module()


# ════════════════════════════════════════════════════════════════
# 격리 안전망 — 실홈(~/geocode-build) 파괴 차단
#
# 안전 경계는 `staged/` 가 아니라 **BUILD_HOME** 이다. 파괴 대상의 최대 조각인
# poi-all/sangga(1.4G)·sources/boundary/legal(54M)·sources/boundary/admin(980K)
# 은 staged/ 밖에 있어, staged/ 를 경계로 삼으면 그대로 뚫린다.
# ════════════════════════════════════════════════════════════════
_REAL_BUILD_HOME = os.path.realpath(os.path.expanduser("~/geocode-build"))

# BUILD_HOME 파생 모듈전역 상수 전량. import 시점에 굳는 개별 상수라 BUILD_HOME 만
# 바꾸면 나머지가 실홈에 남는다. 주석의 숫자는 build-studio.py 의 정의 행.
_REPOINT_ATTRS = (
    "BUILD_HOME",       # 16
    "DB_PATH",          # 29
    "SOURCES_DIR",      # 31
    "STORE_DIR",        # 1034
    "MANIFESTS_DIR",    # 1510
    "DATA_VERSIONS",    # 28   — _migrate_versions_once 82 의 rename 대상. 최우선
    "ARTIFACTS",        # 27   — _snapshot_artifact 797 unlink
    "BUILD_STATE",      # 673  — save_build_state 687 이 Path.replace 로 덮어씀
    "DIST",             # 26
    "SRC_JUSO",         # 575  (str)
    "SRC_LOCALDATA",    # 576  (str)
    "SRC_GIS",          # 577  (str)
    "COMPOSE_FILE",     # 20   (str)
)


def _root_derived_source_keys():
    """dest 가 BUILD_HOME 이 아니라 ROOT 파생인 출처키 — 테스트에서 사용 금지.

    _collect_plan 1315 의 geofabrik 분기는 `ROOT / col["dest"]` 를 돌려주므로
    BUILD_HOME 재바인딩으로도, data-sources.json 사본으로도 막히지 않는다.
    """
    try:
        data = json.loads(pathlib.Path(M.SOURCES_FILE).read_text(encoding="utf-8"))
        keys = tuple(s["key"] for s in data.get("sources", [])
                     if (s.get("collect") or {}).get("method") == "geofabrik")
    except Exception:
        keys = ()
    return keys or ("osm",)


_ROOT_DERIVED_KEYS = _root_derived_source_keys()


def _under(path, root):
    """realpath 기준으로 path 가 root 이거나 그 하위인가."""
    p = os.path.realpath(str(path))
    r = os.path.realpath(str(root))
    return p == r or p.startswith(r + os.sep)


def _check_path(path, safe_roots, prim):
    """파괴 프리미티브 인자 1개의 안전성 판정(순수 함수).

    스파이가 **실제 호출 이전에** 이 함수를 부르므로, 위반이면 파괴는 일어나지 않고
    테스트가 실패한다. 순수 함수로 분리해 둔 덕에, 실홈 경로에 대한 거부 동작을
    실제 파괴 호출 없이 단위테스트할 수 있다.
    """
    p = os.path.realpath(str(path))
    if _under(p, _REAL_BUILD_HOME):
        raise AssertionError(f"실홈 파괴 시도({prim}): {p}")
    if not any(_under(p, r) for r in safe_roots):
        raise AssertionError(f"격리 위반({prim}): {p}")
    return p


def _repoint_build_home(M, root, tc=None):
    """BUILD_HOME 파생 상수를 _REPOINT_ATTRS 루프로 일괄 재바인딩하고 검증한다.

    개별 나열이 아니라 **반드시 튜플 루프**로 돈다 — 상수가 늘 때마다 다시 새는
    구조를 만들지 않기 위해서다. tc 를 주면 원값을 addCleanup 으로 복원한다
    (tearDown 아님 — 중간 예외에도 복원이 보장되어야 한다).
    """
    old, new = str(M.BUILD_HOME), str(root)
    for name in _REPOINT_ATTRS:
        cur = getattr(M, name)
        if tc is not None:
            tc.addCleanup(setattr, M, name, cur)
        s = str(cur)
        if s == old:
            nxt = new
        elif s.startswith(old + os.sep):
            nxt = new + s[len(old):]
        else:
            # BUILD_HOME 파생이 아닌 값(SRC_*·COMPOSE_FILE 의 외부 export 등)
            # → 실경로에 닿지 못하도록 샌드박스 안으로 끌어온다.
            nxt = os.path.join(new, "_repoint", name.lower())
        setattr(M, name, pathlib.Path(nxt) if isinstance(cur, pathlib.PurePath) else nxt)

    # 재바인딩 직후 검증 — 하나라도 실홈을 가리키면 여기서 즉시 실패시킨다.
    for name in _REPOINT_ATTRS:
        v = os.path.realpath(str(getattr(M, name)))
        assert not _under(v, _REAL_BUILD_HOME), f"{name} 이 실홈을 가리킨다: {v}"
    return pathlib.Path(new)


class IsolatedBuildHome(unittest.TestCase):
    """BUILD_HOME 전면 재바인딩 + 파괴 프리미티브 감시를 갖춘 테스트 기반 클래스.

    감시 대상 6종(호출형태 기준 8지점). 인자는 **전부** 검사한다 —
    os.replace(a, b) 는 b 도 파괴하기 때문이다.

      shutil.rmtree   533·1469·1573·1622·1866
      os.replace      (_swap_dir 신설 예정) / Path.replace 687
      os.rename       / Path.rename 82  (DATA_VERSIONS.rename)
      Path.unlink     499·797·1614·1621·1846   ← missing_ok 인자 통과 주의
      os.remove       1055·1134
      shutil.move     1057

    run_collect 의 tmpdir 은 `BUILD_HOME / "tmp"`(1443)라 재바인딩만으로
    샌드박스 안에 들어온다. 따라서 허용 루트는 self.home 하나로 충분하다.
    """

    def setUp(self):
        self.home = _repoint_build_home(M, tempfile.mkdtemp(prefix="rc_"), self)
        self.safe_roots = [str(self.home)]
        self.destructive = []          # [(prim, [인자…])] — 호출 이력
        self._install_spies()
        self._block_side_effects()
        self._reset_db_once_flag()
        self.addCleanup(_REAL_RMTREE, str(self.home), ignore_errors=True)

    # ── 안전망 구성 ────────────────────────────────────────────
    def allow_root(self, path):
        """샌드박스 밖이지만 이 테스트에 한해 파괴를 허용할 루트 등록."""
        self.safe_roots.append(str(path))

    def _spy(self, owner, name, nargs):
        """owner.name 을 감시 래퍼로 교체. 앞 nargs 개 인자를 경로로 보고 검사한다."""
        real = getattr(owner, name)
        label = f"{getattr(owner, '__name__', owner)}.{name}"

        def spy(*a, **k):
            targets = a[:nargs]
            for t in targets:
                _check_path(t, self.safe_roots, label)
            self.destructive.append((label, [str(t) for t in targets]))
            return real(*a, **k)

        p = mock.patch.object(owner, name, spy)
        p.start()
        self.addCleanup(p.stop)

    def _install_spies(self):
        # Path 계열은 클래스에 붙으므로 a[0] 이 곧 대상 경로(self)다.
        for owner, name, nargs in (
            (shutil, "rmtree", 1),
            (shutil, "move", 2),
            (os, "replace", 2),
            (os, "rename", 2),
            (os, "remove", 1),
            (pathlib.Path, "replace", 2),
            (pathlib.Path, "rename", 2),
            (pathlib.Path, "unlink", 1),
        ):
            self._spy(owner, name, nargs)

    def _block_side_effects(self):
        """실 네트워크·실 빌드 기동·수집 간격 대기를 차단한다."""
        for owner, name, kw in (
            (M.urllib.request, "urlopen", {"side_effect": AssertionError("실 네트워크 호출")}),
            (M.MGR, "enqueue", {"new": lambda *a, **k: None}),
            (M.time, "sleep", {"new": lambda *a, **k: None}),
        ):
            p = mock.patch.object(owner, name, **kw)
            p.start()
            self.addCleanup(p.stop)

    def _reset_db_once_flag(self):
        """`_DB_READY`(63)는 **모듈 전역**이라 샌드박스가 바뀌어도 True 로 남는다.

        그대로 두면 첫 케이스만 스키마가 깔리고 두 번째부터 새 DB_PATH 에
        `no such table: sources_state` 가 난다. 매 케이스 초기화해 새 샌드박스마다
        `SCHEMA_DDL` 이 다시 돌게 한다.
        """
        self.addCleanup(setattr, M, "_DB_READY", M._DB_READY)
        M._DB_READY = False

    # ── 공통 단언 ──────────────────────────────────────────────
    def assertNeverTargeted(self, *paths):
        """파괴 프리미티브 8지점 중 **어느 것도** 해당 경로를 인자로 받지 않았다.

        "결과적으로 내용이 남아 있다"보다 강한 단언이다 — 파괴를 시도했다가
        우연히 복구된 경우까지 잡는다. 실패 경로 전용(성공 교체는 `os.replace(dest, …)`
        를 정상적으로 부른다).
        """
        want = {os.path.realpath(str(p)) for p in paths}
        for prim, args in self.destructive:
            for a in args:
                self.assertNotIn(os.path.realpath(a), want, f"{prim} 가 dest 를 건드렸다")

    # ── 테스트 편의 ────────────────────────────────────────────
    def use_sources_copy(self, dest_overrides=None):
        """data-sources.json 을 tmp 로 복사하고 build_input.dest 를 재지정한다.

        load_sources()(46-47)가 매 호출 SOURCES_FILE 을 읽으므로 캐시 문제가 없다.
        `test:*` 같은 가상 키를 쓰면 _item_dest 가 None(1424-1425), _collect_plan 이
        예외(1300-1301)라 파괴 코드 본문에 도달조차 못 해 검증이 무의미해진다.
        그래서 **실재 키를 쓰되 dest 만 샌드박스로 돌린다.**
        """
        data = json.loads(pathlib.Path(M.SOURCES_FILE).read_text(encoding="utf-8"))
        for key in (dest_overrides or {}):
            assert key.partition(":")[0] not in _ROOT_DERIVED_KEYS, (
                f"{key} 는 dest 가 ROOT 파생이라 사본으로도 격리되지 않는다")
        for s in data.get("sources", []):
            if s["key"] in (dest_overrides or {}):
                s.setdefault("build_input", {})["dest"] = dest_overrides[s["key"]]
        copy = self.home / "data-sources.json"
        copy.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.addCleanup(setattr, M, "SOURCES_FILE", M.SOURCES_FILE)
        M.SOURCES_FILE = copy
        return copy

    @contextlib.contextmanager
    def collect_lock(self):
        """_COLLECT_LOCK 을 잡아야 할 때 **같은 스코프에서** 반납한다.

        addCleanup 으로 `locked() and release()` 를 거는 방식은 금지다 —
        MGR 데몬 워커가 잡고 있는 락까지 풀어 상호배제를 깬다.
        """
        M._COLLECT_LOCK.acquire()
        try:
            yield
        finally:
            M._COLLECT_LOCK.release()


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

    # ── A1: 0바이트는 '성공적으로 0바이트 수신'이 아니라 실패다 ──
    def test_H5_zero_byte_body_raises(self):
        """CL=0 이면 대조를 통과해버린다 — 이번 사고의 262건이 전부 이 경로였다.
        반환값(0)이 아니라 **예외**로 전파해야 재시도 백오프와 상위 실패처리가 걸린다."""
        self._patch(b"", {"Content-Length": "0"})
        dest = os.path.join(self.td, "h5.part")
        with self.assertRaises(Exception) as ctx:
            M._http_download("https://example.test/f", dest, retries=1)
        self.assertIn("0바이트", str(ctx.exception))

    def test_H5_zero_byte_without_content_length_raises(self):
        """CL 부재(chunked)라 대조 자체가 생략되는 경로도 막는다."""
        self._patch(b"", {})
        dest = os.path.join(self.td, "h5b.part")
        with self.assertRaises(Exception) as ctx:
            M._http_download("https://example.test/f", dest, retries=1)
        self.assertIn("0바이트", str(ctx.exception))


# ── I. 격리 안전망 자체 검증 ─────────────────────────────────────
# 안전망은 "있다"가 아니라 "실제로 막는다"를 증명해야 의미가 있다.
# 실홈 거부는 **순수 함수 _check_path 를 직접 호출**해 검증한다 — 실제 파괴
# 호출에 실홈 경로를 넘겨 확인하는 방식은, 판정이 틀렸을 때 그 시험 자체가
# 사고가 된다.
class TestIsolationHarness(IsolatedBuildHome):

    def test_I1_all_constants_off_real_home(self):
        """C-1: 13개 상수 전부가 실홈 밖을 가리킨다."""
        self.assertEqual(len(_REPOINT_ATTRS), len(set(_REPOINT_ATTRS)))
        for name in _REPOINT_ATTRS:
            v = os.path.realpath(str(getattr(M, name)))
            self.assertFalse(_under(v, _REAL_BUILD_HOME), f"{name} → {v}")
            self.assertTrue(_under(v, self.home), f"{name} 이 샌드박스 밖: {v}")
        # 대표 상수의 파생관계가 유지되는가
        self.assertEqual(str(M.DB_PATH), str(self.home / "build-studio.db"))
        self.assertEqual(str(M.SRC_JUSO), str(self.home / "staged/navi"))
        self.assertIsInstance(M.DB_PATH, pathlib.PurePath)   # 타입 보존
        self.assertIsInstance(M.SRC_JUSO, str)

    def test_I2_repoint_is_loop_not_enumeration(self):
        """C-1: 헬퍼가 튜플 루프다 — 튜플에 이름을 더하면 그 상수도 따라온다."""
        global _REPOINT_ATTRS
        M.FAKE_CONST = M.BUILD_HOME / "fake/leaf"
        self.addCleanup(delattr, M, "FAKE_CONST")
        orig = _REPOINT_ATTRS
        _REPOINT_ATTRS = orig + ("FAKE_CONST",)
        self.addCleanup(lambda: globals().__setitem__("_REPOINT_ATTRS", orig))
        root2 = self.home / "second"
        _repoint_build_home(M, root2, self)
        self.assertEqual(str(M.FAKE_CONST), str(root2 / "fake/leaf"))

    def test_I3_check_path_rejects_real_home(self):
        """C-10: 안전 경계는 BUILD_HOME 이다 — staged/ 밖 조각도 전부 거부."""
        for leaf in ("", "staged/localdata", "poi-all/sangga",
                     "sources/boundary/legal", "store", "geocode.sqlite"):
            p = os.path.join(_REAL_BUILD_HOME, leaf) if leaf else _REAL_BUILD_HOME
            with self.assertRaises(AssertionError) as ctx:
                _check_path(p, self.safe_roots, "rmtree")
            self.assertIn("실홈 파괴 시도", str(ctx.exception))
        # 실홈을 허용루트로 등록해도 거부가 유지된다(무조건 거부)
        with self.assertRaises(AssertionError):
            _check_path(_REAL_BUILD_HOME + "/staged",
                        [_REAL_BUILD_HOME], "rmtree")

    def test_I4_check_path_rejects_outside_and_accepts_inside(self):
        outside = tempfile.mkdtemp(prefix="outside_")
        self.addCleanup(_REAL_RMTREE, outside, ignore_errors=True)
        with self.assertRaises(AssertionError) as ctx:
            _check_path(outside, self.safe_roots, "rmtree")
        self.assertIn("격리 위반", str(ctx.exception))
        self.assertTrue(_check_path(self.home / "x/y", self.safe_roots, "rmtree"))
        self.allow_root(outside)
        self.assertTrue(_check_path(outside, self.safe_roots, "rmtree"))

    def test_I5_all_six_primitives_are_spied(self):
        """C-2: 6종 8지점 전부가 감시 래퍼로 교체돼 있다."""
        for owner, name in ((shutil, "rmtree"), (shutil, "move"),
                            (os, "replace"), (os, "rename"), (os, "remove"),
                            (pathlib.Path, "replace"), (pathlib.Path, "rename"),
                            (pathlib.Path, "unlink")):
            fn = getattr(owner, name)
            self.assertEqual(getattr(fn, "__name__", ""), "spy",
                             f"{owner}.{name} 미감시")

    def test_I6_sandbox_calls_pass_and_are_recorded(self):
        d = self.home / "d"; d.mkdir(parents=True)
        (d / "a.txt").write_text("a", encoding="utf-8")
        (d / "b.txt").write_text("b", encoding="utf-8")
        (d / "a.txt").unlink()                                   # Path.unlink
        os.remove(str(d / "b.txt"))                              # os.remove
        (d / "c.txt").write_text("c", encoding="utf-8")
        (d / "c.txt").rename(d / "c2.txt")                       # Path.rename
        os.rename(str(d / "c2.txt"), str(d / "c3.txt"))          # os.rename
        os.replace(str(d / "c3.txt"), str(d / "c4.txt"))         # os.replace
        (d / "c4.txt").replace(d / "c5.txt")                     # Path.replace
        shutil.move(str(d / "c5.txt"), str(d / "c6.txt"))        # shutil.move
        shutil.rmtree(str(d))                                    # shutil.rmtree
        prims = {p for p, _ in self.destructive}
        self.assertEqual(prims, {
            "shutil.rmtree", "shutil.move", "os.replace", "os.rename",
            "os.remove", "Path.replace", "Path.rename", "Path.unlink",
        }, f"감시 이력 불일치: {sorted(prims)}")
        # 이력에는 인자 경로가 남아, 어디를 지웠는지 사후 추궁이 가능하다
        self.assertTrue(all(_under(t, self.home)
                            for _, ts in self.destructive for t in ts))
        self.assertFalse(d.exists())

    def test_I7_second_argument_is_also_checked(self):
        """C-2: os.replace(a, b)/shutil.move(a, b) 는 b 도 파괴한다 → dst 도 검사."""
        outside = tempfile.mkdtemp(prefix="outside_dst_")
        self.addCleanup(_REAL_RMTREE, outside, ignore_errors=True)
        src = self.home / "src.txt"; src.write_text("keep", encoding="utf-8")
        victim = os.path.join(outside, "victim.txt")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("원본")
        for call in (lambda: os.replace(str(src), victim),
                     lambda: os.rename(str(src), victim),
                     lambda: shutil.move(str(src), victim),
                     lambda: src.replace(pathlib.Path(victim)),
                     lambda: src.rename(pathlib.Path(victim))):
            with self.assertRaises(AssertionError):
                call()
        # 차단됐으므로 양쪽 다 원상태다
        self.assertEqual(src.read_text(encoding="utf-8"), "keep")
        with open(victim, encoding="utf-8") as f:
            self.assertEqual(f.read(), "원본")

    def test_I8_unlink_missing_ok_passthrough(self):
        """C-2: Path.unlink 의 missing_ok 인자가 래퍼를 통과한다."""
        p = self.home / "nope.txt"
        p.unlink(missing_ok=True)                 # 예외 없어야 함
        p.unlink(True)                            # 위치인자 형태
        with self.assertRaises(FileNotFoundError):
            p.unlink()

    def test_I9_use_sources_copy_redirects_dest(self):
        """C-3: 가상 키가 아니라 실재 키 + dest 재지정."""
        keys = [s["key"] for s in
                json.loads(pathlib.Path(M.SOURCES_FILE).read_text(encoding="utf-8"))["sources"]]
        target = next(k for k in keys if k not in _ROOT_DERIVED_KEYS)
        copy = self.use_sources_copy({target: "t/redirected"})
        self.assertEqual(str(M.SOURCES_FILE), str(copy))
        self.assertTrue(_under(copy, self.home))
        loaded = {s["key"]: s for s in M.load_sources()}          # 46-47 재읽기
        self.assertEqual(loaded[target]["build_input"]["dest"], "t/redirected")
        dest = M._item_dest(target)
        self.assertIsNotNone(dest, "실재 키여야 _item_dest 가 None 이 아니다")
        self.assertTrue(_under(dest, self.home), f"dest 가 샌드박스 밖: {dest}")

    def test_I10_root_derived_keys_are_refused(self):
        """C-3: geofabrik(osm) 키는 사본으로도 격리되지 않으므로 사용 금지."""
        self.assertIn("osm", _ROOT_DERIVED_KEYS)
        for key in ("osm", "osm:sub"):
            with self.assertRaises(AssertionError) as ctx:
                self.use_sources_copy({key: "t/x"})
            self.assertIn("ROOT 파생", str(ctx.exception))

    def test_I11_collect_lock_released_in_same_scope(self):
        """C-5: addCleanup 강제해제 없이, try/finally 로 같은 스코프에서 반납."""
        self.assertFalse(M._COLLECT_LOCK.locked())
        with self.collect_lock():
            self.assertTrue(M._COLLECT_LOCK.locked())
        self.assertFalse(M._COLLECT_LOCK.locked())
        with self.assertRaises(RuntimeError):
            with self.collect_lock():
                raise RuntimeError("본문 예외")
        self.assertFalse(M._COLLECT_LOCK.locked(), "예외 경로에서도 반납된다")
        # 소유하지 않은 락을 푸는 cleanup 이 등록돼 있지 않다
        srcs = "".join(getattr(f, "__qualname__", str(f))
                       for f, _, _ in getattr(self, "_cleanups", []))
        self.assertNotIn("locked", srcs)

    def test_I12_side_effects_blocked(self):
        with self.assertRaises(AssertionError):
            M.urllib.request.urlopen("https://example.test/")
        self.assertIsNone(M.MGR.enqueue("noop"))
        M.time.sleep(9999)          # 즉시 반환해야 한다


class TestHarnessDoesNotLeak(unittest.TestCase):
    """스파이가 프로세스 전역이므로, 미상속 테스트에 새지 않음을 확인한다."""

    def test_I13_no_spy_outside_isolated_cases(self):
        self.assertIs(shutil.rmtree, _REAL_RMTREE)
        for owner, name in ((shutil, "move"), (os, "replace"), (os, "rename"),
                            (os, "remove"), (pathlib.Path, "replace"),
                            (pathlib.Path, "rename"), (pathlib.Path, "unlink")):
            self.assertNotEqual(getattr(getattr(owner, name), "__name__", ""), "spy")

    def test_I14_module_constants_never_point_at_real_home(self):
        """무조건 대입(§4.1) 결과 — 운영자 셸의 export 와 무관하게 실홈 밖."""
        self.assertEqual(os.environ["BUILD_HOME"], _BUILD_HOME_TMP)
        for name in _REPOINT_ATTRS:
            v = os.path.realpath(str(getattr(M, name)))
            self.assertFalse(_under(v, _REAL_BUILD_HOME), f"{name} → {v}")

    def test_I15_path_env_overrides_neutralized(self):
        """BUILD_HOME 만 덮어써서는 573-575·20 이 실홈에 남는다."""
        for k in ("COMPOSE_FILE", "SRC_JUSO", "SRC_LOCALDATA", "SRC_GIS"):
            self.assertNotIn(k, os.environ, f"{k} 가 남으면 그 상수만 실홈을 가리킨다")


# ── T027 2단계-A: _swap_dir 원자적 교체 프리미티브 ────────────────────────
def _fill_files(spec):
    """`{상대경로: 바이트}` 를 staging 에 쓰는 fill 콜백을 만든다."""
    def fill(staging):
        for rel, payload in spec.items():
            p = pathlib.Path(staging) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(payload)
    return fill


def _tree(root):
    """root 하위 파일의 `{상대경로: 바이트}` 스냅샷(디렉터리 내용 동일성 비교용)."""
    root = pathlib.Path(root)
    out = {}
    for dp, _dn, fns in os.walk(root):
        for nm in fns:
            f = pathlib.Path(dp) / nm
            out[f.relative_to(root).as_posix()] = f.read_bytes()
    return out


class TestSwapDir(IsolatedBuildHome):
    """`_swap_dir` 계약 — plan.md §4.5 의 S1·S2·S4 계열.

    S3(`expect_min` 계약)은 design-review-2 §6 삭제 권고에 따라 제외한다
    (호출부 전량이 None, parcel 258→262 변동으로 상수화 불가, 재발 방지 기여 0).
    """

    def setUp(self):
        super().setUp()
        self.dest = self.home / "staged" / "poi"
        self.orig = {"keep.csv": b"name,lon,lat\nold,127.0,37.0\n", "sub/deep.txt": b"x" * 32}
        _fill_files(self.orig)(self.dest)     # dest 를 '지켜야 할 원본' 상태로 만든다

    # ── 공통 단언 ──────────────────────────────────────────────
    def _res(self, suffix):
        return self.dest.parent / (self.dest.name + suffix)

    def assertDestIntact(self):
        self.assertEqual(_tree(self.dest), self.orig, "dest 원본이 훼손됐다")

    def assertNoResidue(self):
        self.assertFalse(self._res(".incoming").exists(), ".incoming 잔재")
        self.assertFalse(self._res(".old").exists(), ".old 잔재")

    # ── S1: 실패 시 dest 원본 보존 (이 태스크의 핵심 회귀 항목) ──────
    def test_S1_fill_exception_keeps_dest(self):
        boom = RuntimeError("추출 실패")

        def fill(st):
            (pathlib.Path(st) / "half.csv").write_bytes(b"partial")
            raise boom

        with self.assertRaises(RuntimeError) as cm:
            M._swap_dir(self.dest, fill)
        self.assertIs(cm.exception, boom, "원 예외를 그대로 재전파해야 한다")
        self.assertDestIntact()
        self.assertNoResidue()

    def test_S1_empty_staging_rejected_and_dest_kept(self):
        with self.assertRaises(RuntimeError):
            M._swap_dir(self.dest, lambda st: None)
        self.assertDestIntact()
        self.assertNoResidue()

    def test_S1_zero_byte_payload_rejected(self):
        """사고 재현형 — 파일 개수는 있는데 전량 0바이트면 교체를 거부한다."""
        with self.assertRaises(RuntimeError):
            M._swap_dir(self.dest, _fill_files({"a.csv": b"", "b/c.csv": b""}))
        self.assertDestIntact()
        self.assertNoResidue()

    def test_S1_failure_when_dest_absent_leaves_nothing(self):
        fresh = self.home / "staged" / "new"
        with self.assertRaises(RuntimeError):
            M._swap_dir(fresh, lambda st: None)
        self.assertFalse(fresh.exists())
        self.assertFalse((fresh.parent / (fresh.name + ".incoming")).exists())

    def test_S1_no_destructive_primitive_ever_targets_dest(self):
        """실패 경로에서 dest 를 겨냥한 파괴 호출이 **한 건도** 없어야 한다(스파이 이력 검사)."""
        with self.assertRaises(RuntimeError):
            M._swap_dir(self.dest, _fill_files({"a.csv": b""}))
        want = os.path.realpath(str(self.dest))
        for prim, args in self.destructive:
            for a in args:
                self.assertNotEqual(os.path.realpath(a), want, f"{prim} 가 dest 를 건드렸다")

    # ── S2: 정상 교체 ──────────────────────────────────────────
    def test_S2_replaces_and_returns_measured_stats(self):
        new = {"fresh.csv": b"name,lon,lat\nnew,128.0,36.0\n", "d/e.bin": b"\x00\x01\x02"}
        n_files, n_bytes = M._swap_dir(self.dest, _fill_files(new))
        self.assertEqual(_tree(self.dest), new)
        self.assertEqual(n_files, len(new))
        self.assertEqual(n_bytes, sum(len(v) for v in new.values()))
        self.assertNoResidue()

    def test_S2_pid_marker_not_published_into_dest(self):
        M._swap_dir(self.dest, _fill_files({"a.csv": b"x"}))
        self.assertEqual(sorted(_tree(self.dest)), ["a.csv"], ".pid 가 dest 로 넘어갔다")

    def test_S2_creates_dest_when_absent(self):
        fresh = self.home / "staged" / "new"
        M._swap_dir(fresh, _fill_files({"a.csv": b"xyz"}))
        self.assertEqual(_tree(fresh), {"a.csv": b"xyz"})

    def test_S2_staging_lives_under_dest_parent(self):
        """C-7 — tmp 는 `/tmp` 도 `BUILD_HOME/tmp` 도 아닌 `dest.parent` 안이다."""
        seen = []

        def fill(st):
            seen.append(pathlib.Path(st))
            (pathlib.Path(st) / "a.csv").write_bytes(b"x")

        M._swap_dir(self.dest, fill)
        self.assertEqual(seen[0].parent, self.dest.parent)
        self.assertEqual(seen[0].name, self.dest.name + ".incoming")

    def test_S2_no_self_deadlock_when_caller_holds_lock(self):
        """C-6 — 호출자가 `_COLLECT_LOCK` 을 쥔 채 불러도 교착이 없어야 한다.

        본 스레드에서 그대로 부르면 결함 시 **영구 정지**라 테스트가 끝나지 않는다.
        데몬 스레드 + join(timeout) 으로 교착을 '실패'로 바꿔 관측한다.
        """
        done = []

        def go():
            try:
                M._swap_dir(self.dest, _fill_files({"a.csv": b"x"}))
                done.append(True)
            except BaseException as e:     # noqa: BLE001 — 스레드 밖으로 전달
                done.append(e)

        with self.collect_lock():
            t = threading.Thread(target=go, daemon=True)
            t.start()
            t.join(timeout=15)
        self.assertFalse(t.is_alive(), "_COLLECT_LOCK 재획득 → 자기교착")
        self.assertEqual(done, [True], f"교체 실패: {done}")

    def test_S2_keep_old_preserves_backup(self):
        M._swap_dir(self.dest, _fill_files({"a.csv": b"x"}), keep_old=True)
        self.assertEqual(_tree(self.dest), {"a.csv": b"x"})
        self.assertEqual(_tree(self._res(".old")), self.orig)

    # ── S4: 잔재 뒤처리 · 롤백 우선순위 ─────────────────────────
    def test_S4_rollback_when_dest_missing(self):
        """①직후 사망 재현 — dest 부재 ∧ `.old` 존재 → 원본 회수."""
        old = self._res(".old")
        os.replace(self.dest, old)
        M._swap_recover(self.dest)
        self.assertDestIntact()
        self.assertFalse(old.exists())

    def test_S4_stale_old_dropped_when_dest_present(self):
        """②완료 후 `.old` 삭제 전 사망 재현 — dest 존재 → `.old` 만 폐기."""
        _fill_files({"junk.csv": b"junk"})(self._res(".old"))
        M._swap_recover(self.dest)
        self.assertDestIntact()
        self.assertFalse(self._res(".old").exists())

    def test_S4_rollback_precedes_incoming_sweep(self):
        """C-13 — `.old` 롤백이 `.incoming` 스윕보다 **먼저**임을 호출 순서로 확인한다."""
        old, inc = self._res(".old"), self._res(".incoming")
        os.replace(self.dest, old)
        _fill_files({"partial.csv": b"partial"})(inc)      # .pid 없음 → 고아
        M._swap_recover(self.dest)
        self.assertDestIntact()
        self.assertFalse(inc.exists())
        self.assertFalse(old.exists())

        i_roll = next(i for i, (p, a) in enumerate(self.destructive)
                      if p.endswith("replace") and os.path.realpath(a[1]) == os.path.realpath(str(self.dest)))
        i_sweep = next(i for i, (p, a) in enumerate(self.destructive)
                       if p.endswith("rmtree") and os.path.realpath(a[0]) == os.path.realpath(str(inc)))
        self.assertLess(i_roll, i_sweep, "C-13 위반 — 스윕이 롤백보다 먼저 돌았다")

    def test_S4_orphan_incoming_swept_when_pid_dead(self):
        inc = self._res(".incoming")
        _fill_files({"p.csv": b"p"})(inc)
        (inc / M._SWAP_PID_NAME).write_text(
            json.dumps({"pid": 0x7FFFFFF0, "start": "Mon Jan  1 00:00:00 2001"}), encoding="utf-8")
        M._swap_recover(self.dest)
        self.assertFalse(inc.exists())

    def test_S4_live_pid_with_different_start_is_orphan(self):
        """C-12 — PID 는 살아있어도 시작시각이 다르면 PID 재사용이므로 고아다."""
        if M._proc_start_key(os.getpid()) is None:
            self.skipTest("이 플랫폼에서 프로세스 시작시각을 얻을 수 없다")
        inc = self._res(".incoming")
        _fill_files({"p.csv": b"p"})(inc)
        (inc / M._SWAP_PID_NAME).write_text(
            json.dumps({"pid": os.getpid(), "start": "Mon Jan  1 00:00:00 2001"}), encoding="utf-8")
        M._swap_recover(self.dest)
        self.assertFalse(inc.exists(), "PID 만 대조하면 고아가 영원히 남는다")

    def test_S4_live_owner_incoming_untouched(self):
        inc = self._res(".incoming")
        inc.mkdir(parents=True)
        M._swap_pid_write(inc)                 # 우리 자신 = 살아있는 소유자
        (inc / "p.csv").write_bytes(b"p")
        M._swap_recover(self.dest)
        self.assertTrue(inc.exists(), "살아있는 소유자의 .incoming 을 지웠다")

    def test_S4_swap_dir_clears_orphan_incoming_and_proceeds(self):
        _fill_files({"stale.csv": b"stale"})(self._res(".incoming"))   # .pid 없음 → 고아
        M._swap_dir(self.dest, _fill_files({"a.csv": b"x"}))
        self.assertEqual(_tree(self.dest), {"a.csv": b"x"})
        self.assertNoResidue()

    def test_S4_swap_dir_refuses_when_live_owner_holds_incoming(self):
        inc = self._res(".incoming")
        inc.mkdir(parents=True)
        M._swap_pid_write(inc)
        with self.assertRaises(RuntimeError):
            M._swap_dir(self.dest, _fill_files({"a.csv": b"x"}))
        self.assertDestIntact()
        self.assertTrue(inc.exists(), "남의 진행 중 스테이징을 지웠다")

    def test_S4_sweep_recovers_every_dest_under_root(self):
        """시동 스윕 — 여러 dest 의 잔재를 한 번에 복구하고, 전역에서도 롤백을 먼저 끝낸다."""
        other = self.home / "sources" / "boundary" / "legal"      # 깊이 3(가장 깊은 실제 dest)
        _fill_files({"z.csv": b"z"})(other)
        os.replace(other, other.parent / (other.name + ".old"))   # ①직후 사망 ×2
        os.replace(self.dest, self._res(".old"))
        _fill_files({"partial.csv": b"p"})(self._res(".incoming"))   # .pid 없음 → 고아

        acts = M._sweep_swap_residue(self.home)
        self.assertDestIntact()
        self.assertEqual(_tree(other), {"z.csv": b"z"})
        self.assertNoResidue()
        kinds = [k for k, _ in acts]
        self.assertEqual(kinds.count("rollback"), 2, f"롤백 누락: {acts}")
        self.assertLess(max(i for i, k in enumerate(kinds) if k == "rollback"),
                        min(i for i, k in enumerate(kinds) if k == "drop-incoming"),
                        "C-13 위반 — 전역 스윕은 롤백을 전부 끝낸 뒤 스윕해야 한다")

    def test_S4_sweep_keeps_live_owner_and_ignores_normal_dirs(self):
        inc = self._res(".incoming")
        inc.mkdir(parents=True)
        M._swap_pid_write(inc)
        acts = M._sweep_swap_residue(self.home)
        self.assertTrue(inc.exists())
        self.assertEqual([k for k, _ in acts], ["keep-incoming"])
        self.assertDestIntact()

    # ── S5: 크로스 디바이스(EXDEV) ─────────────────────────────
    def test_S5_same_device_guard_rejects_mixed_devices(self):
        """전제(같은 파일시스템)를 코드가 검사한다 — 폴백이 아니라 명확한 예외."""
        inc = self._res(".incoming")
        inc.mkdir(parents=True)
        real = M.os.stat

        def fake(path, *a, **k):
            st = real(path, *a, **k)
            if str(path).endswith(".incoming"):
                t = list(tuple(st))
                t[2] = st.st_dev + 1           # st_dev 만 어긋나게
                return os.stat_result(tuple(t))
            return st

        M.os.stat = fake
        try:
            with self.assertRaises(RuntimeError) as cm:
                M._assert_same_device(inc, self.dest.parent)
        finally:
            M.os.stat = real
        self.assertIn("EXDEV", str(cm.exception))

    def test_S5_exdev_on_publish_rolls_back_dest(self):
        """② replace 가 EXDEV 로 죽어도 `.old` 롤백으로 dest 는 살아난다."""
        real = M.os.replace
        want = os.path.realpath(str(self.dest))

        def flaky(src, dst, *a, **k):
            if str(src).endswith(".incoming") and os.path.realpath(str(dst)) == want:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(src, dst, *a, **k)

        M.os.replace = flaky
        try:
            with self.assertRaises(RuntimeError) as cm:
                M._swap_dir(self.dest, _fill_files({"a.csv": b"x"}))
        finally:
            M.os.replace = real
        self.assertIn("EXDEV", str(cm.exception))
        self.assertDestIntact()
        self.assertNoResidue()


# ════════════════════════════════════════════════════════════════
# load_manifest — 프로필 복원(약 5.0GB 파괴 경로)
# ════════════════════════════════════════════════════════════════
class TestLoadManifest(IsolatedBuildHome):
    """`load_manifest` 는 store 원본이 없으면 dest 에 **손도 대지 않는다**.

    사고 재현형: 예전 코드는 `rmtree(dest)` 를 **먼저** 때린 뒤 `store_path(sha)`
    부재로 복원할 게 없어 빈 디렉터리만 남겼다. 게다가 `staged_sig` 에 그 sha 를
    기록해 다음 수집이 '변경 없음' 으로 건너뛰게 만들었다 — 소실이 무증상으로 굳는다.
    """

    DIRKEY = "sangga"     # dest 확장자 없음 → 디렉터리형 분기
    FILEKEY = "police"    # dest 를 .pbf 로 돌려 파일형 분기 검사(실키를 써야 _item_dest 가 산다)

    def setUp(self):
        super().setUp()
        self.use_sources_copy({self.DIRKEY: "staged/sangga",
                               self.FILEKEY: "staged/osm/korea.osm.pbf"})
        self.dest = self.home / "staged" / "sangga"
        self.orig = {"keep.csv": b"name,lon,lat\nold,127.0,37.0\n"}
        _fill_files(self.orig)(self.dest)
        self.fdest = self.home / "staged" / "osm" / "korea.osm.pbf"
        self.fdest.parent.mkdir(parents=True, exist_ok=True)
        self.forig = b"OLD-PBF-PAYLOAD"
        self.fdest.write_bytes(self.forig)
        M.MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)

    # ── 픽스처 ────────────────────────────────────────────────
    def write_manifest(self, sources, mid="p1", name="프로필A"):
        (M.MANIFESTS_DIR / f"{mid}.json").write_text(
            json.dumps({"name": name, "sources": sources}, ensure_ascii=False), encoding="utf-8")
        return mid

    def put_store(self, payload_path):
        M.STORE_DIR.mkdir(parents=True, exist_ok=True)
        sha, _ = M.store_put(pathlib.Path(payload_path))
        return sha

    def store_zip(self, names=("a.csv",)):
        tmp = self.home / "tmp" / "in.zip"; tmp.parent.mkdir(parents=True, exist_ok=True)
        return self.put_store(_make_valid_zip(tmp, names=names))

    # ── 공통 단언 ──────────────────────────────────────────────
    def assertDestIntact(self):
        self.assertEqual(_tree(self.dest), self.orig, "dest 원본이 훼손됐다")
        self.assertEqual(self.fdest.read_bytes(), self.forig, "파일형 dest 가 훼손됐다")

    def assertNoSignature(self, key):
        rec = M.load_versions().get(key, {})
        self.assertIsNone(rec.get("staged_sig"), "복원하지도 않고 거짓 서명을 남겼다")

    # ── M1: 핵심 회귀 — 원본 부재 시 dest 무손상 + skipped + 서명 미기록 ──
    def test_M1_missing_store_keeps_dest_and_skips(self):
        mid = self.write_manifest({self.DIRKEY: {"sha": "de" * 32, "current": "2026-08-01",
                                                 "file": "sangga.zip"}})
        r = M.load_manifest(mid)
        self.assertEqual(r["restored"], 0, "복원한 게 없는데 restored 가 올랐다")
        self.assertEqual([s["key"] for s in r["skipped"]], [self.DIRKEY])
        self.assertDestIntact()
        self.assertNoSignature(self.DIRKEY)

    def test_M1_no_destructive_primitive_ever_targets_dest(self):
        mid = self.write_manifest({self.DIRKEY: {"sha": "de" * 32},
                                   self.FILEKEY: {"sha": "ad" * 32}})
        M.load_manifest(mid)
        self.assertNeverTargeted(self.dest, self.fdest)

    def test_M1_file_branch_missing_store_is_not_counted(self):
        """파일형도 선검증한다 — 예전엔 `sp.exists()` 가 False 여도 restored 가 올랐다."""
        mid = self.write_manifest({self.FILEKEY: {"sha": "ad" * 32}})
        r = M.load_manifest(mid)
        self.assertEqual(r["restored"], 0)
        self.assertEqual(len(r["skipped"]), 1)
        self.assertDestIntact()
        self.assertNoSignature(self.FILEKEY)

    def test_M1_partial_multi_sha_skips_whole_key(self):
        """콤마 다중 sha 중 하나만 없어도 그 키는 통째로 건너뛴다(반쪽 복원 금지)."""
        good = self.store_zip()
        mid = self.write_manifest({self.DIRKEY: {"sha": f"{good},{'de' * 32}"}})
        r = M.load_manifest(mid)
        self.assertEqual(r["restored"], 0)
        self.assertEqual(r["skipped"][0]["missing"], 1)
        self.assertEqual(r["skipped"][0]["total"], 2)
        self.assertDestIntact()
        self.assertNoSignature(self.DIRKEY)

    def test_M1_no_sha_is_skipped_not_recorded(self):
        mid = self.write_manifest({self.DIRKEY: {"sha": None, "current": "2026-08-01"}})
        r = M.load_manifest(mid)
        self.assertEqual(r["restored"], 0)
        self.assertEqual([s["key"] for s in r["skipped"]], [self.DIRKEY])
        self.assertDestIntact()
        self.assertNoSignature(self.DIRKEY)

    def test_M1_extract_failure_keeps_dest_and_skips(self):
        """추출이 깨져도(`_swap_dir` 이 막는다) dest 는 살고 서명은 남지 않는다."""
        tmp = self.home / "tmp" / "bad.zip"; tmp.parent.mkdir(parents=True, exist_ok=True)
        sha = self.put_store(_make_truncated_zip(tmp))
        mid = self.write_manifest({self.DIRKEY: {"sha": sha}})
        r = M.load_manifest(mid)
        self.assertEqual(r["restored"], 0)
        self.assertEqual(len(r["skipped"]), 1)
        self.assertDestIntact()
        self.assertNoSignature(self.DIRKEY)

    # ── M2: 정상 복원 ──────────────────────────────────────────
    def test_M2_restores_dir_and_records_signature(self):
        sha = self.store_zip(names=("fresh.csv",))
        mid = self.write_manifest({self.DIRKEY: {"sha": sha, "current": "2026-08-01",
                                                 "file": "sangga.zip"}})
        r = M.load_manifest(mid)
        self.assertEqual(r["restored"], 1)
        self.assertEqual(r["skipped"], [])
        self.assertEqual(sorted(_tree(self.dest)), ["fresh.csv"])
        rec = M.load_versions()[self.DIRKEY]
        self.assertEqual(rec["staged_sig"], sha)
        self.assertEqual(rec["current"], "2026-08-01")
        self.assertEqual(rec["file"], "sangga.zip")

    def test_M2_restores_file_type_atomically(self):
        tmp = self.home / "tmp" / "new.pbf"; tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(b"NEW-PBF-PAYLOAD")
        sha = self.put_store(tmp)
        mid = self.write_manifest({self.FILEKEY: {"sha": sha}})
        r = M.load_manifest(mid)
        self.assertEqual(r["restored"], 1)
        self.assertEqual(self.fdest.read_bytes(), b"NEW-PBF-PAYLOAD")
        self.assertFalse((self.fdest.parent / (self.fdest.name + ".incoming")).exists())

    def test_M2_mixed_manifest_counts_each_side(self):
        sha = self.store_zip()
        mid = self.write_manifest({self.DIRKEY: {"sha": sha},
                                   self.FILEKEY: {"sha": "ad" * 32}})
        r = M.load_manifest(mid)
        self.assertEqual(r["restored"], 1)
        self.assertEqual([s["key"] for s in r["skipped"]], [self.FILEKEY])
        self.assertEqual(self.fdest.read_bytes(), self.forig)

    # ── M3: 수집과의 상호배제 ──────────────────────────────────
    def test_M3_refuses_while_collect_holds_lock(self):
        sha = self.store_zip()
        mid = self.write_manifest({self.DIRKEY: {"sha": sha}})
        with self.collect_lock():
            with self.assertRaises(RuntimeError):
                M.load_manifest(mid)
        self.assertDestIntact()
        self.assertNoSignature(self.DIRKEY)

    def test_M3_lock_released_after_normal_return(self):
        mid = self.write_manifest({self.DIRKEY: {"sha": "de" * 32}})
        M.load_manifest(mid)
        self.assertFalse(M._COLLECT_LOCK.locked(), "락을 물고 나왔다")


# ════════════════════════════════════════════════════════════════
# _swap_file — 파일형 원자적 교체
# ════════════════════════════════════════════════════════════════
class TestSwapFile(IsolatedBuildHome):
    def setUp(self):
        super().setUp()
        self.dest = self.home / "data" / "osm" / "korea.osm.pbf"
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        self.dest.write_bytes(b"ORIGINAL")

    def test_F1_replaces_content(self):
        src = self.home / "src.bin"; src.write_bytes(b"NEW-CONTENT")
        n_files, n_bytes = M._swap_file(self.dest, src)
        self.assertEqual(self.dest.read_bytes(), b"NEW-CONTENT")
        self.assertEqual((n_files, n_bytes), (1, len(b"NEW-CONTENT")))

    def test_F1_zero_byte_source_rejected_and_dest_kept(self):
        src = self.home / "empty.bin"; src.write_bytes(b"")
        with self.assertRaises(RuntimeError):
            M._swap_file(self.dest, src)
        self.assertEqual(self.dest.read_bytes(), b"ORIGINAL")
        self.assertFalse((self.dest.parent / (self.dest.name + ".incoming")).exists())

    def test_F1_copy_failure_keeps_dest(self):
        missing = self.home / "nope.bin"
        with self.assertRaises(OSError):
            M._swap_file(self.dest, missing)
        self.assertEqual(self.dest.read_bytes(), b"ORIGINAL")
        self.assertFalse((self.dest.parent / (self.dest.name + ".incoming")).exists())

    def test_F1_creates_dest_when_absent(self):
        fresh = self.home / "data" / "osm" / "new.pbf"
        src = self.home / "src.bin"; src.write_bytes(b"X")
        M._swap_file(fresh, src)
        self.assertEqual(fresh.read_bytes(), b"X")


# ════════════════════════════════════════════════════════════════
# /api/collect/upload — 사용자 업로드 경로의 무결성(C-9) 과 락 범위(C-11)
# ════════════════════════════════════════════════════════════════
class TestCollectUpload(IsolatedBuildHome):
    DIRKEY = "sangga"
    FILEKEY = "police"

    def setUp(self):
        super().setUp()
        self.use_sources_copy({self.DIRKEY: "staged/sangga",
                               self.FILEKEY: "staged/osm/korea.osm.pbf"})
        self.dest = self.home / "staged" / "sangga"
        self.orig = {"keep.csv": b"name,lon,lat\nold,127.0,37.0\n"}
        _fill_files(self.orig)(self.dest)
        self.fdest = self.home / "staged" / "osm" / "korea.osm.pbf"
        self.fdest.parent.mkdir(parents=True, exist_ok=True)
        self.forig = b"OLD-PBF-PAYLOAD"
        self.fdest.write_bytes(self.forig)
        self.tmpdir = self.home / "tmp"; self.tmpdir.mkdir(parents=True, exist_ok=True)

    # ── 픽스처 ────────────────────────────────────────────────
    def stage_tmp(self, name="up.zip", *, raw=None, truncated=False):
        """수신이 끝난 상태의 임시파일 — 핸들러의 스트리밍 루프가 남기는 것과 같은 자리."""
        p = self.tmpdir / name
        if raw is not None: p.write_bytes(raw)
        elif truncated: _make_truncated_zip(p)
        else: _make_valid_zip(p, names=("new.csv",))
        return p

    def apply(self, tmp, *, key=None, name="sangga_202601.zip", dest=None,
              written=None, nbytes=None):
        full = tmp.stat().st_size
        return M._apply_upload(key or self.DIRKEY, name,
                               self.dest if dest is None else dest, tmp,
                               full if written is None else written,
                               full if nbytes is None else nbytes)

    def history(self, key):
        c = M._db()
        try:
            return c.execute("SELECT file, size FROM upload_history WHERE key=?", (key,)).fetchall()
        finally:
            c.close()

    # ── 공통 단언 ──────────────────────────────────────────────
    def assertDestIntact(self):
        self.assertEqual(_tree(self.dest), self.orig, "dest 원본이 훼손됐다")
        self.assertEqual(self.fdest.read_bytes(), self.forig, "파일형 dest 가 훼손됐다")

    def assertNoSignature(self, key=None):
        rec = M.load_versions().get(key or self.DIRKEY, {})
        self.assertIsNone(rec.get("staged_sig"), "적재하지도 않고 서명을 남겼다")

    # ── U1: 무결성 게이트 — 이게 없어서 같은 사고가 업로드로 재현된다 ──
    def test_U1_partial_receive_rejected_and_dest_kept(self):
        """`written < nbytes` — 예전엔 그대로 성공 응답이 나가고 dest 가 rmtree 됐다."""
        tmp = self.stage_tmp()
        with self.assertRaises(M._UploadRejected):
            self.apply(tmp, written=tmp.stat().st_size - 3)
        self.assertDestIntact()
        self.assertNoSignature()
        self.assertEqual(self.history(self.DIRKEY), [], "실패했는데 업로드 이력이 남았다")

    def test_U1_zero_length_rejected(self):
        tmp = self.stage_tmp(raw=b"")
        with self.assertRaises(M._UploadRejected):
            self.apply(tmp, written=0, nbytes=0)
        self.assertDestIntact()
        self.assertNoSignature()

    def test_U1_corrupt_payload_rejected_by_gate(self):
        """수신 바이트 수가 맞아도 내용이 깨졌으면 store 에 넣지 않고 거절한다."""
        tmp = self.stage_tmp(truncated=True)
        with self.assertRaises(M._UploadRejected):
            self.apply(tmp)
        self.assertDestIntact()
        self.assertNoSignature()

    def test_U1_no_destructive_primitive_targets_dest_on_failure(self):
        tmp = self.stage_tmp()
        with self.assertRaises(M._UploadRejected):
            self.apply(tmp, written=1)
        want = {os.path.realpath(str(p)) for p in (self.dest, self.fdest)}
        for prim, args in self.destructive:
            for a in args:
                self.assertNotIn(os.path.realpath(a), want, f"{prim} 가 dest 를 건드렸다")

    # ── U2: 정상 적재 + 배지 배선 ──────────────────────────────
    def test_U2_success_replaces_dest_and_records_signature(self):
        sha = self.apply(self.stage_tmp())
        self.assertEqual(sorted(_tree(self.dest)), ["new.csv"])
        rec = M.load_versions()[self.DIRKEY]
        self.assertEqual(rec["staged_sig"], sha)
        self.assertEqual(rec["file"], "sangga_202601.zip")

    def test_U2_wires_record_upload_so_badge_resets(self):
        """`_record_upload` 미배선이라 파일을 갈아도 옛 `ok` 배지가 남아 있었다."""
        M._set_validation(self.DIRKEY, "ok", "이전 검증 통과")
        tmp = self.stage_tmp(); size = tmp.stat().st_size   # store_put 이 tmp 를 옮겨간다
        self.apply(tmp)
        rec = M.load_versions()[self.DIRKEY]
        self.assertEqual(rec.get("validation_status"), "pending", "옛 배지가 그대로 남았다")
        self.assertIsNone(rec.get("validation_msg"))
        self.assertEqual(self.history(self.DIRKEY), [("sangga_202601.zip", size)])

    def test_U2_file_type_dest_swapped_atomically(self):
        tmp = self.stage_tmp("new.pbf", raw=b"NEW-PBF-PAYLOAD")
        self.apply(tmp, key=self.FILEKEY, name="korea.osm.pbf", dest=self.fdest)
        self.assertEqual(self.fdest.read_bytes(), b"NEW-PBF-PAYLOAD")
        self.assertFalse((self.fdest.parent / (self.fdest.name + ".incoming")).exists())

    # ── U3: 락 범위 — 파괴 구간만(C-11) ────────────────────────
    def test_U3_refuses_while_collect_holds_lock(self):
        tmp = self.stage_tmp()
        with self.collect_lock():
            with self.assertRaises(M._UploadRejected) as cm:
                self.apply(tmp)
        self.assertEqual(cm.exception.code, 409)
        self.assertDestIntact()
        self.assertNoSignature()

    def test_U3_lock_only_around_destructive_section(self):
        """무결성 검사·store_put 은 락 밖, 교체만 락 안 — 다GB 업로드가 수집을 막지 않게."""
        seen = {}
        real_gate, real_swap = M._collect_integrity_gate, M._swap_dir
        self.addCleanup(setattr, M, "_collect_integrity_gate", real_gate)
        self.addCleanup(setattr, M, "_swap_dir", real_swap)
        M._collect_integrity_gate = lambda *a, **k: (
            seen.__setitem__("gate", M._COLLECT_LOCK.locked()), real_gate(*a, **k))[1]
        M._swap_dir = lambda *a, **k: (
            seen.__setitem__("swap", M._COLLECT_LOCK.locked()), real_swap(*a, **k))[1]
        self.apply(self.stage_tmp())
        self.assertFalse(seen["gate"], "무결성 검사가 락 안에서 돌았다 — 수신 구간이 수집을 막는다")
        self.assertTrue(seen["swap"], "교체가 락 없이 돌았다")

    def test_U3_lock_released_after_failure(self):
        with self.assertRaises(M._UploadRejected):
            self.apply(self.stage_tmp(truncated=True))
        self.assertFalse(M._COLLECT_LOCK.locked(), "락을 물고 나왔다")


# ════════════════════════════════════════════════════════════════
# T027 2단계-C: run_collect 배선 (A2 동수불변식·A3′ 선삭제 제거·A5 배지·A6 status)
# ════════════════════════════════════════════════════════════════
class _ShortList(list):
    """`len()` 은 선언대로지만 실제로는 그보다 적게 내놓는 URL 목록.

    parcel 258→262 처럼 **목록 산출 개수와 실제 수신 개수가 어긋나는 부분수신**을
    모사한다. 동수 불변식이 없으면 반쪽 수신물이 그대로 staged 를 갈아끼운다.
    """

    def __init__(self, items, declared):
        super().__init__(items)
        self._declared = declared

    def __len__(self):
        return self._declared


class TestRunCollect(IsolatedBuildHome):
    """`run_collect` 는 **온전히 받아냈을 때만** staged 를 교체하고, 실패를 반드시 드러낸다.

    사고 재현형: 262건이 전부 0바이트로 실패했는데 `rmtree(dest)` 가 **먼저** 돌아
    staged 를 비웠고, 로그 한 줄 외엔 흔적 없이 `status="done"` 으로 끝나 UI 는
    정상으로 보였다(무증상 고착). 배지도 예전 `ok` 가 그대로 남았다.
    """

    DIRKEY = "sangga"     # dest 확장자 없음 → 디렉터리형(extract) 분기
    FILEKEY = "police"    # dest 를 .pbf 로 돌려 파일형(file) 분기 검사

    def setUp(self):
        super().setUp()
        self.use_sources_copy({self.DIRKEY: "staged/sangga",
                               self.FILEKEY: "staged/osm/korea.osm.pbf"})
        self.dest = self.home / "staged" / "sangga"
        self.orig = {"keep.csv": b"name,lon,lat\nold,127.0,37.0\n"}
        _fill_files(self.orig)(self.dest)
        self.fdest = self.home / "staged" / "osm" / "korea.osm.pbf"
        self.fdest.parent.mkdir(parents=True, exist_ok=True)
        self.forig = b"OLD-PBF-PAYLOAD"
        self.fdest.write_bytes(self.forig)
        self.plans, self.served = {}, {}
        # 실 출처 레지스트리 대신 픽스처를 물린다 — URL·dest·mode 를 케이스별로 통제한다.
        self.patch(M, "_collect_plan", lambda key: self.plans[key])
        self.patch(M, "_http_download", self._fake_download)

    # ── 픽스처 ────────────────────────────────────────────────
    def patch(self, owner, name, new):
        p = mock.patch.object(owner, name, new)
        p.start()
        self.addCleanup(p.stop)

    def spy_on(self, name):
        """`M.<name>` 호출을 기록하되 실제 동작은 유지 — 첫 인자(dest)만 모은다."""
        seen = []
        real = getattr(M, name)
        self.patch(M, name, lambda *a, **k: (seen.append(str(a[0])), real(*a, **k))[1])
        return seen

    def _fake_download(self, url, dest, headers=None, **kw):
        """실 네트워크 대신 등록된 핸들러 — urlopen 은 하네스가 이미 차단한다."""
        return self.served[url](pathlib.Path(dest))

    def plan(self, key, urls, dest, mode):
        self.plans[key] = ({"key": key}, urls, str(dest), mode)

    def serve_zip(self, url, names=("new.csv",)):
        def h(tmp):
            _make_valid_zip(tmp, names=names)
            return tmp.stat().st_size
        self.served[url] = h

    def serve_raw(self, url, raw):
        def h(tmp):
            tmp.write_bytes(raw)
            return len(raw)
        self.served[url] = h

    def serve_fail(self, url, msg="차단/에러 리다이렉트"):
        def h(tmp):
            raise RuntimeError(msg)
        self.served[url] = h

    # ── 공통 단언 ──────────────────────────────────────────────
    def rec(self, key):
        return M.load_versions().get(key, {})

    def job(self):
        return M.MGR.jobs["collect"]

    def assertDirIntact(self):
        self.assertEqual(_tree(self.dest), self.orig, "실패했는데 기존 staged 가 훼손됐다")

    def assertFileIntact(self):
        self.assertEqual(self.fdest.read_bytes(), self.forig, "실패했는데 파일형 dest 가 훼손됐다")

    # ── A3′: 실패해도 기존 staged 는 보존된다 (핵심 회귀) ────────
    def test_C1_dir_failure_keeps_existing_staged(self):
        """1개라도 못 받으면 교체 자체를 하지 않는다 — 예전엔 rmtree 가 선행해 빈 디렉터리만 남았다."""
        self.plan(self.DIRKEY, ["u1", "u2"], self.dest, "extract")
        self.serve_zip("u1")
        self.serve_fail("u2")
        M.run_collect([self.DIRKEY])
        self.assertDirIntact()
        self.assertIsNone(self.rec(self.DIRKEY).get("staged_sig"), "받지도 못하고 거짓 서명을 남겼다")

    def test_C1_no_destructive_primitive_targets_dest_on_failure(self):
        self.plan(self.DIRKEY, ["u1"], self.dest, "extract")
        self.serve_fail("u1")
        self.plan(self.FILEKEY, ["f1"], self.fdest, "file")
        self.serve_fail("f1")
        M.run_collect([self.DIRKEY, self.FILEKEY])
        self.assertNeverTargeted(self.dest, self.fdest)
        self.assertDirIntact()
        self.assertFileIntact()

    def test_C1_empty_but_valid_payload_never_empties_staged(self):
        """무결성 게이트를 **통과하는** 빈 zip — 사고의 실제 형태이자 아직 안 막힌 경로.

        `_payload_integrity` 판정이 ('healthy','zip')이라 게이트가 통과시키고,
        예전 코드는 rmtree 뒤 추출물 0건이라 staged 를 빈 디렉터리로 만들었다.
        (0바이트 원본은 게이트가 'corrupt'로 이미 막는다 — 이쪽이 남은 구멍이다.)
        """
        self.plan(self.DIRKEY, ["u1"], self.dest, "extract")

        def h(tmp):
            _make_empty_zip(tmp)
            return tmp.stat().st_size
        self.served["u1"] = h
        M.run_collect([self.DIRKEY])
        self.assertDirIntact()
        self.assertNeverTargeted(self.dest)
        self.assertEqual(self.job()["status"], "error")
        self.assertEqual(self.rec(self.DIRKEY).get("validation_status"), "collect_failed")
        self.assertIsNone(self.rec(self.DIRKEY).get("staged_sig"),
                          "빈 수신물에 서명을 남기면 다음 회차가 '변경 없음'으로 건너뛴다")

    def test_C1_file_failure_keeps_existing_dest(self):
        self.plan(self.FILEKEY, ["f1"], self.fdest, "file")
        self.serve_fail("f1")
        M.run_collect([self.FILEKEY])
        self.assertFileIntact()
        self.assertIsNone(self.rec(self.FILEKEY).get("staged_sig"))

    # ── A2: 동수 불변식 — 부분수신은 교체 전에 멈춘다 ────────────
    def test_C1_partial_receipt_aborts_before_swap(self):
        self.plan(self.DIRKEY, _ShortList(["u1"], declared=2), self.dest, "extract")
        self.serve_zip("u1")
        M.run_collect([self.DIRKEY])
        self.assertDirIntact()
        self.assertNeverTargeted(self.dest)
        self.assertEqual(self.rec(self.DIRKEY).get("validation_status"), "collect_failed")
        self.assertIsNone(self.rec(self.DIRKEY).get("staged_sig"))
        self.assertEqual(self.job()["status"], "error")

    def test_C1_full_receipt_passes_invariant(self):
        """동수면 통과한다 — 절대 개수 상수가 아니라 **동수 불변식**임을 고정한다."""
        self.plan(self.DIRKEY, ["u1", "u2", "u3"], self.dest, "extract")
        for u, nm in (("u1", "a.csv"), ("u2", "b.csv"), ("u3", "c.csv")):
            self.serve_zip(u, names=(nm,))
        M.run_collect([self.DIRKEY])
        self.assertEqual(self.job()["status"], "done")
        self.assertEqual(sorted(_tree(self.dest)), ["a.csv", "b.csv", "c.csv"])

    # ── A5: 배지 — 성공/실패가 상태 컬럼에 남는다 ────────────────
    def test_C1_success_writes_collect_ok_badge(self):
        self.plan(self.DIRKEY, ["u1"], self.dest, "extract")
        self.serve_zip("u1")
        M.run_collect([self.DIRKEY])
        r = self.rec(self.DIRKEY)
        self.assertEqual(r.get("validation_status"), "collect_ok")
        self.assertIn("1파일", r.get("validation_msg") or "")

    def test_C1_stale_ok_badge_is_overwritten_by_failure(self):
        """무증상성의 직접 원인 — 실패해도 예전 `ok` 배지가 남아 UI 는 정상으로 보였다."""
        M._set_validation(self.DIRKEY, "ok", "예전 검증 통과")
        self.plan(self.DIRKEY, ["u1"], self.dest, "extract")
        self.serve_fail("u1", "빈 응답(0바이트) — 세션/권한 확인")
        M.run_collect([self.DIRKEY])
        r = self.rec(self.DIRKEY)
        self.assertEqual(r.get("validation_status"), "collect_failed")
        self.assertIn("0바이트", r.get("validation_msg") or "")

    # ── A6: 실패 1건이면 정상 종료 경로에서도 status="error" ──────
    def test_C1_status_error_on_normal_exit_when_item_failed(self):
        self.plan(self.DIRKEY, ["u1"], self.dest, "extract")
        self.serve_fail("u1")
        self.plan(self.FILEKEY, ["f1"], self.fdest, "file")
        self.serve_raw("f1", b"NEW-PBF-PAYLOAD")
        M.run_collect([self.DIRKEY, self.FILEKEY])
        self.assertEqual(self.job()["status"], "error", "262건 전부 실패해도 done 이던 그 경로다")
        self.assertTrue(self.job()["log"][-1].startswith("실패 1건"),
                        f"실패 건수가 마지막 줄 선두에 없다: {self.job()['log'][-1]!r}")

    def test_C1_status_done_when_all_succeeded(self):
        self.plan(self.DIRKEY, ["u1"], self.dest, "extract")
        self.serve_zip("u1")
        M.run_collect([self.DIRKEY])
        self.assertEqual(self.job()["status"], "done")
        self.assertTrue(self.job()["log"][-1].startswith("OK:"))

    # ── 프리미티브 통일 — 파일형/디렉터리형 모두 스왑을 거친다 ────
    def test_C1_file_branch_goes_through_swap_file(self):
        seen = self.spy_on("_swap_file")
        self.plan(self.FILEKEY, ["f1"], self.fdest, "file")
        self.serve_raw("f1", b"NEW-PBF-PAYLOAD")
        M.run_collect([self.FILEKEY])
        self.assertEqual(seen, [str(self.fdest)], "파일형이 아직 copy2 로 dest 를 직접 덮는다")
        self.assertEqual(self.fdest.read_bytes(), b"NEW-PBF-PAYLOAD")
        self.assertFalse((self.fdest.parent / (self.fdest.name + ".incoming")).exists())

    def test_C1_dir_branch_goes_through_swap_dir(self):
        seen = self.spy_on("_swap_dir")
        self.plan(self.DIRKEY, ["u1"], self.dest, "extract")
        self.serve_zip("u1")
        M.run_collect([self.DIRKEY])
        self.assertEqual(seen, [str(self.dest)], "디렉터리형이 아직 rmtree+재추출로 돈다")
        self.assertEqual(sorted(_tree(self.dest)), ["new.csv"])
        self.assertFalse((self.dest.parent / (self.dest.name + ".incoming")).exists())


# ════════════════════════════════════════════════════════════════
# T4(데이터부) — data-sources.json `boundary_ri` 등록 (T028 커밋 10)
# ════════════════════════════════════════════════════════════════
class TestBoundaryRiSource(IsolatedBuildHome):
    """T028 §9-T4 중 **데이터 등록분**. 배선 3곳(_VALIDATORS·명령체인·TFRESH)은 커밋 11.

    리(里) 경계는 `06-gen-areas.py --type legal-ri` 로 areas 에 적재할 원천이나
    목록에 없어 재현이 불가능했다(§5). `boundary_legal`(dsId=30603) 을 복제해
    dsId=30602 로 등록한다.

    **이번 태스크에서 수집·적재는 하지 않는다**(§5.1) — 배선만 넣는다.
    """

    RI = "boundary_ri"

    def setUp(self):
        super().setUp()
        self.srcs = {s["key"]: s for s in M.load_sources()}

    def test_registered_and_key_count(self):
        """§8-V6 — 13키 + boundary_ri = 14키."""
        self.assertIn(self.RI, self.srcs, "boundary_ri 가 등록되지 않았다")
        self.assertEqual(len(self.srcs), 14, f"키 14개여야 한다: {sorted(self.srcs)}")

    def test_shape_matches_boundary_legal(self):
        """복제 원본과 필드 구성이 같아야 한다 — 차이는 default_collect 하나뿐.

        `category` 는 `/api/sources` 1674 가 `s["category"]` 로 **직접 인덱싱**하므로
        누락되면 KeyError 로 콘솔 목록 API 가 통째로 죽는다. 값도 원본과 맞춘다
        (계획서 스케치의 "경계" 대신 "행정경계" — 아래 test_category_matches_siblings).
        """
        leg = self.srcs["boundary_legal"]
        ri = self.srcs[self.RI]
        self.assertEqual(set(ri) - set(leg), {"default_collect"})
        self.assertEqual(set(leg) - set(ri), set())

    def test_no_collect_key(self):
        """수동 업로드 전용 — `collect` 를 넣지 않는다.

        복제 원본 `boundary_legal` 에도 `collect` 가 없다(실측). 넣으면 UI 에
        수집 버튼이 살아나는데, VWorld 30602 의 `level`(sido/sigungu)을 확인할
        방법이 이번 태스크엔 없고(수집 금지), 틀리면 `_vworld_list_filenos` 가
        엉뚱한 파일을 받는다. 게다가 `run_collect` 는 dest 를 선삭제한다.
        """
        self.assertNotIn("collect", self.srcs[self.RI])
        self.assertNotIn("collect", self.srcs["boundary_legal"], "원본 전제가 깨졌다")

    def test_uploadable_not_collectable(self):
        """§8-V9 — 카드가 `uploadable:true` / `collectable:false` 로 렌더된다(1675-1676)."""
        ri = self.srcs[self.RI]
        self.assertTrue(bool(ri.get("build_input")), "uploadable 이 false 로 렌더된다")
        self.assertFalse(bool(ri.get("collect")), "collectable 이 true 로 렌더된다")
        self.assertFalse(ri["default_collect"], "자동수집 대상이 되면 안 된다")

    def test_item_dest(self):
        """§9-T4 — `_item_dest` 가 BUILD_HOME/sources/boundary/ri 를 준다."""
        self.assertEqual(self.srcs[self.RI]["build_input"],
                         {"dest": "sources/boundary/ri", "extract": "zip", "into": "dir"})
        dest = M._item_dest(self.RI)
        self.assertIsNotNone(dest, "실재 키여야 _item_dest 가 None 이 아니다")
        self.assertEqual(pathlib.Path(dest), M.BUILD_HOME / "sources/boundary/ri")

    def test_ds_id_30602(self):
        """읍면동(30603)이 아니라 리(30602)를 가리켜야 한다 — 복붙 사고 방지."""
        ri = self.srcs[self.RI]
        for label, url in (("url", ri["url"]), ("latest_check.url", ri["latest_check"]["url"])):
            self.assertIn("dsId=30602", url, f"{label} 이 30602 가 아니다")
            self.assertNotIn("dsId=30603", url, f"{label} 에 원본 dsId 가 남았다")

    def test_latest_check_regex_shared(self):
        """갱신일 파싱 규칙은 boundary_legal 과 동일 — VWorld 상세면 서식이 같다."""
        ri, leg = self.srcs[self.RI], self.srcs["boundary_legal"]
        self.assertEqual(ri["latest_check"]["regex"], leg["latest_check"]["regex"])
        self.assertEqual(ri["latest_check"]["pick"], leg["latest_check"]["pick"])
        self.assertEqual(ri["period_fmt"], leg["period_fmt"])

    def test_category_matches_siblings(self):
        """같은 VWorld 행정구역 경계 3종이 한 카테고리에 모여야 한다.

        계획서 §5.2 스케치는 "경계" 라고 적었으나 기존 두 블록이 "행정경계" 다.
        `category` 는 UI 그룹핑에 쓰이지 않아(build-studio.py 전체에서 1674 한 곳)
        기능 영향은 없지만, 리는 법정동 하위 구역이라 분류가 같아야 맞다.
        """
        for k in ("boundary_legal", "boundary_admin"):
            self.assertEqual(self.srcs[self.RI]["category"], self.srcs[k]["category"])

    def test_note_flags_provisional_fields(self):
        """RI_NM/RI_CD 는 **미검증 잠정값**이다 — SHP 미반입이라 ogrinfo 확인 불가(§8-V5).

        note 에 못박아 두지 않으면 다음 사람이 확정 사실로 읽고 그대로 적재한다.
        """
        note = self.srcs[self.RI]["note"]
        self.assertIn("legal-ri", note)
        self.assertIn("잠정", note)
        self.assertIn("ogrinfo", note)

    def test_aed_note_records_soft_wiring(self):
        """§3.1 — `aed` 는 유령이 아니다. 소비 경로를 note 에 남겨 재오판을 막는다.

        load-all.sh 135-147 이 staged/facility_src/<디렉토리명> 을 그대로 --kind 로
        넘기므로, 업로드만 하면 `--kind aed` 로 자동 적재된다(soft-wired).
        """
        note = self.srcs["aed"]["note"]
        self.assertIn("load-all.sh", note)
        self.assertIn("--kind aed", note)
        self.assertNotIn("collect", self.srcs["aed"], "aed 는 자동수집 미배선 상태 유지")


# ════════════════════════════════════════════════════════════════
# T4(배선부) — build-studio.py areas 3곳 배선 (T028 커밋 11)
#
# 검증기·명령체인·TFRESH 세 곳이 붙어야 리 SHP 가 "업로드하면 그냥 적재되는"
# 원천이 된다. 하나라도 빠지면 조용히 절름발이가 된다:
#   검증기 누락  → 업로드 화면이 "검증기 미정의" warn 으로 통과시킨다
#   명령 누락    → 업로드해도 areas 에 legal-ri 가 영영 안 들어간다
#   TFRESH 누락  → 리 SHP 를 교체해도 areas 가 fresh 로 판정돼 재빌드가 안 된다
# ════════════════════════════════════════════════════════════════
class TestBoundaryRiWiring(IsolatedBuildHome):
    """T028 §9-T4 배선부. 데이터 등록분(data-sources.json)은 커밋 10.

    명령 문자열은 **문자열 단언만으로는 부족하다**. rev0 안이 통째로 폐기된
    이유 3가지(§5.2)가 전부 "문자열은 그럴듯한데 실행하면 틀린" 종류였다:
      1) f-string 리터럴 `{` 미이스케이프 → import 시점 SyntaxError(콘솔 전면 불능)
      2) `$RI` 미정의 → `[ -d "" ]` 가 항상 거짓 → 리가 있어도 영구 건너뜀
      3) `A && B || echo` → 06 실패를 rc=0 으로 삼켜 areas 가 성공으로 보고됨
    그래서 여기서는 **생성된 가드 절편을 실제로 bash 에 넣어 돌린다**(2·3 검출).
    다만 `06-gen-areas.py` 자체는 절대 실행하지 않는다 — geocode.sqlite 에
    DELETE(`06:80-81`)를 거는 스크립트다. python3 호출부만 `echo`/`false` 로
    치환해 **분기 판정과 rc 전파만** 본다.
    """

    RI = "boundary_ri"

    def setUp(self):
        super().setUp()
        self.cmd = M.TARGETS()["areas"]["cmd"]

    # ── 절편 추출 헬퍼 ────────────────────────────────────────
    def _fragment(self, stub):
        """areas 명령의 마지막 ` && ` 조각(= 리 가드)에서 06 호출만 stub 으로 치환.

        `rpartition` 을 쓰는 이유: 가드 내부에는 ` && ` 가 없어야 한다(if/else 로
        쓰기로 한 이유 그 자체). 만약 누군가 가드 안에 `&&` 를 넣으면 절편이
        잘려 뒤따르는 실행 테스트가 깨지므로, 그것도 회귀 신호가 된다.

        **뽑아낸 조각이 진짜 리 가드인지 먼저 확인한다.** 이 단언이 없으면 가드
        자체가 통째로 없을 때 admin-dong 조각이 대신 잡혀, 실행 테스트 3개가
        엉뚱한 명령을 검사하며 초록으로 통과한다(실제로 그렇게 통과했다).
        """
        frag = self.cmd[2].rpartition(" && ")[2]
        self.assertIn("legal-ri", frag, f"마지막 && 조각이 리 가드가 아니다: {frag[:80]!r}")
        return re.sub(r'python3 "[^"]*06-gen-areas\.py"', stub, frag)

    def _run(self, frag):
        return subprocess.run(["bash", "-c", frag], capture_output=True, text=True)

    @property
    def _ri_dir(self):
        return self.home / "sources" / "boundary" / "ri"

    # ── 1) 검증기 ─────────────────────────────────────────────
    def test_validator_registered(self):
        """`_v_boundary` 를 **재사용**한다(동일 함수 객체여야 한다).

        복제본을 만들면 zip-of-zips 처리 로직이 두 벌로 갈라진다. `boundary_legal`
        이 시도별 중첩 zip 인데 리(30602)도 같은 계열이라 같은 검증기가 맞다.
        """
        self.assertIs(M._VALIDATORS.get(self.RI), M._v_boundary)
        self.assertIs(M._VALIDATORS["boundary_legal"], M._v_boundary)

    def test_validator_actually_runs(self):
        """등록만이 아니라 `_validate_source` 경로가 실제로 그 검증기를 탄다.

        미등록이면 `:302-303` 이 ("warn", "N개 파일(검증기 미정의)") 로 빠져
        형식이 틀려도 업로드가 통과한다 — 그 폴백과 구분되는지 본다.
        """
        d = M.SOURCES_DIR / self.RI
        d.mkdir(parents=True, exist_ok=True)
        (d / "LSMD_ADM_SECT_RI_11.shp").write_bytes(b"\x00")
        verdict, detail = M._validate_source(self.RI)
        self.assertEqual(verdict, "ok")
        self.assertNotIn("검증기 미정의", detail)

    # ── 2) TFRESH ─────────────────────────────────────────────
    def test_tfresh_src_includes_ri(self):
        """리 SHP 를 교체하면 areas 가 stale 로 떨어져야 한다.

        `src` 는 staged_sig 변경 추적 대상 원천키 목록이다(`:633`). 빠뜨리면
        새 리 경계를 올려도 areas 가 fresh 로 판정돼 **조용히 재빌드를 건너뛴다**.
        기존 2키가 유지되는지도 함께 본다(치환 사고 방지).
        """
        src = M.TFRESH["areas"]["src"]
        self.assertIn(self.RI, src)
        self.assertIn("boundary_legal", src)
        self.assertIn("boundary_admin", src)
        self.assertEqual(len(src), len(set(src)), "중복 등록")
        self.assertEqual(M.TFRESH["areas"]["dep_art"], ["geocode"])
        self.assertEqual(M.TFRESH["areas"]["scripts"], ["scripts/06-gen-areas.py"])

    # ── 3) 명령 체인 ──────────────────────────────────────────
    def test_areas_cmd_has_legal_ri(self):
        """3번째 호출이 붙었고 `--type legal-ri` 로 나간다.

        `06-gen-areas.py:57` 의 `--type` 은 choices 없는 자유 문자열이라
        오타가 나도 인자 파싱은 통과하고 areas 에 엉뚱한 type 이 적재된다.
        """
        self.assertEqual(self.cmd[:2], ["bash", "-c"])
        s = self.cmd[2]
        self.assertEqual(s.count("06-gen-areas.py"), 3, "legal-dong·admin-dong·legal-ri 3회")
        self.assertIn("--type legal-ri", s)
        self.assertIn("--name-field RI_NM", s)
        self.assertIn("--code-field RI_CD", s)
        self.assertEqual(s.count("--srs EPSG:5186"), 3)
        # 기존 2개는 그대로
        self.assertIn("--type legal-dong", s)
        self.assertIn("--type admin-dong", s)

    def test_areas_cmd_guard_shape(self):
        """`if/else` 여야 한다 — `&&` + `|| echo` 는 **실패를 삼킨다**(폐기 사유 3).

        디렉터리가 있는데 06 이 실패하면 `|| echo` 가 rc 를 0 으로 덮어써
        areas 단계가 성공으로 보고된다. 실행 테스트(test_guard_propagates_failure)
        가 동작으로도 잡지만, 형태 자체를 못박아 재도입을 막는다.
        """
        s = self.cmd[2]
        self.assertIn("if [ -d ", s)
        self.assertIn("; then", s)
        self.assertIn("else", s)
        self.assertTrue(s.rstrip().endswith("fi"), f"가드가 fi 로 닫히지 않는다: …{s[-40:]!r}")
        self.assertNotIn("|| echo", s)

    def test_areas_cmd_no_shell_var(self):
        """경로는 **파이썬이 박는다** — 셸 변수 참조가 있으면 안 된다(폐기 사유 2).

        `bash -c` 가 받는 것은 조립된 문자열뿐이고 `$RI`·`$BUILD_HOME` 은 이
        셸에 상속되지 않는다. 참조하면 `[ -d "" ]` 가 항상 거짓이 되어
        **리 경계가 있어도 영구히 건너뛴다** — 조용해서 가장 늦게 발견된다.
        리터럴 중괄호(폐기 사유 1)도 여기서 함께 차단한다.
        """
        s = self.cmd[2]
        self.assertNotIn("$", s)
        self.assertNotIn("{", s)
        self.assertNotIn("}", s)
        self.assertIn(str(self._ri_dir), s, "BUILD_HOME 파생 실경로가 박혀 있어야 한다")

    def test_areas_cmd_bash_syntax(self):
        """§8-V2b — `py_compile` 은 셸 문법을 못 잡는다. `bash -n` 으로 별도 확인."""
        p = self.home / "_areas_cmd.sh"
        p.write_text(self.cmd[2], encoding="utf-8")
        r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"셸 문법 오류: {r.stderr}")

    # ── 4) 가드 동작(실행) ────────────────────────────────────
    def test_guard_skips_when_absent(self):
        """리 미반입 상태(=지금)에서 rc=0 으로 통과하고 안내를 남긴다.

        이게 커밋 11 을 "실질 무해"하게 만드는 근거다(§10). rc 가 0 이 아니면
        `&&` 체인이 끊겨 **기존 areas 빌드까지 실패**한다.
        """
        self.assertFalse(self._ri_dir.exists())
        r = self._run(self._fragment("echo RAN"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("건너뜀", r.stdout)
        self.assertNotIn("RAN", r.stdout)

    def test_guard_runs_when_present(self):
        """디렉터리가 생기면 실제로 then 분기로 들어간다(`$RI` 사고의 동작 검출).

        문자열 단언만으로는 폐기 사유 2 를 못 잡는다 — `[ -d "$RI" ]` 도
        `if [ -d ` 를 포함하기 때문이다. 분기가 **바뀌는지**를 봐야 한다.
        """
        self._ri_dir.mkdir(parents=True)
        r = self._run(self._fragment("echo RAN"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RAN", r.stdout)
        self.assertIn("--type legal-ri", r.stdout)
        self.assertNotIn("건너뜀", r.stdout)

    def test_guard_propagates_failure(self):
        """06 이 실패하면 rc 가 전파돼야 한다(폐기 사유 3 의 동작 검출).

        `|| echo` 였다면 여기서 rc=0 이 나오고, areas 는 아무 것도 적재하지
        않은 채 '성공'으로 기록된다.
        """
        self._ri_dir.mkdir(parents=True)
        r = self._run(self._fragment("false"))
        self.assertNotEqual(r.returncode, 0, "06 실패가 삼켜졌다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
