#!/usr/bin/env python3
"""T047 P0 — 부팅 침묵 실패의 방어·탐지 단위시험 (DB 불요).

## 무엇을 지키는 시험인가

2026-08-21T04:46:14Z 기동한 `server-geocode-pg-1` 인스턴스는 `_boot_check` 의
`with POOL.connection()` 이 30 초 타임아웃으로 실패해 **그 블록 안의 로더 5 개가 전부
호출되지 않은 채** 떴다. 전역은 초기값으로 확정됐다:

    _HAS_LAWD_RI=False · _HAS_SIDO_REMAP=False · _HAS_SGG_REMAP=False · _RI_EMDS=frozenset()

즉 리 좁힘·안 A 시도 치환·T026 인천 치환이 **동시에** 꺼졌고, 그 사실을 기계로 읽을
수단이 없어 T046 본 측정 12,000 건이 오염된 채 완주했다. 5 회 기동 중 1 회 발생(20%).

이 파일은 세 가지를 고정한다.

  T1  방어 — 연결 실패는 재시도한다. 단 **절대 deadline** 으로 상한을 묶는다.
             `POOL` 기본 `timeout` 은 30 초이므로 시도마다 명시적으로 짧게 줘야 한다.
             (r2 안은 이 30 초를 빠뜨려 실제 상한이 ~211 초였다.)
  T1b 회귀 — 전량 실패해도 **풀은 열려 있어야 한다**. `POOL.wait()` 는 실패 시 풀을 닫으므로
             금지다 — 쓰면 PostGIS 가 나중에 떠도 모든 요청이 PoolClosed 로 죽어 R-9
             fail-open 계약이 깨진다.
  T2  탐지 — `/health` 는 `POOL.connection()` **밖**에서 사전 상태를 낸다. DB 가 죽은
             바로 그 순간에도 읽혀야 탐지 수단으로서 의미가 있다.

## 가상 시계

`_boot_check` 의 예산은 실시간 60 초다. 시험이 실제로 기다릴 수는 없으므로 모듈 전역
`time` 을 가상 시계로 갈아끼운다. 연결 시도 실패는 **그 시도에 준 timeout 만큼 시계를
소모**하도록 모의한다 — 이렇게 해야 "timeout 을 명시하지 않으면 30 초를 먹는다"는
이번 사고의 핵심이 시험으로 잡힌다.
"""
import contextlib
import importlib.util
import io
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
import psycopg                                    # noqa: E402  (모듈 import 성공 = 가용)
from psycopg_pool import PoolTimeout              # noqa: E402

# psycopg_pool 공식 기본값. POOL(:34) 이 timeout 을 지정하지 않으므로 이 값이 적용된다.
# 구현이 timeout= 을 명시하지 않으면 아래 BootPool 이 이 값만큼 시계를 소모시켜
# 절대 deadline 시험이 실패한다 — 그것이 이 상수의 존재 이유다.
POOL_DEFAULT_TIMEOUT = 30.0


class FakeClock:
    """monotonic/sleep 만 갖는 가상 시계 — 모듈 전역 `time` 을 대체한다."""

    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def advance(self, s):
        self.t += s


