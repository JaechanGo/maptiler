#!/usr/bin/env python3
"""T046 §7 — 호출 제어. 토큰 버킷·재시도·즉시 중단·카운터.

계획 §5 가 못박은 계약:
  - 동시성 4, 초당 상한 8 req/s (토큰 버킷)
  - 재시도는 네트워크 오류·5xx·타임아웃(10 s)에 한해 지수 백오프 3 회
    (1 s → 2 s → 4 s, ±20% 지터)
  - `status=NOT_FOUND` 는 재시도하지 않는다 — 정상 응답이며 실패로 계수한다
  - `OVER_REQUEST_LIMIT` 또는 그에 준하는 `ERROR` 는 즉시 전체 중단
  - 카운터: 총 호출(순/역 분리), OK / NOT_FOUND / ERROR(코드별), 네트워크 실패,
    재시도 발생, 재시도 후 성공, 최종 포기, 한도초과 여부, 평균·p95 응답시간
  - 우리 8092 의 5xx·타임아웃은 **별도 카운터**(게이트 E2)

§7 말미: "`test_ratelimit.py` 는 VWorld 를 실제로 부르지 않는다 — 가짜 응답
객체를 주입한다." 이 파일은 네트워크를 건드리지 않는다. 시계도 가짜다.

실행: /usr/bin/python3 -m unittest discover -s tests/t046 -t . -v
"""
import threading
import time
import unittest

import _ctx  # noqa: F401  (sys.path 주입)

import measure  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# 주입용 가짜들
# ─────────────────────────────────────────────────────────────────────────────

class FakeClock(object):
    """단조 시계 + sleep. sleep 이 시각을 밀어준다 — 실제로 기다리지 않는다."""

    def __init__(self, start=0.0):
        self.t = float(start)
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        assert seconds >= 0.0, seconds
        self.slept.append(seconds)
        self.t += seconds

    def advance(self, seconds):
        self.t += seconds


def ok_body(x="127.0", y="37.0"):
    return {"response": {"status": "OK",
                         "result": {"crs": "EPSG:4326", "point": {"x": x, "y": y}}}}


def not_found_body():
    return {"response": {"status": "NOT_FOUND"}}


def error_body(code):
    return {"response": {"status": "ERROR",
                         "error": {"level": "error", "code": code, "text": "…"}}}


