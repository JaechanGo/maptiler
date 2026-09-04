#!/usr/bin/env python3
"""T047 P0 §3.3 — 측정 파이프라인의 사전 상태 게이트 (DB·네트워크 불요).

## 무엇을 막는 시험인가

T046 본 측정 12,000 건은 리 사전·안 A 시도 치환·T026 인천 치환이 **전부 꺼진** 프로세스를
상대로 완주했다. 경고 신호는 있었다 — `run_main.json` 의 `window_start.health` 가
`"ERR:HTTP Error 503"` 이었다. 그런데 그것이 **경고에 그쳐 측정이 그대로 진행됐다.**

그래서 여기서 고정하는 계약은 두 줄이다.

  1. `/health` 의 `dict.lawd_ri` 가 `"present"` 가 아니면 **측정을 시작하지 않는다**(경고 아님).
  2. `/health` 자체가 `ERR:*` 이면 그것도 **실패**다 — "모르면 진행"이 이번 사고의 본체다.

호스트에 psycopg 가 없어도 돌아간다(measure.py 최상위는 표준 라이브러리만 쓴다).
실행:  python3 -m unittest scripts.t046.test_dict_gate   또는 이 파일 단독 실행.
"""
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import measure  # noqa: E402


def win(health, **kw):
    w = {"at": "2026-08-22T02:00:00+0900", "geocode_id": "9ae219f7c168",
         "geocode_started": "2026-08-21T15:40:53Z", "health": health}
    w.update(kw)
    return w


HEALTHY = {"ok": True, "places": 16282127, "areas": 8929,
           "dict": {"lawd_ri": "present", "sido_remap": "on",
                    "sgg_remap": "on", "ri_emds": 1411}}


class TestDictGate(unittest.TestCase):

    def test_healthy_passes(self):
        ok, reason = measure.dict_gate(win(HEALTHY))
        self.assertTrue(ok, reason)

    def test_unknown_blocks_and_names_the_boot_race(self):
        """★ 이번 사고의 정확한 형상 — 연결 실패로 로더 5종이 통째로 안 돌았다."""
        h = {"ok": False, "db": "unreachable",
             "dict": {"lawd_ri": "unknown", "sido_remap": "off",
                      "sgg_remap": "off", "ri_emds": 0}}
        ok, reason = measure.dict_gate(win(h))
        self.assertFalse(ok)
        self.assertIn("lawd_ri", reason)
        self.assertIn("unknown", reason)

    def test_absent_blocks(self):
        h = dict(HEALTHY, dict=dict(HEALTHY["dict"], lawd_ri="absent"))
        ok, reason = measure.dict_gate(win(h))
        self.assertFalse(ok)
        self.assertIn("absent", reason)

    def test_health_err_string_is_failure_not_warning(self):
        """T046 은 이 상태에서 그대로 완주했다. 다시는 안 된다."""
        ok, reason = measure.dict_gate(win("ERR:HTTP Error 503: Service Unavailable"))
        self.assertFalse(ok)
        self.assertIn("health", reason.lower())

    def test_missing_dict_key_blocks(self):
        """dict 를 안 싣는 구판 서버는 '모르는 상태'다 — 모르면 재지 않는다."""
        ok, reason = measure.dict_gate(win({"ok": True, "places": 1, "areas": 1}))
        self.assertFalse(ok)
        self.assertIn("dict", reason)

    def test_remap_off_is_reported_but_does_not_block(self):
        """안 A·T026 표는 R-9 상 정당하게 부재할 수 있다 → 차단이 아니라 경고로 남긴다.

        (부팅 경합이면 lawd_ri 가 unknown 이 되므로 위 시험이 이미 잡는다.)
        """
        h = dict(HEALTHY, dict=dict(HEALTHY["dict"], sido_remap="off", sgg_remap="off"))
        ok, reason = measure.dict_gate(win(h))
        self.assertTrue(ok, reason)
        self.assertIn("sido_remap", reason)
        self.assertIn("sgg_remap", reason)

    def test_gate_never_raises_on_junk(self):
        for junk in (None, "", 0, [], {"dict": None}, {"dict": {"lawd_ri": None}}):
            ok, reason = measure.dict_gate(win(junk))
            self.assertIsInstance(ok, bool)
            self.assertIsInstance(reason, str)
            self.assertFalse(ok, f"쓰레기 입력을 통과시켰다: {junk!r}")