class BootCursor:
    """_boot_check 안의 로더 5 개가 던지는 질의에 답하는 커서."""

    def __init__(self, has_ri=True, missing=(), has_sido=True, has_sgg=True,
                 raise_on=None):
        self.has_ri = has_ri
        self.missing = list(missing)
        self.has_sido = has_sido
        self.has_sgg = has_sgg
        self.raise_on = raise_on          # SQL 부분문자열 → 예외
        self._last = None
        self.executed = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.executed.append(s)
        if self.raise_on and self.raise_on[0] in s:
            raise self.raise_on[1]
        if "FROM unnest(%s::text[])" in s:
            self._last = "check_tables"
        elif "public.lawd_ri" in s and "to_regclass" in s:
            self._last = "probe_ri"
        elif "lawd_sido_remap" in s and "to_regclass" in s:
            self._last = "probe_sido"
        elif "FROM lawd_sido_remap" in s:
            self._last = "sido_rows"
        elif "FROM lawd_ri_remap_exception" in s:
            self._last = "exc_rows"
        elif "lawd_sgg_remap" in s and "to_regclass" in s:
            self._last = "probe_sgg"
        elif "FROM lawd_sgg_remap" in s:
            self._last = "sgg_rows"
        elif "FROM lawd_ri WHERE exist" in s:
            self._last = "ri_emds"
        else:
            self._last = "other"
        return self

    def fetchone(self):
        if self._last == "probe_ri":
            return {"ok": self.has_ri}
        if self._last == "probe_sido":
            return {"ok": self.has_sido}
        if self._last == "probe_sgg":
            return {"ok": self.has_sgg}
        return None

    def fetchall(self):
        if self._last == "check_tables":
            return [{"t": t} for t in self.missing]
        if self._last == "sido_rows":
            return [{"o": "46110250", "n": "12110250"}]
        if self._last == "exc_rows":
            return [{"o": "4611025031", "n": "1211025031"}]
        if self._last == "sgg_rows":
            return [{"o": "28110105", "n": "28115105", "g": "제물포구"}]
        if self._last == "ri_emds":
            return [{"e": "46110250"}]
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Ctx:
    def __init__(self, pool, timeout):
        self.pool = pool
        self.timeout = timeout

    def __enter__(self):
        p = self.pool
        if p.closed:
            raise PoolTimeout("the pool is closed")
        exc = p._next()
        if exc is not None:
            # 연결 대기는 **실시간을 먹는다**. timeout 미지정이면 풀 기본값 30 초.
            spent = p.timeouts[-1] if p.timeouts[-1] is not None else POOL_DEFAULT_TIMEOUT
            if p.clock is not None:
                p.clock.advance(spent)
            raise exc
        return _Conn(p.cur)

    def __exit__(self, *a):
        return False


class BootPool:
    """POOL 대체.

    `script` 는 시도별 결과 목록이다 — 예외 인스턴스면 그 예외, None 이면 성공.
    목록이 소진되면 `tail` 을 반복한다(None=이후 전부 성공, 예외=영구 실패).
    """

    def __init__(self, script=(), tail=None, clock=None, cur=None):
        self.script = list(script)
        self.tail = tail
        self.clock = clock
        self.cur = cur or BootCursor()
        self.timeouts = []
        self.closed = False
        self.open_calls = 0
        self.wait_calls = 0

    def _next(self):
        if self.script:
            return self.script.pop(0)
        return self.tail

    def connection(self, timeout=None):
        self.timeouts.append(timeout)
        return _Ctx(self, timeout)

    # 실패 시 풀을 닫는 함정(§3.1.2). 호출되면 시험이 잡는다.
    def wait(self, timeout=None):
        self.wait_calls += 1
        self.closed = True
        raise PoolTimeout("pool not ready")

    def open(self, *a, **kw):
        self.open_calls += 1
        self.closed = False

    def close(self):
        self.closed = True

    @property
    def attempts(self):
        return len(self.timeouts)


class _BootBase(unittest.TestCase):
    """전역 오염 방지 — 매 시험마다 원상복구."""

    GLOBALS = ("_HAS_LAWD_RI", "_HAS_SIDO_REMAP", "_HAS_SGG_REMAP",
               "_SIDO_REMAP", "_RI_REMAP_EXC", "_SGG_REMAP", "_RI_EMDS",
               "_DICT_STATE", "POOL", "time")

    def setUp(self):
        self._saved = {k: getattr(M, k, None) for k in self.GLOBALS}
        self.clock = FakeClock()
        M.time = self.clock

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None and not hasattr(M, k):
                continue
            setattr(M, k, v)

    def run_boot(self, pool):
        """_boot_check 을 돌리고 (stderr 전문, 소요 가상초) 를 돌려준다."""
        M.POOL = pool
        t0 = self.clock.monotonic()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            M._boot_check()
        return buf.getvalue(), self.clock.monotonic() - t0