class Responder(object):
    """호출 순서대로 미리 정한 결과를 돌려주는 가짜 엔드포인트.

    각 항목은 `(http_status, payload)` 이거나, 던질 예외 인스턴스다.
    목록이 바닥나면 마지막 항목을 계속 반복한다.
    """

    def __init__(self, *scripted):
        self.scripted = list(scripted)
        self.calls = 0

    def __call__(self):
        item = self.scripted[min(self.calls, len(self.scripted) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item


def fixed_rng(value):
    """지터용 난수를 고정한다. 0.5 → 지터 0, 0.0 → −20%, 1.0 → +20%."""
    return lambda: value


def drain(bucket, n, clock):
    """`acquire()` 를 n 회 돌리고 각 호출이 성립한 시각을 돌려준다."""
    stamps = []
    for _ in range(n):
        bucket.acquire()
        stamps.append(clock.now())
    return stamps


def max_in_any_window(stamps, window=1.0):
    """어떤 길이 `window` 의 반열린 창 [t, t+window) 에도 몇 개까지 들어가는지."""
    worst = 0
    for start in stamps:
        cnt = sum(1 for t in stamps if start <= t < start + window)
        worst = max(worst, cnt)
    return worst


# ─────────────────────────────────────────────────────────────────────────────
# 토큰 버킷
# ─────────────────────────────────────────────────────────────────────────────

class TestTokenBucket(unittest.TestCase):
    """8 req/s 상한을 **가짜 시계**로 검정한다(§5, §7)."""

    def test_declared_limits_match_plan(self):
        self.assertEqual(measure.RATE_LIMIT_RPS, 8.0)
        self.assertEqual(measure.CONCURRENCY, 4)
        self.assertEqual(measure.TIMEOUT_S, 10.0)

    def test_capacity_is_one(self):
        """버킷 용량은 1 이다 — 이 값이 '초당 8' 을 슬라이딩 창에서 성립시킨다.

        계획 §5 는 용량을 적지 않았다. 용량 8 로 두면 정지 상태에서 8 건이 한
        순간에 터진 뒤 곧바로 초당 8 건이 이어져 [0,1) 창에 15 건이 들어간다 —
        선언한 상한의 두 배에 가깝다. 용량 1 은 매 호출을 1/8 초 간격으로 고르게
        미는 순수 페이싱이며, 계획 §9 의 소요 산정(`26,700/8 = 3,337.5 s`)도
        정확히 이 모델을 전제한다.
        """
        self.assertEqual(measure.BUCKET_CAPACITY, 1)

    def test_never_exceeds_eight_per_second(self):
        clock = FakeClock()
        bucket = measure.TokenBucket(rate=8.0, capacity=1,
                                     now=clock.now, sleep=clock.sleep)
        stamps = drain(bucket, 100, clock)
        self.assertLessEqual(max_in_any_window(stamps, 1.0), 8)

    def test_paces_at_125ms(self):
        clock = FakeClock()
        bucket = measure.TokenBucket(rate=8.0, capacity=1,
                                     now=clock.now, sleep=clock.sleep)
        stamps = drain(bucket, 9, clock)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        for gap in gaps:
            self.assertAlmostEqual(gap, 0.125, places=9)
        self.assertAlmostEqual(stamps[-1] - stamps[0], 1.0, places=9)

    def test_first_call_does_not_sleep(self):
        clock = FakeClock()
        bucket = measure.TokenBucket(rate=8.0, capacity=1,
                                     now=clock.now, sleep=clock.sleep)
        bucket.acquire()
        self.assertEqual(clock.slept, [])

    def test_idle_does_not_bank_a_burst(self):
        """오래 쉬어도 크레딧이 쌓이지 않는다 — 용량 1 의 핵심 성질."""
        clock = FakeClock()
        bucket = measure.TokenBucket(rate=8.0, capacity=1,
                                     now=clock.now, sleep=clock.sleep)
        bucket.acquire()
        clock.advance(60.0)          # 1 분 유휴 = 480 토큰 분량
        stamps = drain(bucket, 10, clock)
        self.assertLessEqual(max_in_any_window(stamps, 1.0), 8)

    def test_external_delay_is_absorbed(self):
        """호출이 오래 걸린 만큼은 기다리지 않는다 — 페이싱이지 강제 지연이 아니다."""
        clock = FakeClock()
        bucket = measure.TokenBucket(rate=8.0, capacity=1,
                                     now=clock.now, sleep=clock.sleep)
        bucket.acquire()
        clock.advance(0.5)           # 응답이 0.5 초 걸렸다
        bucket.acquire()
        self.assertEqual(clock.slept, [])

    def test_thread_safe_under_concurrency_four(self):
        """스레드 4 개가 동시에 두드려도 상한이 무너지지 않는다.

        여기서만 실시간 시계를 쓴다(경합은 가짜 시계로 재현되지 않는다).
        rate 를 200 으로 올려 총 소요를 0.2 초 안쪽으로 묶는다.
        """
        rate = 200.0
        bucket = measure.TokenBucket(rate=rate, capacity=1)
        stamps = []
        lock = threading.Lock()

        def worker():
            for _ in range(10):
                bucket.acquire()
                with lock:
                    stamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(stamps), 40)
        stamps.sort()
        # 창 안 개수로 검정한다. 스케줄러 지터를 감안해 1 건 여유를 준다.
        self.assertLessEqual(max_in_any_window(stamps, 1.0), int(rate) + 1)
        self.assertGreaterEqual(stamps[-1] - stamps[0], 39.0 / rate * 0.9)


# ─────────────────────────────────────────────────────────────────────────────
# 백오프 계산
# ─────────────────────────────────────────────────────────────────────────────

class TestBackoff(unittest.TestCase):
    """1 s → 2 s → 4 s, ±20% 지터(§5)."""

    def test_schedule_is_one_two_four(self):
        self.assertEqual(measure.BACKOFF_SCHEDULE, (1.0, 2.0, 4.0))
        self.assertEqual(measure.MAX_RETRIES, 3)
        self.assertEqual(len(measure.BACKOFF_SCHEDULE), measure.MAX_RETRIES)

    def test_zero_jitter_gives_base(self):
        for i, base in enumerate(measure.BACKOFF_SCHEDULE):
            self.assertAlmostEqual(
                measure.backoff_delay(i, rng=fixed_rng(0.5)), base, places=12)

    def test_jitter_is_within_twenty_percent(self):
        self.assertEqual(measure.JITTER_FRAC, 0.2)
        for i, base in enumerate(measure.BACKOFF_SCHEDULE):
            lo = measure.backoff_delay(i, rng=fixed_rng(0.0))
            hi = measure.backoff_delay(i, rng=fixed_rng(1.0))
            self.assertAlmostEqual(lo, base * 0.8, places=12)
            self.assertAlmostEqual(hi, base * 1.2, places=12)

    def test_delay_is_never_negative(self):
        for i in range(measure.MAX_RETRIES):
            for r in (0.0, 0.25, 0.5, 0.75, 1.0):
                self.assertGreater(measure.backoff_delay(i, rng=fixed_rng(r)), 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# 재시도 정책
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryPolicy(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        self.counters = measure.Counters()

    def call(self, responder, service="vworld", direction="fwd", rng=fixed_rng(0.5)):
        return measure.call_with_retry(
            responder, service=service, direction=direction,
            counters=self.counters, sleep=self.clock.sleep,
            now=self.clock.now, rng=rng)

    # ── NOT_FOUND ────────────────────────────────────────────────────────────
    def test_not_found_is_not_retried(self):
        """정상 응답이다. 다시 물어도 답이 바뀌지 않으므로 호출을 낭비하지 않는다."""
        r = Responder((200, not_found_body()))
        out = self.call(r)
        self.assertEqual(r.calls, 1)
        self.assertEqual(clock_slept(self.clock), [])
        self.assertEqual(out.status, "NOT_FOUND")
        self.assertFalse(out.ok)
        self.assertEqual(out.attempts, 1)
        self.assertEqual(self.counters.status[("vworld", "fwd", "NOT_FOUND")], 1)
        self.assertEqual(self.counters.retried[("vworld", "fwd")], 0)

    def test_not_found_is_counted_as_failure_not_success(self):
        """§6·프롬프트: 부재를 성공으로 계산하지 않는다."""
        self.call(Responder((200, not_found_body())))
        self.assertEqual(self.counters.status[("vworld", "fwd", "OK")], 0)

    # ── 5xx ──────────────────────────────────────────────────────────────────
    def test_five_xx_retries_three_times_then_gives_up(self):
        r = Responder((500, None))
        out = self.call(r)
        self.assertEqual(r.calls, 4)                      # 최초 1 + 재시도 3
        self.assertEqual(self.clock.slept, [1.0, 2.0, 4.0])
        self.assertFalse(out.ok)
        self.assertEqual(out.attempts, 4)
        self.assertEqual(self.counters.retried[("vworld", "fwd")], 1)
        self.assertEqual(self.counters.retried_ok[("vworld", "fwd")], 0)
        self.assertEqual(self.counters.gave_up[("vworld", "fwd")], 1)

    def test_five_xx_then_success_counts_retry_success(self):
        r = Responder((500, None), (503, None), (200, ok_body()))
        out = self.call(r)
        self.assertEqual(r.calls, 3)
        self.assertEqual(self.clock.slept, [1.0, 2.0])
        self.assertTrue(out.ok)
        self.assertEqual(out.status, "OK")
        self.assertEqual(self.counters.retried[("vworld", "fwd")], 1)
        self.assertEqual(self.counters.retried_ok[("vworld", "fwd")], 1)
        self.assertEqual(self.counters.gave_up[("vworld", "fwd")], 0)

    def test_retried_counter_counts_calls_not_attempts(self):
        """'재시도 발생 건수' 는 건 단위다. 한 건이 3 번 자도 1 로 센다."""
        self.call(Responder((500, None)))
        self.call(Responder((500, None), (200, ok_body())))
        self.assertEqual(self.counters.retried[("vworld", "fwd")], 2)

    def test_four_xx_is_not_retried(self):
        """4xx 는 재시도 대상이 아니다(§5 는 네트워크·5xx·타임아웃만 든다)."""
        r = Responder((400, None))
        out = self.call(r)
        self.assertEqual(r.calls, 1)
        self.assertEqual(self.clock.slept, [])
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "HTTP_400")

    # ── 네트워크·타임아웃 ────────────────────────────────────────────────────
    def test_network_error_is_retried(self):
        r = Responder(OSError("connection reset"), OSError("x"), (200, ok_body()))
        out = self.call(r)
        self.assertEqual(r.calls, 3)
        self.assertTrue(out.ok)
        self.assertEqual(self.counters.network_fail[("vworld", "fwd")], 2)

    def test_timeout_is_retried_then_given_up(self):
        r = Responder(measure.CallTimeout("10 s"))
        out = self.call(r)
        self.assertEqual(r.calls, 4)
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "TIMEOUT")
        self.assertEqual(self.counters.gave_up[("vworld", "fwd")], 1)

    # ── 즉시 중단 ────────────────────────────────────────────────────────────
    def test_over_request_limit_aborts_immediately(self):
        r = Responder((200, error_body("OVER_REQUEST_LIMIT")))
        with self.assertRaises(measure.OverRequestLimit):
            self.call(r)
        self.assertEqual(r.calls, 1)
        self.assertEqual(self.clock.slept, [])
        self.assertTrue(self.counters.over_limit)

    def test_over_request_limit_is_in_fatal_set(self):
        self.assertIn("OVER_REQUEST_LIMIT", measure.FATAL_ERROR_CODES)

    def test_http_429_aborts_immediately(self):
        """한도초과의 HTTP 표현. §5 '그에 준하는 ERROR' 로 본다.

        계획 본문은 body 의 `OVER_REQUEST_LIMIT` 만 명시했다. HTTP 429 를
        5xx 처럼 재시도하면 한도를 넘긴 채로 세 번 더 두드리게 되므로,
        같은 취급(즉시 중단)으로 둔다. 보고서 ⑦ 기재 대상.
        """
        r = Responder((429, None))
        with self.assertRaises(measure.OverRequestLimit):
            self.call(r)
        self.assertEqual(r.calls, 1)
        self.assertTrue(self.counters.over_limit)

    def test_non_fatal_error_is_counted_by_code_and_not_retried(self):
        r = Responder((200, error_body("INVALID_PARAMETER")))
        out = self.call(r)
        self.assertEqual(r.calls, 1)
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "ERROR")
        self.assertEqual(out.error_code, "INVALID_PARAMETER")
        self.assertEqual(
            self.counters.error_codes[("vworld", "fwd", "INVALID_PARAMETER")], 1)