class TestDictSnapshotDrift(unittest.TestCase):
    """측정 도중 재기동으로 상태가 바뀐 경우도 사후 판별 가능해야 한다(R7)."""

    def test_same_dict_detected(self):
        self.assertTrue(measure.dict_same(win(HEALTHY), win(HEALTHY)))

    def test_drift_detected(self):
        after = dict(HEALTHY, dict=dict(HEALTHY["dict"], lawd_ri="unknown"))
        self.assertFalse(measure.dict_same(win(HEALTHY), win(after)))

    def test_err_at_either_end_is_drift(self):
        self.assertFalse(measure.dict_same(win(HEALTHY), win("ERR:x")))
        self.assertFalse(measure.dict_same(win("ERR:x"), win(HEALTHY)))


class TestFetchHealth(unittest.TestCase):
    """503 본문의 dict 를 버리면 §3.2 가 소비자에게 닿지 않는다.

    `/health` 는 DB 가 죽어도 `dict` 를 담아 **503 으로** 응답한다. 그런데
    `urlopen` 은 503 에서 `HTTPError` 를 던지므로, 본문을 읽지 않으면 사전 상태가
    통째로 유실되고 게이트는 "응답 없음"이라는 덜 유용한 사유만 남긴다.
    """

    def setUp(self):
        self._saved = measure.urllib.request.urlopen

    def tearDown(self):
        measure.urllib.request.urlopen = self._saved

    def _patch(self, fn):
        measure.urllib.request.urlopen = fn

    def test_200_body_parsed(self):
        self._patch(lambda *a, **k: _Resp(json.dumps(HEALTHY)))
        self.assertEqual(measure.fetch_health("http://x")["dict"]["lawd_ri"], "present")

    def test_503_body_with_dict_is_recovered(self):
        body = {"ok": False, "db": "unreachable",
                "dict": {"lawd_ri": "unknown", "sido_remap": "off",
                         "sgg_remap": "off", "ri_emds": 0}}

        def boom(*a, **k):
            raise measure.urllib.error.HTTPError(
                "http://x", 503, "Service Unavailable", {},
                io.BytesIO(json.dumps(body).encode()))
        self._patch(boom)
        h = measure.fetch_health("http://x")
        self.assertEqual(h["dict"]["lawd_ri"], "unknown")
        ok, reason = measure.dict_gate(win(h))
        self.assertFalse(ok)
        # 사유가 "응답 없음" 이 아니라 **부팅 경합**을 정확히 지목해야 한다.
        self.assertIn("unknown", reason)
        self.assertIn("재기동", reason)

    def test_503_without_body_degrades_to_err_string(self):
        def boom(*a, **k):
            raise measure.urllib.error.HTTPError(
                "http://x", 503, "Service Unavailable", {}, io.BytesIO(b"not json"))
        self._patch(boom)
        h = measure.fetch_health("http://x")
        self.assertIsInstance(h, str)
        self.assertTrue(h.startswith("ERR:"))
        self.assertFalse(measure.dict_gate(win(h))[0])

    def test_connection_refused_degrades_to_err_string(self):
        def boom(*a, **k):
            raise OSError("connection refused")
        self._patch(boom)
        self.assertTrue(measure.fetch_health("http://x").startswith("ERR:"))