# ════════════════════════════════════════════════════════════════
# T1 — 방어: 연결 실패 재시도 + 절대 deadline
# ════════════════════════════════════════════════════════════════
class TestBootRetry(_BootBase):

    def test_first_attempt_success_no_retry(self):
        """정상 기동은 종전과 같이 1 회로 끝난다(지연 0)."""
        pool = BootPool(script=[None], clock=self.clock)
        err, elapsed = self.run_boot(pool)
        self.assertEqual(pool.attempts, 1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(elapsed, 0.0)
        self.assertEqual(M._DICT_STATE, "present")
        self.assertIn("lawd_ri: present", err)
        # 로더 5 개가 전부 돌았다 — 리만이 아니라 안 A·T026 까지.
        self.assertTrue(M._HAS_LAWD_RI)
        self.assertTrue(M._HAS_SIDO_REMAP)
        self.assertTrue(M._HAS_SGG_REMAP)
        self.assertIn("sido remap(안 A): ON", err)
        self.assertIn("incheon sgg remap(T026): ON", err)

    def test_every_attempt_passes_explicit_short_timeout(self):
        """timeout 미지정이면 시도당 30 초를 먹는다 — 반드시 명시해야 한다."""
        pool = BootPool(script=[PoolTimeout("x"), None], clock=self.clock)
        self.run_boot(pool)
        self.assertTrue(pool.timeouts, "connection() 이 한 번도 호출되지 않았다")
        for t in pool.timeouts:
            self.assertIsNotNone(t, "POOL.connection() 에 timeout 을 명시하지 않았다 "
                                    "— 풀 기본값 30 초가 적용된다")
            self.assertLessEqual(t, 5.0)
        self.assertEqual(pool.timeouts[0], M.BOOT_TRY_TIMEOUT_S)

    def test_retries_until_postgis_up(self):
        """PostGIS 가 늦게 떠도 present 로 수렴한다 — 이번 사고의 직접 해소."""
        pool = BootPool(script=[PoolTimeout("a"), PoolTimeout("b"), None],
                        clock=self.clock)
        err, _ = self.run_boot(pool)
        self.assertEqual(pool.attempts, 3)
        self.assertEqual(M._DICT_STATE, "present")
        self.assertIn("lawd_ri: present", err)
        self.assertIn("attempts", err)          # 재시도했음을 로그로 알린다

    def test_backoff_is_exponential_and_capped(self):
        pool = BootPool(script=[PoolTimeout("x")] * 5 + [None], clock=self.clock)
        self.run_boot(pool)
        self.assertEqual(self.clock.sleeps[:4], [1.0, 2.0, 4.0, 8.0])
        self.assertTrue(all(s <= 8.0 for s in self.clock.sleeps))

    def test_operational_error_is_retried(self):
        """PostGIS 미기동은 OperationalError 로도 온다."""
        pool = BootPool(script=[psycopg.OperationalError("starting up"), None],
                        clock=self.clock)
        self.run_boot(pool)
        self.assertEqual(pool.attempts, 2)
        self.assertEqual(M._DICT_STATE, "present")

    def test_absolute_deadline_63s(self):
        """전량 실패해도 63 초 안에 끝난다(60 s deadline + 진행 중이던 시도 3 s)."""
        pool = BootPool(tail=PoolTimeout("forever"), clock=self.clock)
        err, elapsed = self.run_boot(pool)
        self.assertLessEqual(elapsed, M.BOOT_DB_WAIT_S + M.BOOT_TRY_TIMEOUT_S)
        self.assertLessEqual(elapsed, 63.0)
        self.assertGreater(elapsed, M.BOOT_DB_WAIT_S * 0.5,
                           "deadline 을 다 쓰지 않고 조기 포기했다")

    def test_total_failure_state_is_unknown_not_absent(self):
        """'사전이 없음'과 '확인 못 함'은 운영상 다른 사건이다 — 반드시 구분한다."""
        pool = BootPool(tail=PoolTimeout("forever"), clock=self.clock)
        err, _ = self.run_boot(pool)
        self.assertEqual(M._DICT_STATE, "unknown")
        self.assertNotEqual(M._DICT_STATE, "absent")
        self.assertIn("UNKNOWN", err)
        # 리만 꺼진 게 아니라는 사실을 로그에 남긴다(이번 사고의 교훈).
        self.assertIn("NOT loaded", err)
        self.assertIn("sido remap", err)
        self.assertIn("T026", err)

    def test_table_absent_is_not_retried(self):
        """to_regclass 가 정상 응답한 '테이블 부재'는 재시도 대상이 아니다."""
        pool = BootPool(script=[None], clock=self.clock,
                        cur=BootCursor(has_ri=False))
        err, elapsed = self.run_boot(pool)
        self.assertEqual(pool.attempts, 1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(elapsed, 0.0)
        self.assertEqual(M._DICT_STATE, "absent")
        self.assertIn("lawd_ri: absent", err)

    def test_query_error_is_not_retried(self):
        """연결은 됐는데 질의가 깨진 경우 — 재시도해도 소용없다. 즉시 UNKNOWN."""
        pool = BootPool(script=[None], clock=self.clock,
                        cur=BootCursor(raise_on=("unnest", RuntimeError("boom"))))
        err, elapsed = self.run_boot(pool)
        self.assertEqual(pool.attempts, 1)
        self.assertEqual(elapsed, 0.0)
        self.assertEqual(M._DICT_STATE, "unknown")
        self.assertIn("boot check error", err)

    def test_missing_required_tables_still_warns(self):
        """기존 degraded 경고는 그대로 남는다."""
        pool = BootPool(script=[None], clock=self.clock,
                        cur=BootCursor(missing=["admin_boundary"]))
        err, _ = self.run_boot(pool)
        self.assertIn("degraded", err)
        self.assertIn("admin_boundary", err)

    def test_fail_open_never_raises(self):
        """R-9: 실패해도 프로세스는 뜬다. 바꾸는 것은 *조용함*이지 *열림*이 아니다."""
        pool = BootPool(tail=psycopg.OperationalError("down"), clock=self.clock)
        M.POOL = pool
        with contextlib.redirect_stderr(io.StringIO()):
            M._boot_check()          # 예외가 새어 나오면 시험 실패


# ════════════════════════════════════════════════════════════════
# T1b — 회귀: 전량 실패 후에도 풀은 열려 있어야 한다
# ════════════════════════════════════════════════════════════════
class TestPoolStaysOpen(_BootBase):

    def test_pool_open_after_total_failure(self):
        pool = BootPool(tail=PoolTimeout("forever"), clock=self.clock)
        self.run_boot(pool)
        self.assertFalse(pool.closed,
                         "부팅 점검 실패가 풀을 닫았다 — 이후 모든 요청이 PoolClosed 로 죽는다")
        self.assertEqual(pool.wait_calls, 0,
                         "POOL.wait() 를 호출했다 — 공식 문서상 실패 시 풀이 닫힌다(§3.1.2)")

    def test_requests_work_after_boot_failure(self):
        """PostGIS 가 뒤늦게 떠도 서비스는 정상 복귀한다(fail-open 의 실질)."""
        pool = BootPool(tail=PoolTimeout("forever"), clock=self.clock)
        self.run_boot(pool)
        pool.tail = None                       # PostGIS 기동 완료
        pool.script = []
        with pool.connection(timeout=3) as con:
            self.assertIsNotNone(con.cursor())

    def test_source_does_not_call_pool_wait(self):
        """`POOL.wait()` 금지를 소스 수준에서 못박는다(§3.1.2 함정).

        문자열·주석의 언급은 허용한다(문서화는 오히려 권장) — AST 로 **실제 호출**만 잡는다.
        """
        import ast
        with open(_MOD_PATH, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        bad = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute) and n.func.attr == "wait"
               and isinstance(n.func.value, ast.Name) and n.func.value.id == "POOL"]
        self.assertEqual(bad, [], f"POOL.wait() 호출 발견(행 {bad}) — 실패 시 풀이 닫힌다")


# ════════════════════════════════════════════════════════════════
# T2 — 탐지: /health 는 DB 밖에서 사전 상태를 낸다
# ════════════════════════════════════════════════════════════════
class _Recorder:
    def __init__(self):
        self.obj = None
        self.code = None

    def __call__(self, obj, code=200):
        self.obj = obj
        self.code = code


class HealthCursor(BootCursor):
    """/health 의 count(*) 두 방까지 답하는 커서."""

    def __init__(self, missing=(), counts=(16282127, 8929)):
        super().__init__(missing=missing)
        self.counts = list(counts)

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.executed.append(s)
        if "count(*) c FROM address" in s:
            self._last = "n_addr"
        elif "count(*) c FROM admin_boundary" in s:
            self._last = "n_area"
        else:
            return super().execute(sql, params)
        return self

    def fetchone(self):
        if self._last == "n_addr":
            return {"c": self.counts[0]}
        if self._last == "n_area":
            return {"c": self.counts[1]}
        return super().fetchone()


class TestHealthDictExposure(_BootBase):

    def _get(self, pool, path="/health"):
        H = M.Handler.__new__(M.Handler)
        H.path = path
        rec = _Recorder()
        H._send = rec
        M.POOL = pool
        H.do_GET()
        return rec

    def test_ok_response_keys_preserved_plus_dict(self):
        """하위 호환 — ok/places/areas 를 그대로 두고 dict 만 더한다."""
        M._DICT_STATE = "present"
        M._HAS_SIDO_REMAP = True
        M._HAS_SGG_REMAP = True
        M._RI_EMDS = frozenset({"46110250"})
        rec = self._get(BootPool(script=[None], cur=HealthCursor()))
        self.assertEqual(rec.code, 200)
        self.assertTrue(rec.obj["ok"])
        self.assertEqual(rec.obj["places"], 16282127)
        self.assertEqual(rec.obj["areas"], 8929)
        d = rec.obj["dict"]
        self.assertEqual(d["lawd_ri"], "present")
        self.assertEqual(d["sido_remap"], "on")
        self.assertEqual(d["sgg_remap"], "on")
        self.assertEqual(d["ri_emds"], 1)

    def test_missing_tables_503_preserved_plus_dict(self):
        """기존 degraded 503 계약을 깨지 않는다(R8)."""
        M._DICT_STATE = "present"
        rec = self._get(BootPool(script=[None],
                                 cur=HealthCursor(missing=["admin_boundary"])))
        self.assertEqual(rec.code, 503)
        self.assertFalse(rec.obj["ok"])
        self.assertTrue(rec.obj["degraded"])
        self.assertIn("admin_boundary", rec.obj["missing_tables"])
        self.assertEqual(rec.obj["dict"]["lawd_ri"], "present")

    def test_dict_survives_db_outage(self):
        """★ 핵심 — 탐지 수단이 탐지 대상 상황에서 함께 죽으면 안 된다."""
        M._DICT_STATE = "unknown"
        M._HAS_SIDO_REMAP = False
        M._HAS_SGG_REMAP = False
        M._RI_EMDS = frozenset()
        rec = self._get(BootPool(tail=psycopg.OperationalError("conn down"),
                                 clock=self.clock))
        self.assertEqual(rec.code, 503)
        self.assertFalse(rec.obj["ok"])
        self.assertEqual(rec.obj["db"], "unreachable")
        d = rec.obj["dict"]
        self.assertEqual(d["lawd_ri"], "unknown")
        self.assertEqual(d["sido_remap"], "off")
        self.assertEqual(d["sgg_remap"], "off")

    def test_health_reflects_absent_state(self):
        M._DICT_STATE = "absent"
        rec = self._get(BootPool(script=[None], cur=HealthCursor()))
        self.assertEqual(rec.obj["dict"]["lawd_ri"], "absent")

    def test_health_uses_bounded_timeout(self):
        """/health 가 풀 기본 30 초를 물고 늘어지면 헬스체크로서 쓸모가 없다."""
        pool = BootPool(script=[None], cur=HealthCursor())
        self._get(pool)
        self.assertTrue(pool.timeouts)
        for t in pool.timeouts:
            self.assertIsNotNone(t)
            self.assertLessEqual(t, 5.0)

    def test_geocode_exception_ladder_untouched(self):
        """/health 를 밖으로 뺐다고 /geocode 의 503/500 사다리가 흔들리면 안 된다."""
        rec = self._get(BootPool(tail=psycopg.OperationalError("down"),
                                 clock=self.clock), path="/geocode?q=강남역")
        self.assertEqual(rec.code, 503)
        self.assertIn("error", rec.obj)

    def test_unknown_endpoint_still_404(self):
        rec = self._get(BootPool(script=[None], cur=HealthCursor()), path="/nope")
        self.assertEqual(rec.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