# ─────────────────────────────────────────────────────────────────────────────
# 카운터
# ─────────────────────────────────────────────────────────────────────────────

class TestCounters(unittest.TestCase):
    """§5 '집계 대상 카운터 — 전부 리포트에 싣는다'."""

    def setUp(self):
        self.clock = FakeClock()
        self.counters = measure.Counters()

    def call(self, responder, service="vworld", direction="fwd", cost=0.0):
        def wrapped():
            self.clock.advance(cost)
            return responder()
        return measure.call_with_retry(
            wrapped, service=service, direction=direction,
            counters=self.counters, sleep=self.clock.sleep,
            now=self.clock.now, rng=fixed_rng(0.5))

    def test_forward_and_reverse_are_split(self):
        self.call(Responder((200, ok_body())), direction="fwd")
        self.call(Responder((200, ok_body())), direction="fwd")
        self.call(Responder((200, ok_body())), direction="rev")
        self.assertEqual(self.counters.calls[("vworld", "fwd")], 2)
        self.assertEqual(self.counters.calls[("vworld", "rev")], 1)

    def test_calls_counts_attempts_including_retries(self):
        """'총 호출 수' 는 실제로 나간 요청 수다 — 예산 소진의 척도이므로."""
        self.call(Responder((500, None), (200, ok_body())))
        self.assertEqual(self.counters.calls[("vworld", "fwd")], 2)

    def test_our_server_failures_are_counted_separately(self):
        """게이트 E2. 우리 서버가 죽어서 생긴 무응답을 커버리지 결손으로 세면 안 된다."""
        self.call(Responder((500, None)), service="ours")
        self.call(Responder((200, {"results": []})), service="ours")
        self.assertEqual(self.counters.gave_up[("ours", "fwd")], 1)
        self.assertEqual(self.counters.gave_up[("vworld", "fwd")], 0)
        self.assertEqual(self.counters.gate_e2(), 1)

    def test_gate_e1_counts_vworld_non_ok(self):
        self.call(Responder((200, not_found_body())))
        self.call(Responder((200, error_body("INVALID_PARAMETER"))))
        self.call(Responder((200, ok_body())))
        self.assertEqual(self.counters.gate_e1(), 2)

    def test_our_server_has_no_body_status(self):
        """우리 8092 응답에는 VWorld 의 `status` 필드가 없다. HTTP 200 이면 OK."""
        out = self.call(Responder((200, {"results": []})), service="ours")
        self.assertTrue(out.ok)
        self.assertEqual(out.status, "OK")

    def test_latency_mean_and_p95(self):
        for cost in [0.10, 0.20, 0.30, 0.40, 0.50,
                     0.60, 0.70, 0.80, 0.90, 1.00,
                     1.10, 1.20, 1.30, 1.40, 1.50,
                     1.60, 1.70, 1.80, 1.90, 2.00]:
            self.call(Responder((200, ok_body())), cost=cost)
        key = ("vworld", "fwd")
        self.assertEqual(len(self.counters.latency[key]), 20)
        self.assertAlmostEqual(self.counters.mean(key), 1.05, places=6)
        # 최근접 순위법: ceil(0.95 × 20) = 19 번째 = 1.90
        self.assertAlmostEqual(self.counters.p95(key), 1.90, places=6)

    def test_p95_on_empty_is_none(self):
        self.assertIsNone(self.counters.p95(("vworld", "rev")))
        self.assertIsNone(self.counters.mean(("vworld", "rev")))

    def test_elapsed_is_measured_per_attempt_not_including_backoff(self):
        """백오프로 잠든 시간은 응답시간이 아니다."""
        self.call(Responder((500, None), (200, ok_body())), cost=0.25)
        samples = self.counters.latency[("vworld", "fwd")]
        self.assertEqual(len(samples), 2)
        for s in samples:
            self.assertAlmostEqual(s, 0.25, places=6)