class _Resp:
    def __init__(self, body):
        self._b = body.encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestResolveGeocodeContainer(unittest.TestCase):
    """감사 기록이 **측정 대상과 같은 컨테이너**를 기술해야 한다 (T047 검수 6-2).

    종전에는 컨테이너명이 고정이고 `/health` 만 `OUR_BASE` 를 따라, 프로브 포트를 재면
    창(window)이 구 컨테이너의 id·md5 를 적으면서 프로브의 사전 상태를 함께 적었다.
    기록을 믿고 판을 확정하면 다시 오염된다 — T047 이 막으려는 사고와 같은 계열이다.
    """

    @staticmethod
    def _sh(out):
        return lambda args: out

    def test_derives_from_port(self):
        n, why = measure.resolve_geocode_container(
            "http://127.0.0.1:8093", self._sh("t047-probe"))
        self.assertEqual(n, "t047-probe")
        self.assertEqual(why, "port:8093")

    def test_default_port_still_resolves(self):
        n, why = measure.resolve_geocode_container(
            "http://127.0.0.1:8092", self._sh("server-geocode-pg-1"))
        self.assertEqual(n, "server-geocode-pg-1")
        self.assertEqual(why, "port:8092")

    def test_fallback_when_no_container_on_port(self):
        n, why = measure.resolve_geocode_container(
            "http://127.0.0.1:8099", self._sh(""))
        self.assertEqual(n, measure.GEOCODE_CONTAINER_DEFAULT)
        self.assertIn("fallback", why)
        self.assertIn("8099", why)

    def test_fallback_when_ambiguous(self):
        """포트를 둘 이상이 발행하면 **고르지 않는다** — 틀린 기록보다 명시적 폴백이 낫다."""
        n, why = measure.resolve_geocode_container(
            "http://127.0.0.1:8092", self._sh("a\nb\n"))
        self.assertEqual(n, measure.GEOCODE_CONTAINER_DEFAULT)
        self.assertIn("ambiguous", why)
        self.assertIn("a", why)

    def test_fallback_when_docker_errors(self):
        n, why = measure.resolve_geocode_container(
            "http://127.0.0.1:8092", self._sh("ERR:timeout"))
        self.assertEqual(n, measure.GEOCODE_CONTAINER_DEFAULT)
        self.assertEqual(why, "fallback:docker-error")

    def test_fallback_when_base_has_no_port(self):
        for base in ("http://geocode", "", None):
            n, why = measure.resolve_geocode_container(base, self._sh("x"))
            self.assertEqual(n, measure.GEOCODE_CONTAINER_DEFAULT)
            self.assertEqual(why, "fallback:no-port-in-base")

    def test_reason_is_always_recorded(self):
        """조용히 기본값으로 떨어지지 않는다 — 근거가 반드시 창에 남는다."""
        import inspect
        src = inspect.getsource(measure.probe_window)
        self.assertIn("geocode_container_resolved_by", src)
        self.assertIn("resolve_geocode_container", src)
        # 하드코딩 잔재가 남아 있으면 안 된다(기본값 상수 정의는 함수 밖에 있다).
        self.assertNotIn('"server-geocode-pg-1"', src)


class TestGateWiredIntoMain(unittest.TestCase):
    """게이트가 존재만 하고 호출되지 않으면 아무것도 막지 못한다."""

    def test_main_calls_gate_before_sample_load(self):
        import inspect
        src = inspect.getsource(measure.main)
        self.assertIn("dict_gate", src)
        self.assertLess(src.index("dict_gate"), src.index("load_sample"),
                        "게이트가 표본 적재보다 뒤에 있다 — 호출이 나간 뒤에 막아도 늦다")

    def test_override_flag_exists_and_is_recorded(self):
        """차단을 뚫을 수단은 있어야 하되, 반드시 명시적이고 기록에 남아야 한다."""
        import inspect
        src = inspect.getsource(measure.main)
        self.assertIn("allow-dict-degraded", src)
        self.assertIn("dict_gate_override", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
