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
import shutil
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
    "SRC_JUSO",         # 573  (str)
    "SRC_LOCALDATA",    # 574  (str)
    "SRC_GIS",          # 575  (str)
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

    def assertNeverTargeted(self, *paths):
        want = {os.path.realpath(str(p)) for p in paths}
        for prim, args in self.destructive:
            for a in args:
                self.assertNotIn(os.path.realpath(a), want, f"{prim} 가 dest 를 건드렸다")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