# ─────────────────────────────────────────────────────────────────────────────
# 비밀 취급 · resume
# ─────────────────────────────────────────────────────────────────────────────

class TestKeyMasking(unittest.TestCase):
    """§6 — 로그에 URL 을 남길 때 `key=***`. 값이 새면 안 된다."""

    def test_masks_key_query_parameter(self):
        url = ("https://api.vworld.kr/req/address?service=address&request=getcoord"
               "&key=DEADBEEF-1234-5678-ABCD-0123456789AB&address=%EC%84%9C%EC%9A%B8")
        masked = measure.mask_key(url)
        self.assertNotIn("DEADBEEF", masked)
        self.assertIn("key=***", masked)
        self.assertIn("request=getcoord", masked)

    def test_masks_regardless_of_position(self):
        self.assertEqual(measure.mask_key("https://h/p?key=SECRET"), "https://h/p?key=***")
        self.assertIn("key=***", measure.mask_key("https://h/p?key=SECRET&a=1"))
        self.assertNotIn("SECRET", measure.mask_key("https://h/p?a=1&key=SECRET&b=2"))

    def test_leaves_urls_without_key_untouched(self):
        url = "http://127.0.0.1:8092/geocode?q=abc&limit=5"
        self.assertEqual(measure.mask_key(url), url)

    def test_does_not_maul_similarly_named_parameters(self):
        url = "https://h/p?monkey=1&keyword=2"
        self.assertEqual(measure.mask_key(url), url)


class TestResumeAccounting(unittest.TestCase):
    """2 차 검토 조건 6 — resume 손실 범위를 정직하게 기술한다.

    §3.4 의 "소모한 호출을 다시 쓰지 않는다" 는 **청크 단위로만** 참이다.
    청크 중간에 끊기면 그 청크의 완료분은 버려지고 최대 1,000 건을 다시 쓴다.
    그 재소모 건수를 세어 리포트에 싣는다.
    """

    def test_chunk_size_is_one_thousand(self):
        self.assertEqual(measure.CHUNK_SIZE, 1000)

    def test_resume_rewinds_to_chunk_boundary(self):
        for done, want_start, want_wasted in [
            (0, 0, 0),
            (1, 0, 1),
            (999, 0, 999),
            (1000, 1000, 0),
            (1500, 1000, 500),
            (11999, 11000, 999),
            (12000, 12000, 0),
        ]:
            start, wasted = measure.resume_start(done, chunk=measure.CHUNK_SIZE)
            self.assertEqual((start, wasted), (want_start, want_wasted), msg=done)

    def test_wasted_never_reaches_a_full_chunk(self):
        for done in range(0, 3000, 7):
            _, wasted = measure.resume_start(done, chunk=measure.CHUNK_SIZE)
            self.assertLess(wasted, measure.CHUNK_SIZE)

    def test_counter_records_rechunk_waste(self):
        counters = measure.Counters()
        counters.note_resume(done=1500, chunk=1000)
        self.assertEqual(counters.resume_wasted, 500)
        counters.note_resume(done=2400, chunk=1000)
        self.assertEqual(counters.resume_wasted, 900)


def clock_slept(clock):
    return clock.slept


if __name__ == "__main__":
    unittest.main(verbosity=2)
