#!/usr/bin/env python3
"""T046 §3.2 · §5 — 본 측정 루프. 호출 제어 + 청크 파이프라인.

이 파일은 두 층으로 나뉜다.

  ① **순수 로직** — 토큰 버킷·백오프·재시도·카운터·키 마스킹·resume 회계.
     네트워크도 시계도 주입받는다. `test_ratelimit.py` 가 전량 검정한다.
  ② **실호출부** — VWorld·우리 8092 호출, 오라클/심판 배치, 판정 append.
     ①의 계약 위에 얹는다.

## 왜 버킷 용량이 1 인가(§5 는 적지 않았다)

용량 8 이면 정지 상태에서 8 건이 한 순간에 터진 뒤 곧바로 초당 8 건이 이어져
`[0,1)` 창에 15 건이 들어간다 — 선언한 상한의 두 배다. 용량 1 은 매 호출을
1/8 초 간격으로 고르게 미는 순수 페이싱이고, 계획 §9 의 소요 산정
(`26,700/8 = 3,337.5 s`)도 정확히 이 모델을 전제한다.

## HTTP 429 는 즉시 중단이다(계획 본문에 없다)

계획은 body 의 `OVER_REQUEST_LIMIT` 만 명시했다. 429 를 5xx 처럼 재시도하면
한도를 넘긴 채로 세 번 더 두드리게 되므로 같은 취급으로 둔다. 보고서 ⑦ 대상.

## §3.3 — 응답 원문을 디스크에 쓰지 않는다

청크의 응답은 프로세스 메모리에만 두고, 판정이 끝나면 **판정 결과만** append
한다. 좌표·`text`·`structure` 원문은 verdict 파일에 들어가지 않는다.
예외는 `--diag N` 으로 켜는 진단 파일뿐이며 §3.3 (a)~(e) 를 따른다
(리포 밖 `~/geocode-build/t046/diag/`, 커밋 금지, 집계 후 삭제).

실행:
    /usr/bin/python3 scripts/t046/measure.py --sample A --limit 100 --tag warmup
"""
import argparse
import errno
import hashlib
import json
import math
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

__all__ = [
    "RATE_LIMIT_RPS", "CONCURRENCY", "TIMEOUT_S", "BUCKET_CAPACITY",
    "BACKOFF_SCHEDULE", "MAX_RETRIES", "JITTER_FRAC", "CHUNK_SIZE",
    "FATAL_ERROR_CODES", "TokenBucket", "backoff_delay", "Counters",
    "call_with_retry", "CallTimeout", "OverRequestLimit", "CallResult",
    "mask_key", "resume_start",
]

# ── §5 가 못박은 상수 ─────────────────────────────────────────────────
RATE_LIMIT_RPS = 8.0
CONCURRENCY = 4
TIMEOUT_S = 10.0
BUCKET_CAPACITY = 1
BACKOFF_SCHEDULE = (1.0, 2.0, 4.0)
MAX_RETRIES = 3
JITTER_FRAC = 0.2
CHUNK_SIZE = 1000
FATAL_ERROR_CODES = frozenset(("OVER_REQUEST_LIMIT",))

# ── §4 측정 상수 ──────────────────────────────────────────────────────
THRESHOLD_M = 100.0                       # 주 임계 T
SENSITIVITY_T = (25.0, 100.0, 1000.0)     # 민감도 분석용
OUR_BASE = os.environ.get("T046_OURS", "http://127.0.0.1:8092")
VWORLD_BASE = "https://api.vworld.kr/req/address"
KEY_PATH = os.path.expanduser("~/geocode-build/.secrets/vworld_apikey")
DEFAULT_ROOT = os.path.expanduser("~/geocode-build/t046")


class CallTimeout(Exception):
    """응답이 `TIMEOUT_S` 안에 오지 않았다. 재시도 대상."""


class OverRequestLimit(Exception):
    """한도초과. §5 — 즉시 **전체** 중단한다. 더 두드려봐야 한도만 깎는다."""


# ─────────────────────────────────────────────────────────────────────
# 토큰 버킷
# ─────────────────────────────────────────────────────────────────────
class TokenBucket(object):
    """초당 `rate` 로 고르게 미는 페이서. 스레드 안전.

    `capacity` 는 "미리 당겨 쓸 수 있는 토큰 수"다. 1 이면 유휴 시간이 아무리
    길어도 크레딧이 쌓이지 않는다 — 다음 허용 시각을 현재보다 과거로 내려두지
    않기 때문이다.

    대기 시간 계산은 락 안에서, 실제 수면은 락 **밖**에서 한다. 락을 쥔 채
    자면 동시성 4 가 직렬 1 이 된다.
    """

    __slots__ = ("rate", "capacity", "_interval", "_now", "_sleep",
                 "_lock", "_allow_at")

    def __init__(self, rate, capacity=BUCKET_CAPACITY, now=None, sleep=None):
        if rate <= 0:
            raise ValueError("rate 는 양수여야 한다: %r" % (rate,))
        self.rate = float(rate)
        self.capacity = max(int(capacity), 1)
        self._interval = 1.0 / self.rate
        self._now = now or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._allow_at = None

    def acquire(self):
        """다음 토큰이 설 때까지 기다린다. 첫 호출은 자지 않는다."""
        with self._lock:
            t = self._now()
            # 용량만큼만 과거를 인정한다 — 그 이전의 유휴는 크레딧이 되지 않는다.
            floor = t - (self.capacity - 1) * self._interval
            if self._allow_at is None or self._allow_at < floor:
                self._allow_at = floor
            start = self._allow_at if self._allow_at > t else t
            self._allow_at = start + self._interval
            wait = start - t
        if wait > 0:
            self._sleep(wait)


def backoff_delay(i, rng=None):
    """`BACKOFF_SCHEDULE[i]` 에 ±`JITTER_FRAC` 지터. `rng()=0.5` 면 지터 0."""
    r = (rng or random.random)()
    base = BACKOFF_SCHEDULE[i]
    return base * (1.0 + JITTER_FRAC * (2.0 * r - 1.0))


# ─────────────────────────────────────────────────────────────────────
# 카운터
# ─────────────────────────────────────────────────────────────────────
class Counters(object):
    """§5 '집계 대상 카운터 — 전부 리포트에 싣는다'.

    `calls` 는 **시도** 수다(재시도 포함) — 예산 소진의 척도이므로.
    `retried`·`gave_up`·`status` 는 **건** 단위다. 한 건이 세 번 자도 1 이다.
    두 축을 섞으면 "몇 건을 물었나"와 "몇 번을 던졌나"를 구분할 수 없다.
    """

    def __init__(self):
        self.status = defaultdict(int)        # (svc, dir, status) → 건
        self.error_codes = defaultdict(int)   # (svc, dir, code)  → 건
        self.calls = defaultdict(int)         # (svc, dir) → 시도
        self.retried = defaultdict(int)
        self.retried_ok = defaultdict(int)
        self.gave_up = defaultdict(int)
        self.network_fail = defaultdict(int)  # (svc, dir) → 시도
        self.latency = defaultdict(list)      # (svc, dir) → [초]
        self.over_limit = False
        self.resume_wasted = 0
        self._lock = threading.Lock()

    # -- 갱신 (스레드 안전) -----------------------------------------
    def _bump(self, table, key, n=1):
        with self._lock:
            table[key] += n

    def note_call(self, service, direction):
        self._bump(self.calls, (service, direction))

    def note_network_fail(self, service, direction):
        self._bump(self.network_fail, (service, direction))

    def note_latency(self, service, direction, seconds):
        self.latency[(service, direction)].append(seconds)

    def note_status(self, service, direction, status):
        self._bump(self.status, (service, direction, status))

    def note_error_code(self, service, direction, code):
        self._bump(self.error_codes, (service, direction, code))

    def note_retried(self, service, direction, ok):
        self._bump(self.retried, (service, direction))
        if ok:
            self._bump(self.retried_ok, (service, direction))

    def note_gave_up(self, service, direction):
        self._bump(self.gave_up, (service, direction))

    def note_resume(self, done, chunk):
        """조건 6(Minor) — 청크 중간 중단으로 다시 쓰게 된 호출 수를 누적한다."""
        _, wasted = resume_start(done, chunk)
        with self._lock:
            self.resume_wasted += wasted

    # -- 조회 --------------------------------------------------------
    def mean(self, key):
        vals = self.latency.get(key) or []
        if not vals:
            return None
        return sum(vals) / float(len(vals))

    def p95(self, key):
        """최근접 순위법 — `ceil(0.95 n)` 번째(1-based). 보간하지 않는다."""
        vals = sorted(self.latency.get(key) or [])
        if not vals:
            return None
        rank = int(math.ceil(0.95 * len(vals)))
        return vals[max(rank, 1) - 1]

    def gate_e1(self):
        """VWorld **순방향**이 `status != "OK"` 인 건. 외부기준이 없으면 비교 불가.

        F8 정정 — 이전 판은 방향을 가리지 않아 **역방향** 실패까지 더했다.
        본 측정에서 계수기가 19, 실제 E1 은 18 이었고 차이 1 은 역방향
        `NOT_FOUND` 였다. E1 은 순방향 게이트이므로 방향을 좁힌다.
        (보고 수치는 판정 레코드에서 도출했으므로 발표된 87 은 영향 없다.)
        """
        return sum(v for (svc, d, st), v in self.status.items()
                   if svc == "vworld" and d == "fwd" and st != "OK")

    def gate_e2(self):
        """우리 8092 가 5xx/타임아웃으로 끝난 건. 커버리지 결손과 구분한다."""
        return sum(v for (svc, _d), v in self.gave_up.items() if svc == "ours")

    def snapshot(self):
        """리포트용 직렬화. 키 튜플을 문자열로 편다."""
        def flat(table):
            return {"|".join(map(str, k)): v for k, v in sorted(table.items())}
        lat = {}
        for k in sorted(self.latency):
            lat["|".join(k)] = {"n": len(self.latency[k]),
                                "mean_s": self.mean(k), "p95_s": self.p95(k)}
        return {
            "calls": flat(self.calls),
            "status": flat(self.status),
            "error_codes": flat(self.error_codes),
            "retried": flat(self.retried),
            "retried_ok": flat(self.retried_ok),
            "gave_up": flat(self.gave_up),
            "network_fail": flat(self.network_fail),
            "latency": lat,
            "over_limit": self.over_limit,
            "resume_wasted": self.resume_wasted,
            "gate_e1": self.gate_e1(),
            "gate_e2": self.gate_e2(),
        }


class CallResult(object):
    """`call_with_retry` 의 결과. `body` 는 메모리 전용이다(§3.3)."""

    __slots__ = ("ok", "status", "attempts", "error_code", "body")

    def __init__(self, ok, status, attempts, error_code=None, body=None):
        self.ok = ok
        self.status = status
        self.attempts = attempts
        self.error_code = error_code
        self.body = body

    def __repr__(self):
        return ("CallResult(ok=%r, status=%r, attempts=%r, error_code=%r)"
                % (self.ok, self.status, self.attempts, self.error_code))


def _read_vworld_status(body):
    """`(status, error_code)`. 우리가 아는 형태가 아니면 `BAD_BODY` 다.

    형태를 못 알아본 것을 성공으로 흘리면 규칙 12(부재를 성공으로 세지 않는다)가
    무너진다.
    """
    resp = (body or {}).get("response")
    if not isinstance(resp, dict):
        return "BAD_BODY", None
    st = resp.get("status")
    if st == "ERROR":
        err = resp.get("error") or {}
        return "ERROR", err.get("code")
    if st in ("OK", "NOT_FOUND"):
        return st, None
    return "BAD_BODY", None


def call_with_retry(responder, service, direction, counters,
                    sleep=None, now=None, rng=None):
    """§5 — 네트워크·5xx·타임아웃만 재시도한다. `NOT_FOUND` 는 정상 응답이다.

    `responder()` 는 `(http_status, body)` 를 돌려주거나 예외를 던진다.
    """
    sleep = sleep or time.sleep
    now = now or time.monotonic
    attempts = 0
    ok = False
    status = "BAD_BODY"
    error_code = None
    body = None
    retryable = False

    while True:
        attempts += 1
        counters.note_call(service, direction)
        retryable = False
        t0 = now()
        try:
            item = responder()
        except CallTimeout:
            status = "TIMEOUT"
            retryable = True
        except OverRequestLimit:
            raise
        except OSError:
            # 백오프 수면은 응답시간이 아니다 — latency 를 남기지 않는다.
            counters.note_network_fail(service, direction)
            status = "NETWORK"
            retryable = True
        else:
            counters.note_latency(service, direction, now() - t0)
            http, body = item
            if http == 429:
                # 계획 외 결정: 한도초과의 HTTP 표현으로 본다(보고서 ⑦).
                counters.over_limit = True
                raise OverRequestLimit("HTTP 429")
            if 500 <= http < 600:
                status = "HTTP_%d" % http
                retryable = True
            elif http != 200:
                status = "HTTP_%d" % http
            elif service == "ours":
                # 우리 8092 응답에는 VWorld 의 `status` 필드가 없다(§1.6).
                status, ok = "OK", True
            else:
                status, error_code = _read_vworld_status(body)
                if error_code in FATAL_ERROR_CODES:
                    counters.over_limit = True
                    raise OverRequestLimit(error_code)
                ok = (status == "OK")

        if ok or not retryable or attempts > MAX_RETRIES:
            break
        sleep(backoff_delay(attempts - 1, rng=rng))

    if attempts > 1:
        counters.note_retried(service, direction, ok)
    if retryable and not ok:
        counters.note_gave_up(service, direction)
    counters.note_status(service, direction, status)
    if error_code:
        counters.note_error_code(service, direction, error_code)
    return CallResult(ok, status, attempts, error_code, body)


# ─────────────────────────────────────────────────────────────────────
# 비밀 취급 · resume 회계
# ─────────────────────────────────────────────────────────────────────
_KEY_RE = re.compile(r"(?<=[?&])key=[^&]*")


def mask_key(url):
    """§6 — 로그에 URL 을 남길 때 `key` **값만** 지운다.

    `monkey=1` 이나 `keyword=2` 를 건드리면 로그가 못 읽게 된다. 경계
    (`?` 또는 `&`) 를 붙여 정확히 `key` 파라미터만 잡는다.
    """
    return _KEY_RE.sub("key=***", url)


def resume_start(done, chunk=CHUNK_SIZE):
    """조건 6 — 청크 경계로 되감는다. `(시작 인덱스, 재소모 건수)`.

    §3.4 의 "소모한 호출을 다시 쓰지 않는다"는 **청크 단위로만** 참이다.
    청크 중간에 끊기면 그 청크의 완료분(최대 999 건)을 다시 쓴다.
    """
    start = (int(done) // int(chunk)) * int(chunk)
    return start, int(done) - start


# ═════════════════════════════════════════════════════════════════════
# 실호출부 — 여기부터는 네트워크와 DB 를 만진다.
# ═════════════════════════════════════════════════════════════════════

def load_api_key(path=KEY_PATH):
    """§6 — 키는 파일에서만 읽는다. 코드·로그·리포트에 값이 남으면 안 된다."""
    with open(path, "r") as fh:
        key = fh.read().strip()
    if not key:
        raise RuntimeError("VWorld API 키가 비어 있다: %s" % path)
    return key


def _http_responder(url, timeout=TIMEOUT_S):
    """`(http_status, body)` 를 돌려주는 무인자 호출가능.

    타임아웃은 `CallTimeout`, 그 밖의 전송 실패는 `OSError` 로 정규화한다 —
    `call_with_retry` 가 두 경우를 다르게 센다(§5).
    """
    def call():
        try:
            resp = urllib.request.urlopen(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                exc.read()
            except Exception:  # pragma: no cover — 본문을 못 읽어도 코드는 유효하다
                pass
            return exc.code, None
        except socket.timeout:
            raise CallTimeout(mask_key(url))
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise CallTimeout(mask_key(url))
            raise OSError(errno.ECONNRESET, str(exc.reason))
        try:
            raw = resp.read()
            code = resp.getcode()
        except socket.timeout:
            raise CallTimeout(mask_key(url))
        finally:
            resp.close()
        try:
            return code, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return code, None
    return call


def vworld_forward_url(key, query, atype):
    return VWORLD_BASE + "?" + urllib.parse.urlencode([
        ("service", "address"), ("request", "getcoord"), ("version", "2.0"),
        ("crs", "EPSG:4326"), ("address", query), ("type", atype),
        ("refine", "true"), ("simple", "false"), ("format", "json"),
        ("key", key),
    ])


def vworld_reverse_url(key, lon, lat, atype="both"):
    """역방향 `getAddress`. **`type=both`** 이 기본이다.

    A 단계는 `type=PARCEL` 로 고정돼 있어 도로명 축을 아예 받지 못했다 —
    §4.2 의 도로명·건물번호 지표가 비어 있던 직접 원인이다. `both` 는 한 번의
    호출로 `result[]` 에 `parcel` 과 `road` 를 함께 실어 준다(실측). 역방향은
    호출 한도가 없으므로(§1.1) 순방향 예산을 한 건도 쓰지 않는다.
    """
    return VWORLD_BASE + "?" + urllib.parse.urlencode([
        ("service", "address"), ("request", "getAddress"), ("version", "2.0"),
        ("crs", "EPSG:4326"), ("point", "%.7f,%.7f" % (lon, lat)),
        ("type", atype), ("format", "json"), ("key", key),
    ])


def ours_geocode_url(query, limit=5):
    return OUR_BASE + "/geocode?" + urllib.parse.urlencode(
        [("q", query), ("limit", limit)])


def ours_reverse_url(lon, lat, limit=2):
    return OUR_BASE + "/reverse?" + urllib.parse.urlencode(
        [("lon", "%.7f" % lon), ("lat", "%.7f" % lat), ("limit", limit)])


# ── 응답 해석 ────────────────────────────────────────────────────────
def parse_vworld_point(body):
    """순방향 좌표 `(lon, lat)`. 숫자가 아니면 `None` — 0.0 으로 뭉개지 않는다."""
    res = ((body or {}).get("response") or {}).get("result") or {}
    pt = res.get("point") or {}
    try:
        return float(pt["x"]), float(pt["y"])
    except (KeyError, TypeError, ValueError):
        return None


def parse_vworld_refined(body):
    """`(level4LC, text)`. 순방향 PARCEL 의 `level4LC` 는 19 자리 PNU 다(§1.3)."""
    refined = ((body or {}).get("response") or {}).get("refined") or {}
    st = refined.get("structure") or {}
    return st.get("level4LC"), refined.get("text")


def parse_vworld_reverse(body):
    """역방향 **지번 항목**의 `(level4LC, level5, text)`. `level4LC` 는 10 자리다.

    `result[0]` 을 쓰지 않는다. `type=both` 에서는 항목 순서가 계약이 아니고,
    도로명만 오는 지점도 있다 — 첫 항목을 지번으로 가정하면 도로명코드
    7 자리를 법정동코드로 읽는다. 선택은 `revmetrics` 가 `type` 으로 한다.
    """
    from revmetrics import split_reverse_entries

    entry = split_reverse_entries(body)["parcel"]
    if entry is None:
        return None, None, None
    st = entry.get("structure") or {}
    return st.get("level4LC"), st.get("level5"), entry.get("text")


def parse_ours(body):
    """우리 8092 순방향 → `(addr 후보 리스트, kind≠addr 개수)`.

    §4.1 — 엄격 응답률의 분자는 `kind='addr'` 뿐이다. 카테고리 폴백을
    응답으로 세면 "찾았다"가 부풀려진다(그 차이가 F2 다).
    """
    results = (body or {}).get("results")
    if not isinstance(results, list):
        return [], 0
    addrs = [r for r in results if isinstance(r, dict) and r.get("kind") == "addr"]
    return addrs, len(results) - len(addrs)


def cand_lonlat(cand):
    try:
        return float(cand["lon"]), float(cand["lat"])
    except (KeyError, TypeError, ValueError):
        return None


def cand_structure(cand):
    return ((cand or {}).get("address") or {}).get("structure") or {}


# ── 관측 조립 ────────────────────────────────────────────────────────
class Fetcher(object):
    """청크 1 건분 원격 호출(§3.2 의 1~3 단계). 결과는 **메모리에만** 둔다."""

    def __init__(self, key, counters, bucket, timeout=TIMEOUT_S, reverse=True):
        self.key = key
        self.counters = counters
        self.bucket = bucket
        self.timeout = timeout
        self.reverse = reverse

    def _vworld(self, url, direction):
        self.bucket.acquire()          # 8 req/s 는 VWorld 에만 건다
        return call_with_retry(_http_responder(url, self.timeout),
                               service="vworld", direction=direction,
                               counters=self.counters)

    def _ours(self, url, direction):
        return call_with_retry(_http_responder(url, self.timeout),
                               service="ours", direction=direction,
                               counters=self.counters)

    def fetch(self, rec):
        """표본 1 건 → 원재료 dict. 분류는 하지 않는다."""
        from geodist import distance_lonlat

        layer = rec["layer"]
        atype = "PARCEL" if layer == "jibun" else "ROAD"
        out = {
            "sid": rec["sid"], "layer": layer, "stratum": rec["stratum"],
            "pnu": rec.get("pnu"), "bm25": rec.get("bm25"),
            "san_query": bool(rec.get("san")),
            "bcode_sample": rec.get("bcode"),
            "t": time.time(),
        }

        # 1) VWorld 순방향 + 우리 순방향
        fv = self._vworld(vworld_forward_url(self.key, rec["query"], atype), "fwd")
        fo = self._ours(ours_geocode_url(rec["query"], 5), "fwd")

        out["v_status"] = fv.status
        out["e1"] = not fv.ok
        # E2 는 "우리 서버가 죽었다"만이다. 200 에 빈 결과는 커버리지 결손이지 장애가 아니다.
        out["e2"] = (not fo.ok) and fv.ok

        v_pt = parse_vworld_point(fv.body) if fv.ok else None
        v_lc, v_text = parse_vworld_refined(fv.body) if fv.ok else (None, None)
        out["v_level4lc"] = v_lc
        out["v_text"] = v_text
        out["v_pt"] = v_pt

        addrs, nonaddr = parse_ours(fo.body) if fo.ok else ([], 0)
        out["our_addr_count"] = len(addrs)
        out["nonaddr_count"] = nonaddr

        top5 = addrs[:5]
        pts = [cand_lonlat(c) for c in top5]
        out["m_pt"] = pts[0] if pts else None
        out["m5"] = [{"kind": c.get("kind"), "source": c.get("source"), "pt": p}
                     for c, p in zip(top5, pts)]

        if v_pt is not None and pts:
            dists = [distance_lonlat(v_pt, p) if p is not None else None for p in pts]
            valid = [d for d in dists if d is not None]
            out["d_top1"] = dists[0]
            out["d_min5"] = min(valid) if valid else None
            out["top1_is_nearest"] = (
                dists[0] is not None and valid and dists[0] <= min(valid) + 1e-9)
        else:
            out["d_top1"] = None
            out["d_min5"] = None
            out["top1_is_nearest"] = True

        st0 = cand_structure(top5[0]) if top5 else {}
        out["bcode_ours"] = st0.get("b_code")
        out["san_ours"] = bool(st0.get("san"))
        out["source"] = (top5[0] or {}).get("source") if top5 else None

        # 2) VWorld 가 OK 일 때만 역방향(§3.2). 표본 B 는 역방향이 없다.
        out["rev_v_status"] = None
        out["rev_ours_ok"] = None
        out["rev_dist_m"] = None
        out["rev_v_level4lc"] = None
        out["rev_v_level5"] = None
        out["rev_v_ri"] = None
        out["rev_ours_bcode"] = None
        out["rev_ours_ri"] = None
        out["rev_ours_source"] = None
        if self.reverse and v_pt is not None:
            rv = self._vworld(vworld_reverse_url(self.key, v_pt[0], v_pt[1]), "rev")
            out["rev_v_status"] = rv.status
            if rv.ok:
                from revmetrics import split_reverse_entries

                lc, lv5, txt = parse_vworld_reverse(rv.body)
                out["rev_v_level4lc"] = lc
                out["rev_v_level5"] = lv5
                # `level4L` 은 `parse_vworld_reverse` 반환값에 없어 여기서만
                # 꺼낸다. **지번 항목에서** 꺼내야 한다 — `result[0]` 은
                # `type=both` 에서 도로명일 수 있고, 그 `level4L` 은 도로명이다.
                _p = split_reverse_entries(rv.body)["parcel"] or {}
                _l4l = (_p.get("structure") or {}).get("level4L")
                out["rev_v_ri"] = _vw_reverse_ri(txt, _l4l)
            ro = self._ours(ours_reverse_url(v_pt[0], v_pt[1], 2), "rev")
            out["rev_ours_ok"] = bool(ro.ok)
            if ro.ok:
                out["rev_dist_m"] = _nearest_dist(ro.body)
                out["rev_ours_bcode"] = _reverse_bcode(ro.body)
                out["rev_ours_ri"] = _reverse_ri(ro.body)
                out["rev_ours_source"] = _reverse_source(ro.body)

        out["bcode_vw"] = _pick_bcode_vw(v_lc, out["rev_v_level4lc"])
        out["norm_applied"] = _norm_applied(out, rec)
        return out


def _nearest_dist(body):
    near = (body or {}).get("nearest") or []
    if not near:
        return None
    try:
        return float(near[0].get("dist_m"))
    except (TypeError, ValueError):
        return None


def _reverse_structure(body):
    """우리 `/reverse` 의 `address.structure`(§4.2 가 지정한 비교 대상).

    스키마 v2 수선. v1 은 `body["results"][0]` 를 읽었으나 `/reverse` 응답에
    `results` 키가 없어 전건 `None` 이었다 — 순방향 `/geocode` 의 응답 형태를
    역방향에 그대로 적용한 오독이다. 실측 최상위 키는
    `address / address_source / areas / contract_version / lat / lon / nearest`.
    """
    return ((body or {}).get("address") or {}).get("structure") or {}


def _reverse_bcode(body):
    return _reverse_structure(body).get("b_code")


def _reverse_ri(body):
    """우리 역방향 리 **문자열**. 비어 있으면 `None`(부재는 성공이 아니다)."""
    v = _reverse_structure(body).get("ri")
    v = ("" if v is None else str(v)).strip()
    return v or None


def _reverse_source(body):
    """`address_source` — 합성(`knn`) 과 실주소(`pip_key`) 를 가르는 축(M8)."""
    v = (body or {}).get("address_source")
    return str(v) if v else None


# VWorld 역방향 `structure` 에는 리 명칭 필드가 없다(실측: `level4L` 은 읍면동,
# `level5` 는 지번). 리 명칭은 `text` 에만 실리므로 계획 §4.2 대로 파싱한다.
# 규칙: 마지막 토큰은 지번이고, 그 앞 토큰이 `리` 로 끝나면서 `level4L`(읍면동)
# 과 다르면 리다. 도시 지점은 앞 토큰이 `…로/…동` 이라 자연히 `None` 이 된다.
def _vw_reverse_ri(text, level4l):
    toks = (text or "").split()
    if len(toks) < 2:
        return None
    cand = toks[-2]
    if not cand.endswith("리"):
        return None
    if level4l and cand == str(level4l).strip():
        return None
    return cand


def _pick_bcode_vw(fwd_lc, rev_lc):
    """법정동코드 10 자리. 순방향 `level4LC` 는 19 자리 PNU 라 앞 10 만 쓴다(§1.3)."""
    for lc in (fwd_lc, rev_lc):
        s = ("" if lc is None else str(lc)).strip()
        if len(s) >= 10 and s[:10].isdigit():
            return s[:10]
    return None


def _norm_applied(obs, rec):
    """F7 — 정규화가 **판정을 살린** 건.

    계획은 F7 을 "정규화가 값을 바꿨다"로만 적었다. 문자열이 바뀐 전건을 세면
    역방향 `level5` 가 지목 접미(`"산102임"`)를 늘 달고 오므로 거의 전건이
    True 가 되어 플래그가 아무것도 말하지 않는다. 그래서 **엄격 비교는 실패인데
    정규화 비교는 성공한 축이 하나라도 있는가**로 좁힌다(보고서 ⑦).
    """
    from normalize import jibun_match, parse_jibun

    lv5 = obs.get("rev_v_level5")
    if not lv5:
        return False
    ji_main, ji_sub = rec.get("ji_main"), rec.get("ji_sub")
    if ji_main is None:
        return False
    ours = "%d" % ji_main if not ji_sub else "%d-%d" % (ji_main, ji_sub)
    if rec.get("san"):
        ours = "산 " + ours
    return str(lv5).strip() != ours and jibun_match(lv5, ours) and bool(parse_jibun(lv5))


# ── 오라클·심판 배치(§3.2 의 4 단계) ─────────────────────────────────
def _within(d, t):
    return d is not None and d <= t


def _needs_referee(obs, t):
    """C 군 후보 — 상위 5 가 전부 임계 밖. 여기서만 심판을 부른다."""
    if obs["e1"] or obs["e2"] or obs["our_addr_count"] == 0:
        return False
    return not _within(obs["d_top1"], t) and not _within(obs["d_min5"], t)


def annotate_chunk(rows, orc, t=THRESHOLD_M):
    """청크 전건에 오라클·본번근사·심판을 붙인다. DB 왕복은 청크당 몇 회뿐이다."""
    from geodist import distance_lonlat
    from utmk import utmk_to_wgs84

    jib = {o["sid"]: o["pnu"] for o in rows if o["layer"] == "jibun" and o["pnu"]}
    road = {o["sid"]: (o["bm25"], o["pnu"])
            for o in rows if o["layer"] == "road" and o["pnu"] and o["bm25"]}
    o_j = orc.jibun_batch(jib) if jib else {}
    o_r = orc.road_batch(road) if road else {}
    apx = orc.apx_batch(jib) if jib else {}

    # 심판 — 지번층은 필지 폴리곤 PIP, 도로명층은 표본에 실린 기준점과의 거리다.
    ref_keys = {}
    for o in rows:
        if not _needs_referee(o, t) or o["layer"] != "jibun" or not o["pnu"]:
            continue
        if o.get("v_pt"):
            ref_keys["%d:v" % o["sid"]] = (o["pnu"], o["v_pt"][0], o["v_pt"][1])
        if o.get("m_pt"):
            ref_keys["%d:m" % o["sid"]] = (o["pnu"], o["m_pt"][0], o["m_pt"][1])
    ref = orc.referee_parcel_batch(ref_keys) if ref_keys else {}

    for o in rows:
        o["oracle"] = o_j.get(o["sid"]) if o["layer"] == "jibun" else o_r.get(o["sid"])
        if o["oracle"] is None:
            o["oracle"] = "N"
        # 조건 1 — 이 건의 오라클이 시도코드 12 완화로 뒤집혔는가. 총계
        # (`relax12_hits`)만으로는 §8-10 의 엄격 수치를 재구성할 수 없다.
        o["relax12_used"] = o["sid"] in orc.relax12_keys
        o["o_apx"] = bool(apx.get(o["sid"], False))
        o["T"] = t
        o["r_v"] = o["r_m"] = None
        if not _needs_referee(o, t):
            continue
        if o["layer"] == "jibun":
            o["r_v"] = ref.get("%d:v" % o["sid"])
            o["r_m"] = ref.get("%d:m" % o["sid"])
        else:
            anchors = o.get("anchors") or []
            o["r_v"] = _road_referee(o.get("v_pt"), anchors, t, distance_lonlat)
            o["r_m"] = _road_referee(o.get("m_pt"), anchors, t, distance_lonlat)
        _ = utmk_to_wgs84  # 앵커 변환은 load_sample 에서 끝났다
    return rows


def _road_referee(pt, anchors, t, dist):
    """도로명 심판 — `min(d(X, 출입구), d(X, 건물중심)) ≤ T`.

    기준점이 하나도 없으면 `None`(자료 부재)이다. `False` 로 뭉개면 분류 10 이
    "판정할 자료가 없었다"를 삼킨다(조건 4).
    """
    if pt is None or not anchors:
        return None
    return min(dist(pt, a) for a in anchors) <= t


# ── 표본 읽기 ────────────────────────────────────────────────────────
def load_sample(path, limit=None):
    """표본 JSONL → 레코드 리스트. 도로명 기준점은 여기서 WGS84 로 옮긴다."""
    from utmk import utmk_to_wgs84

    out = []
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("layer") == "road":
                anchors = []
                for field in ("entrance_5179", "center_5179"):
                    xy = rec.get(field)
                    if xy:
                        anchors.append(utmk_to_wgs84(float(xy[0]), float(xy[1])))
                rec["anchors"] = anchors
            out.append(rec)
            if limit and len(out) >= limit:
                break
    return out


# ── 판정 기록(§3.2 의 5~6 단계) ──────────────────────────────────────
# 판정 레코드 스키마 판. v1 = A 단계 원본, v2 = 역방향 주소축 수선(§4.2).
# v2 에서 **추가만** 했다 — v1 필드의 이름·의미·산출식은 하나도 바꾸지 않았으므로
# 두 판을 같은 분모로 합산해도 되는 지표와 v2 구간에서만 낼 수 있는 지표가 갈린다.
VERDICT_SCHEMA = 2

VERDICT_FIELDS = (
    "schema",
    "sid", "layer", "stratum", "cls", "flags", "gate",
    "cls_relaxed", "flags_relaxed",
    "v_status", "our_addr_count", "nonaddr_count",
    "d_top1", "d_min5", "top1_is_nearest",
    "oracle", "o_apx", "r_v", "r_m", "T",
    "bcode_eq", "bcode_eq_relaxed", "san_eq", "source",
    "rev_v_status", "rev_ours_ok", "rev_dist_m", "rev_bcode_eq",
    # ── v2 추가: 역방향 주소축(§4.2) ──────────────────────────────
    "rev_bcode_eq_relaxed",
    "rev_eq_sido", "rev_eq_sido_relaxed", "rev_eq_sgg", "rev_eq_emd",
    "rev_ri_code_eq", "rev_ri_code_ours", "rev_ri_code_vw",
    "rev_ri_filled_ours", "rev_ri_filled_vw", "rev_ri_str_eq",
    "rev_ri_conflict_ours", "rev_ri_conflict_vw",
    "rev_address_source",
    "norm_applied", "relax12_used", "t",
)


def to_verdict(obs, verdict, verdict_relaxed):
    """§3.3 — 거리·불리언·`status`·분류 코드·플래그·시각만 남긴다.

    좌표와 `text`·`structure` 원문은 여기서 떨어져 나간다. `stratum` 은 층
    이름이지 주소가 아니므로 집계를 위해 싣는다.
    """
    from normalize import bcode_match, sido_code_match, strip_spaces

    a, b = obs.get("bcode_ours"), obs.get("bcode_vw")
    rev_ours = obs.get("rev_ours_bcode")
    rev_vw = obs.get("rev_v_level4lc")

    # ── v2 역방향 주소축(§4.2). 양쪽 코드가 10 자리로 있을 때만 판정한다.
    # 한쪽이라도 없으면 `None`(판정 불가)이다 — 부재를 일치로 세지 않는다.
    _ro, _rv = strip_spaces(rev_ours), strip_spaces(rev_vw)
    _pair = len(_ro) == 10 and len(_rv) == 10

    def _pfx_eq(n):
        return (_ro[:n] == _rv[:n]) if _pair else None

    _ri_ours = obs.get("rev_ours_ri")
    _ri_vw = obs.get("rev_v_ri")
    # 리 코드 = 법정동코드 끝 2 자리. `00` 은 리 없음이다.
    _lo = _ro[8:10] if _pair else None
    _lv = _rv[8:10] if _pair else None
    _code_ri_ours = (_lo != "00") if _lo is not None else None
    _code_ri_vw = (_lv != "00") if _lv is not None else None
    # 역방향 호출이 성공한 건에서만 채움 여부를 묻는다.
    _fill_ours = bool(_ri_ours) if obs.get("rev_ours_ok") else None
    _fill_vw = bool(_ri_vw) if (obs.get("rev_v_status") == "OK") else None
    rec = {
        "sid": obs["sid"], "layer": obs["layer"], "stratum": obs["stratum"],
        "cls": verdict.cls, "flags": list(verdict.flags), "gate": verdict.gate,
        "cls_relaxed": verdict_relaxed.cls,
        "flags_relaxed": list(verdict_relaxed.flags),
        "v_status": obs.get("v_status"),
        "our_addr_count": obs["our_addr_count"],
        "nonaddr_count": obs["nonaddr_count"],
        "d_top1": obs["d_top1"], "d_min5": obs["d_min5"],
        "top1_is_nearest": bool(obs["top1_is_nearest"]),
        "oracle": obs["oracle"], "o_apx": obs["o_apx"],
        "r_v": obs["r_v"], "r_m": obs["r_m"], "T": obs["T"],
        "bcode_eq": bcode_match(a, b) if (a and b) else None,
        "bcode_eq_relaxed": bcode_match(a, b, relax12=True) if (a and b) else None,
        "san_eq": (obs["san_query"] == obs["san_ours"]) if obs["our_addr_count"] else None,
        "source": obs.get("source"),
        "rev_v_status": obs.get("rev_v_status"),
        "rev_ours_ok": obs.get("rev_ours_ok"),
        "rev_dist_m": obs.get("rev_dist_m"),
        "rev_bcode_eq": (bcode_match(rev_ours, rev_vw)
                         if (rev_ours and rev_vw) else None),
        "schema": VERDICT_SCHEMA,
        "rev_bcode_eq_relaxed": (bcode_match(rev_ours, rev_vw, relax12=True)
                                 if (rev_ours and rev_vw) else None),
        "rev_eq_sido": (sido_code_match(_ro[:2], _rv[:2]) if _pair else None),
        "rev_eq_sido_relaxed": (sido_code_match(_ro[:2], _rv[:2], relax12=True)
                                if _pair else None),
        "rev_eq_sgg": _pfx_eq(5),
        "rev_eq_emd": _pfx_eq(8),
        "rev_ri_code_eq": ((_lo == _lv) if _pair else None),
        "rev_ri_code_ours": _code_ri_ours,
        "rev_ri_code_vw": _code_ri_vw,
        "rev_ri_filled_ours": _fill_ours,
        "rev_ri_filled_vw": _fill_vw,
        # 리 문자열 일치는 양쪽이 모두 채워졌을 때만 묻는다. 둘 다 비어 있는 것을
        # "일치"로 세면 §4.2 의 '채움률이 아니라 정확도' 요구를 정면으로 어긴다.
        "rev_ri_str_eq": ((strip_spaces(_ri_ours) == strip_spaces(_ri_vw))
                          if (_ri_ours and _ri_vw) else None),
        # 코드-문자열 상호 불일치(T029 결함의 표본 내 재현율).
        "rev_ri_conflict_ours": ((_code_ri_ours != _fill_ours)
                                 if (_code_ri_ours is not None
                                     and _fill_ours is not None) else None),
        "rev_ri_conflict_vw": ((_code_ri_vw != _fill_vw)
                               if (_code_ri_vw is not None
                                   and _fill_vw is not None) else None),
        "rev_address_source": obs.get("rev_ours_source"),
        "norm_applied": bool(obs.get("norm_applied")),
        "relax12_used": bool(obs.get("relax12_used")),
        "t": round(obs["t"], 3),
    }
    return {k: rec[k] for k in VERDICT_FIELDS}


def to_diag(obs, verdict, query):
    """§3.3 진단 — 실패 건 한정. 질의 원문 대신 SHA-256 앞 12 자만 싣는다."""
    return {
        "sid": obs["sid"],
        "qhash": hashlib.sha256(query.encode("utf-8")).hexdigest()[:12],
        "layer": obs["layer"], "stratum": obs["stratum"],
        "v_status": obs.get("v_status"), "v_level4lc": obs.get("v_level4lc"),
        "d_top1": obs["d_top1"], "d_min5": obs["d_min5"],
        "m5": obs.get("m5"),
        "oracle": obs["oracle"], "o_apx": obs["o_apx"],
        "r_v": obs["r_v"], "r_m": obs["r_m"],
        "referee": "parcel_pip" if obs["layer"] == "jibun" else "road_anchor",
        "cls": verdict.cls, "flags": list(verdict.flags), "gate": verdict.gate,
        "t": round(obs["t"], 3),
    }


# ── 측정 창(§3.5) ────────────────────────────────────────────────────
def fetch_health(base):
    """`/health` 를 읽는다 → dict(파싱 성공) 또는 `"ERR:..."` 문자열.

    ★ **503 본문을 반드시 읽는다.** T047 §3.2 의 `/health` 는 DB 가 죽어도 사전 상태를
      `dict` 에 담아 내보내는데, 그 응답 코드가 **503** 이다. `urlopen` 은 503 에서
      `HTTPError` 를 던지므로 예외만 잡고 넘어가면 정작 필요한 `dict` 를 통째로 버린다 —
      "탐지 수단이 탐지 대상 상황에서 침묵한다"는 이번 사고의 형상이 한 겹 뒤에서 재현된다.
      `HTTPError` 는 그 자체가 파일 객체이므로 본문을 그대로 읽을 수 있다.
    """
    try:
        with urllib.request.urlopen(base + "/health", timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return "ERR:%s" % exc
    except Exception as exc:
        return "ERR:%s" % exc


GEOCODE_CONTAINER_DEFAULT = "server-geocode-pg-1"


def resolve_geocode_container(base, sh):
    """측정 대상(`base`)을 **실제로 서빙하는** 컨테이너 이름을 유도한다 → `(이름, 유도근거)`.

    ★ 왜 필요한가 (T047 검수 6-2). 종전에는 컨테이너명이 `"server-geocode-pg-1"` 로 **고정**인데
      `/health` 만 `OUR_BASE` 를 따랐다. 그래서 별도 프로브 포트(예: 8093)를 측정하면 무결성
      기록이 **엉뚱한 컨테이너**의 id·md5·마운트를 적으면서 동시에 프로브의 사전 상태를 적었다.
      즉 감사 기록이 측정 대상과 다른 판을 기술한다 — T047 이 막으려는 사고(런타임 상태와 기록의
      불일치)와 정확히 같은 계열이라, 기록을 믿고 판을 확정하면 다시 오염된다.

    유도는 **포트 발행**으로 한다(`docker ps --filter publish=<port>`). 이름 규칙이나 이미지에
    기대지 않는다.

    폴백 (전부 근거 문자열로 남긴다 — 조용히 기본값으로 떨어지지 않는다):
      · 포트를 못 뽑음 / docker 실패 / 매칭 0 건  → 기본값, 근거 `fallback:*`
      · 매칭 2 건 이상(포트 중복 발행)            → 기본값, 근거 `fallback:ambiguous(...)`
    """
    port = None
    m = re.search(r":(\d+)", base or "")
    if m:
        port = m.group(1)
    if not port:
        return GEOCODE_CONTAINER_DEFAULT, "fallback:no-port-in-base"
    out = sh(["docker_placeholder", "ps", "--filter", "publish=" + port,
              "--format", "{{.Names}}"])
    if out.startswith("ERR:"):
        return GEOCODE_CONTAINER_DEFAULT, "fallback:docker-error"
    names = [n.strip() for n in out.splitlines() if n.strip()]
    if len(names) == 1:
        return names[0], "port:" + port
    if not names:
        return GEOCODE_CONTAINER_DEFAULT, "fallback:no-container-on-port-" + port
    return GEOCODE_CONTAINER_DEFAULT, "fallback:ambiguous(%s)" % ",".join(names[:4])


def probe_window():
    """§3.5 — 컨테이너 ID·마운트·md5(양쪽)·`/health` 를 찍는다.

    8092 에는 healthcheck 가 없다. 낡은 코드를 물린 컨테이너를 측정한 사고가
    있었으므로 시작과 끝에서 같은 4 개를 대조한다.

    컨테이너는 `OUR_BASE` 에서 유도한다(`resolve_geocode_container`) — 고정값을 쓰면
    프로브 포트를 잴 때 기록과 측정 대상이 어긋난다(T047 검수 6-2).
    """
    import subprocess
    import pgprobe

    def sh(args):
        try:
            p = subprocess.run(args, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=30)
            return p.stdout.decode("utf-8", "replace").strip()
        except Exception as exc:      # pragma: no cover — 진단용, 실패해도 계속
            return "ERR:%s" % exc

    docker = pgprobe.DOCKER

    def dsh(args):
        return sh([docker] + list(args[1:]))      # 첫 항목(placeholder)을 실제 docker 로 치환

    win = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    cname, cwhy = resolve_geocode_container(OUR_BASE, dsh)
    # 어느 컨테이너를 왜 골랐는지도 기록에 남긴다 — 사후에 판을 재구성할 수 있어야 한다.
    win["geocode_base"] = OUR_BASE
    win["geocode_container"] = cname
    win["geocode_container_resolved_by"] = cwhy
    win["geocode_id"] = sh([docker, "inspect", "-f", "{{.Id}}", cname])[:12]
    win["geocode_started"] = sh([docker, "inspect", "-f", "{{.State.StartedAt}}", cname])
    src = sh([docker, "inspect", "-f",
              '{{range .Mounts}}{{if eq .Destination "/app/geocode-api-pg.py"}}'
              '{{.Source}}{{end}}{{end}}', cname])
    win["mount_source"] = src
    win["md5_in_container"] = sh([docker, "exec", cname,
                                  "md5sum", "/app/geocode-api-pg.py"]).split()[:1]
    if src and os.path.exists(src):
        with open(src, "rb") as fh:
            win["md5_on_host"] = hashlib.md5(fh.read()).hexdigest()
    else:
        win["md5_on_host"] = None
    win["health"] = fetch_health(OUR_BASE)
    win["pg_id"] = sh([docker, "inspect", "-f", "{{.Id}}", pgprobe.CONTAINER])[:12]
    win["pg_started"] = sh([docker, "inspect", "-f", "{{.State.StartedAt}}",
                            pgprobe.CONTAINER])
    return win


# ── T047 P0 §3.3 — 사전 상태 게이트 ──────────────────────────────────
#
# T046 본 측정 12,000 건은 리 사전·안 A 시도 치환·T026 인천 치환이 **전부 꺼진**
# 프로세스를 상대로 완주했다. `_boot_check` 의 `with POOL.connection()` 이 30 초
# 타임아웃으로 실패해 그 블록 안 로더 5 개가 통째로 호출되지 않은 상태였다
# (5 회 기동 중 1 회 = 20%). md5 3 자 대조로는 잡히지 않는다 — 코드도 데이터도
# 정본이었고 **런타임 상태만** 달랐다.
#
# 경고 신호는 실제로 있었다: `run_main.json` 의 `window_start.health` 가
# `"ERR:HTTP Error 503"`. 그런데 그것이 **경고에 그쳐** 측정이 진행됐다.
# 그래서 여기서는 경고가 아니라 **중단**한다.

def _health_dict(win):
    """측정창 스냅샷에서 `/health` 의 dict 를 꺼낸다. 못 꺼내면 None."""
    h = (win or {}).get("health")
    if not isinstance(h, dict):
        return None                      # "ERR:..." 문자열이거나 아예 없음
    d = h.get("dict")
    return d if isinstance(d, dict) else None


def dict_gate(win):
    """측정 시작 가부를 판정한다 → `(ok, reason)`.

    차단(=`ok False`) 조건은 둘뿐이고 둘 다 **"확인되지 않았다"** 는 뜻이다.
      · `/health` 가 응답하지 않았다(`ERR:*`) — 모르면 재지 않는다
      · `dict.lawd_ri != "present"` — 사전이 안 걸린 채로 재면 그 수치는 폐기 대상이다

    `sido_remap`/`sgg_remap` 이 `off` 인 것은 **차단하지 않는다.** 두 치환표는 R-9 상
    정당하게 부재할 수 있기 때문이다(부재 시 46/29·중구 유지가 설계된 동작). 다만 이번
    사고에서는 다섯이 함께 꺼졌으므로 사유 문자열에 반드시 실어 기록에 남긴다 —
    부팅 경합이면 `lawd_ri` 가 `unknown` 이 되어 위 조건이 먼저 잡는다.
    """
    h = (win or {}).get("health")
    if not isinstance(h, dict):
        return False, ("/health 응답 없음(%s) — 사전 상태를 확인할 수 없다. "
                       "T046 은 바로 이 상태(ERR:HTTP Error 503)에서 완주해 오염됐다."
                       % (str(h)[:80] if h else "없음"))
    d = _health_dict(win)
    if d is None:
        return False, ("/health 에 dict 가 없다 — 사전 상태를 노출하지 않는 구판 서버다. "
                       "T047 §3.2 판을 올린 뒤 다시 시작하라.")
    state = d.get("lawd_ri")
    notes = []
    if d.get("sido_remap") == "off":
        notes.append("sido_remap=off(안 A 시도 치환 비활성)")
    if d.get("sgg_remap") == "off":
        notes.append("sgg_remap=off(T026 인천 치환 비활성)")
    tail = ("  ※ " + " · ".join(notes)) if notes else ""
    if state != "present":
        extra = ""
        if state == "unknown":
            extra = (" — 부팅 시 DB 연결 실패로 사전 로더 5 종이 통째로 건너뛰어졌다는 뜻이다. "
                     "컨테이너를 재기동하고 `docker logs` 에서 lawd_ri: present 를 확인하라.")
        elif state == "absent":
            extra = " — lawd_ri 테이블이 실제로 없다. 사전을 적재한 뒤 재시도하라."
        return False, "dict.lawd_ri=%r (present 아님)%s%s" % (state, extra, tail)
    return True, ("dict.lawd_ri=present ri_emds=%s" % d.get("ri_emds")) + tail


def dict_same(win_a, win_b):
    """측정 창 양끝의 사전 상태가 같은가 — 측정 도중 재기동 탐지(R7).

    한쪽이라도 `/health` 를 못 읽었으면 **다르다고 본다**(모르는 것을 같다고 하지 않는다).
    """
    a, b = _health_dict(win_a), _health_dict(win_b)
    if a is None or b is None:
        return False
    return a == b


# ── 역방향 재측정(F1) ────────────────────────────────────────────────
# 질의 좌표 기준. **원본 판정에 쓴 좌표는 재현할 수 없다.**
#
#   원본 기준 = VWorld 순방향이 돌려준 좌표. 그 좌표는 판정 레코드에 남지
#   않았고(§3.3 이 좌표를 의도적으로 버린다) `--diag` 도 쓰이지 않아 진단
#   파일 자체가 없다. 순방향 재호출은 예산 보호로 금지다. 그래서 태스크가
#   허용한 "표본에서 재유도"를 택하되, **어느 기준도 중립이 아니라는 사실을
#   숨기지 않는다.** 두 기준을 모두 돌려 참값을 사이에 가둔다.
#
#   src  표본 자체에서 유도. 도로명은 원천의 출입구 좌표(없으면 건물중심),
#        지번은 필지 폴리곤 대표점(`ST_PointOnSurface`).
#        · 도로명: **가장 중립적**이다. 점을 만든 것은 원천이지 어느
#          지오코더도 아니다. 원본 기준(VWorld 순방향 출력)보다 낫다 —
#          그 기준은 VWorld 자기 출력이라 VWorld 쪽에 자기참조였다.
#        · 지번: **우리 쪽에 자기참조다.** 우리 `/reverse` 의 `pip_key` 경로가
#          바로 그 필지 폴리곤을 다시 무므로 사실상 항진명제다. 상한으로만
#          읽어야 한다.
#   fwd  우리 8092 순방향 top-1. 위 항진명제는 없다. 다만 **보수적 기준이라고
#        부르지 않는다** — 순방향이 틀리면 점이 옮겨갈 뿐 양쪽이 여전히 같은
#        점을 설명하므로, 순방향 오차가 역방향 불일치로 자동 전이되지 않는다.
#        이 기준이 재는 것은 **왕복 일관성**이고 편향 방향은 미확인이다.
#
# 두 기준 모두 원본과 다르다. 따라서 여기 수치는 이미 발표한 `rev_*` 수치와
# **직접 비교할 수 없다.** 보고서에 그렇게 적는다.
REV2_BASES = ("src", "fwd")
REV2_SCHEMA = 1

REV2_FIELDS = (
    "rev_schema", "sid", "layer", "stratum", "urban", "basis",
    "basis_ok", "skip",
    "rev_v_status", "vw_has_parcel", "vw_has_road",
    "ours_ok", "address_source", "rev_dist_m",
    "axes", "ri", "t",
)


class Rev2Fetcher(object):
    """역방향 전용 호출기. **VWorld 순방향은 한 번도 부르지 않는다**(예산 보호).

    역방향은 한도가 없지만 8 req/s 와 백오프는 그대로 지킨다 — 한도가 없다는
    것이 마구 불러도 된다는 뜻은 아니다.
    """

    def __init__(self, key, counters, bucket, timeout=TIMEOUT_S):
        self.key = key
        self.counters = counters
        self.bucket = bucket
        self.timeout = timeout

    def _vworld(self, url, direction):
        self.bucket.acquire()
        return call_with_retry(_http_responder(url, self.timeout),
                               service="vworld", direction=direction,
                               counters=self.counters)

    def _ours(self, url, direction):
        return call_with_retry(_http_responder(url, self.timeout),
                               service="ours", direction=direction,
                               counters=self.counters)

    def forward_point(self, rec):
        """기준 `fwd` — 우리 8092 순방향 top-1 좌표. VWorld 는 부르지 않는다."""
        r = self._ours(ours_geocode_url(rec["query"], 5), "fwd")
        if not r.ok:
            return None
        addrs, _nonaddr = parse_ours(r.body or {})
        return cand_lonlat(addrs[0]) if addrs else None

    def reverse(self, item):
        """`(rec, 좌표)` → `(rec, VWorld 결과, 우리 결과)`. 좌표가 없으면 안 부른다."""
        rec, pt = item
        if pt is None:
            return rec, None, None
        rv = self._vworld(vworld_reverse_url(self.key, pt[0], pt[1]), "rev")
        ro = self._ours(ours_reverse_url(pt[0], pt[1], 2), "rev")
        return rec, rv, ro


def chunk_points(chunk, basis, orc, fetcher, pool):
    """청크의 질의 좌표. → `({sid: (lon, lat)}, {sid: 사유})`.

    좌표를 못 만든 건은 **사유와 함께 남긴다.** 조용히 빼면 분모가 줄어들어
    커버리지 결손이 정확도로 둔갑한다.
    """
    pts, skip = {}, {}
    if basis == "fwd":
        for rec, pt in zip(chunk, pool.map(fetcher.forward_point, chunk)):
            if pt is None:
                skip[rec["sid"]] = "우리 순방향이 주소 후보를 내지 않음"
            else:
                pts[rec["sid"]] = pt
        return pts, skip

    jibun = {rec["sid"]: rec["pnu"] for rec in chunk
             if rec["layer"] == "jibun" and rec.get("pnu")}
    got = orc.repr_point_batch(jibun) if jibun else {}
    for rec in chunk:
        sid = rec["sid"]
        if rec["layer"] == "jibun":
            if sid in got:
                pts[sid] = got[sid]
            elif sid not in jibun:
                skip[sid] = "표본에 PNU 없음"
            else:
                skip[sid] = "필지 대표점 부재(우리 DB 에 그 PNU 가 없다)"
        else:
            anchors = rec.get("anchors") or []
            if anchors:
                pts[sid] = anchors[0]          # 출입구 우선, 없으면 건물중심
            else:
                skip[sid] = "원천 좌표 없음(출입구·건물중심 모두 부재)"
    return pts, skip


def rev2_row(rec, basis, rv, ro, skip=None):
    """§4.2 축 판정 1 행. **좌표도 주소 문자열도 남기지 않는다**(§3.3).

    `status != "OK"` 는 HTTP 200 이어도 실패다 — 그 건의 축은 전부 판정불가로
    남고 부재가 일치로 새지 않는다.
    """
    from revmetrics import (compare_axes, ours_axes, ri_cells,
                            split_reverse_entries, vw_parcel_axes,
                            vw_road_axes, vw_status)

    v_ok = bool(rv is not None and rv.ok)
    o_ok = bool(ro is not None and ro.ok)
    entries = split_reverse_entries(rv.body) if v_ok else {"parcel": None,
                                                           "road": None}
    vwp = vw_parcel_axes(entries["parcel"])
    vwr = vw_road_axes(entries["road"])
    ours = ours_axes(ro.body if o_ok else None)

    row = {
        "rev_schema": REV2_SCHEMA,
        "sid": rec["sid"], "layer": rec["layer"], "stratum": rec["stratum"],
        "urban": rec.get("urban"), "basis": basis,
        "basis_ok": skip is None, "skip": skip,
        "rev_v_status": (rv.status if rv is not None else None),
        "vw_has_parcel": entries["parcel"] is not None,
        "vw_has_road": entries["road"] is not None,
        "ours_ok": o_ok,
        "address_source": ours["address_source"],
        "rev_dist_m": _nearest_dist(ro.body) if o_ok else None,
        "axes": compare_axes(vwp, vwr, ours),
        "ri": ri_cells(vwp, ours),
        "t": round(time.time(), 3),
    }
    # `vw_status` 는 재시도 회계를 거친 `rv.status` 와 어긋날 수 있다. 어긋나면
    # 응답 원문 쪽을 믿는다 — 그것이 VWorld 가 실제로 말한 바다.
    if v_ok:
        row["rev_v_status"] = vw_status(rv.body) or row["rev_v_status"]
    return {k: row[k] for k in REV2_FIELDS}


def run_rev2(records, key, basis, out_path, progress_path, counters,
             start=0, log=None):
    """역방향만 재실행한다(F1). 순방향 VWorld 는 호출하지 않는다."""
    from concurrent.futures import ThreadPoolExecutor
    from oracle import Oracle

    log = log or (lambda msg: None)
    bucket = TokenBucket(RATE_LIMIT_RPS, BUCKET_CAPACITY)
    fetcher = Rev2Fetcher(key, counters, bucket, TIMEOUT_S)
    orc = Oracle()
    done = start
    n_skip = 0

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for base in range(start, len(records), CHUNK_SIZE):
            chunk = records[base:base + CHUNK_SIZE]
            t0 = time.time()
            pts, skip = chunk_points(chunk, basis, orc, fetcher, pool)
            n_skip += len(skip)
            items = [(rec, pts.get(rec["sid"])) for rec in chunk]
            lines = []
            for rec, rv, ro in pool.map(fetcher.reverse, items):
                lines.append(json.dumps(
                    rev2_row(rec, basis, rv, ro, skip.get(rec["sid"])),
                    ensure_ascii=False, sort_keys=True))
            with open(out_path, "a") as fh:
                fh.write("\n".join(lines) + "\n")
            done = base + len(chunk)
            with open(progress_path, "w") as fh:
                json.dump({"done": done, "total": len(records)}, fh)
            log("청크 %d~%d  %d 건  기준없음 %d  %.1f s  (오라클 SQL %d 회)"
                % (base, done - 1, len(chunk), len(skip),
                   time.time() - t0, orc.queries))
            del pts, skip, items, lines
    return {"done": done, "basis": basis, "no_basis": n_skip,
            "oracle_queries": orc.queries,
            "legacy_reprpt_hits": orc.legacy_reprpt_hits,
            "legacy_reprpt_attempts": orc.legacy_reprpt_attempts}


# ── 루프 ─────────────────────────────────────────────────────────────
def _truncate_lines(path, keep):
    """resume 시 청크 경계 뒤의 판정을 잘라낸다. 조용히 이어붙이지 않는다(§3.5)."""
    if not os.path.exists(path):
        return 0
    with open(path, "r") as fh:
        lines = fh.readlines()
    if len(lines) <= keep:
        return len(lines)
    with open(path, "w") as fh:
        fh.writelines(lines[:keep])
    return keep


def run(records, key, out_path, progress_path, counters, t=THRESHOLD_M,
        reverse=True, start=0, diag_path=None, diag_max=0, log=None):
    """§3.2 청크 파이프라인. 청크마다 판정 결과만 append 하고 응답은 버린다."""
    from concurrent.futures import ThreadPoolExecutor
    from classify import classify_one
    from oracle import Oracle

    log = log or (lambda msg: None)
    bucket = TokenBucket(RATE_LIMIT_RPS, BUCKET_CAPACITY)
    fetcher = Fetcher(key, counters, bucket, TIMEOUT_S, reverse=reverse)
    orc = Oracle()
    diag_written = 0
    done = start

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for base in range(start, len(records), CHUNK_SIZE):
            chunk = records[base:base + CHUNK_SIZE]
            t0 = time.time()
            rows = list(pool.map(fetcher.fetch, chunk))          # 1~3 단계
            annotate_chunk(rows, orc, t)                          # 4 단계
            lines, diags = [], []
            for obs, rec in zip(rows, chunk):                     # 5 단계
                v = classify_one(obs)
                vr = classify_one(obs, relax12=True)
                lines.append(json.dumps(to_verdict(obs, v, vr),
                                        ensure_ascii=False, sort_keys=True))
                if (diag_path and diag_written + len(diags) < diag_max
                        and (v.gate or v.cls not in (5, 6))):
                    diags.append(json.dumps(to_diag(obs, v, rec["query"]),
                                            ensure_ascii=False, sort_keys=True))
            with open(out_path, "a") as fh:                        # 6 단계
                fh.write("\n".join(lines) + "\n")
            if diags:
                with open(diag_path, "a") as fh:
                    fh.write("\n".join(diags) + "\n")
                diag_written += len(diags)
            done = base + len(chunk)
            with open(progress_path, "w") as fh:
                json.dump({"done": done, "total": len(records)}, fh)
            log("청크 %d~%d  %d 건  %.1f s  (오라클 SQL %d 회)"
                % (base, done - 1, len(chunk), time.time() - t0, orc.queries))
            del rows, lines, diags                                # 7 단계
    return {"done": done, "diag_written": diag_written,
            "relax12_hits": orc.relax12_hits,
            "relax12_attempts": orc.relax12_attempts,
            "oracle_queries": orc.queries}


def main(argv=None):
    ap = argparse.ArgumentParser(description="T046 본 측정 루프(§3.2)")
    ap.add_argument("--sample", choices=("A", "B"), default="A")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--tag", default="main")
    ap.add_argument("--limit", type=int, default=0,
                    help="앞에서 N 건만. 예열용. 0 이면 전량")
    ap.add_argument("--threshold", type=float, default=THRESHOLD_M)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--diag", type=int, default=0,
                    help="진단 보존 상한(§3.3). 0 이면 쓰지 않는다")
    ap.add_argument("--reverse-only", action="store_true",
                    help="F1 — 역방향만 재실행한다. 순방향 VWorld 는 부르지 않는다")
    ap.add_argument("--basis", choices=REV2_BASES, default="src",
                    help="역방향 질의 좌표 기준(--reverse-only 전용)")
    # T047 §3.3 — 사전 상태 게이트의 유일한 탈출구. 기본은 차단이다.
    # 명시적으로 타이핑해야 하고 run_*.json 에 dict_gate_override=true 로 남는다
    # (조용히 넘어가는 순간 그것이 새로운 침묵 실패다).
    ap.add_argument("--allow-dict-degraded", action="store_true",
                    help="사전 미적재 상태에서도 측정을 강행한다. 결과는 오염 가능으로 "
                         "표시되며 run_*.json 에 기록된다. 정상 운용에서는 쓰지 마라")
    args = ap.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import geodist

    sample_path = os.path.join(args.root, "sample",
                               "sample_%s.jsonl" % args.sample.lower())
    out_dir = os.path.join(args.root, "out")
    diag_dir = os.path.join(args.root, "diag")
    for d in (out_dir, diag_dir):
        if not os.path.isdir(d):
            os.makedirs(d)
    # 역방향 재측정은 **다른 이름으로** 쓴다. 기존 판정 파일을 덮으면 전후
    # 대조가 불가능해진다(태스크 §6 금지). `--tag` 도 새 값을 받아야 한다.
    stem = ("rev2_" + args.tag) if args.reverse_only else args.tag
    out_path = os.path.join(out_dir, "%s_%s.jsonl"
                            % ("rev2" if args.reverse_only else "verdict",
                               args.tag))
    progress_path = os.path.join(out_dir, "progress_%s.json" % stem)
    diag_path = os.path.join(diag_dir, "diag_%s.jsonl" % args.tag) if args.diag else None

    def log(msg):
        sys.stdout.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
        sys.stdout.flush()

    counters = Counters()
    geodist.reset_fallback_count()

    win_start = probe_window()
    log("측정 창 시작: %s" % json.dumps(win_start, ensure_ascii=False))

    # ── T047 §3.3 사전 상태 게이트 — 표본 적재·외부 호출보다 **먼저** ──────────────
    #   경고가 아니라 중단이다. T046 은 window_start.health 가 ERR:503 인 채로 12,000 건을
    #   완주해 통째로 오염됐다. 여기서 막지 못하면 그 사고가 그대로 재발한다.
    gate_ok, gate_reason = dict_gate(win_start)
    if gate_ok:
        log("사전 상태 게이트 통과: %s" % gate_reason)
    elif args.allow_dict_degraded:
        log("!! 사전 상태 게이트 강제 통과(--allow-dict-degraded): %s" % gate_reason)
        log("!! 이 측정의 결과는 오염 가능이다. run_%s.json 에 그대로 기록된다." % stem)
    else:
        log("측정 중단 — 사전 상태 게이트: %s" % gate_reason)
        log("강행하려면 --allow-dict-degraded 를 명시하라(결과에 오염 표시가 남는다).")
        return 3

    records = load_sample(sample_path, args.limit or None)
    if args.reverse_only:
        log("표본 %s  %d 건  역방향 재측정  기준=%s  type=both"
            % (args.sample, len(records), args.basis))
    else:
        log("표본 %s  %d 건  (T=%.0f m, 역방향=%s)"
            % (args.sample, len(records), args.threshold, args.sample == "A"))

    start = 0
    if args.resume and os.path.exists(progress_path):
        with open(progress_path) as fh:
            done_prev = json.load(fh).get("done", 0)
        start, wasted = resume_start(done_prev, CHUNK_SIZE)
        counters.note_resume(done_prev, CHUNK_SIZE)
        _truncate_lines(out_path, start)
        log("resume: done=%d → start=%d (재소모 %d 건)" % (done_prev, start, wasted))
    elif os.path.exists(out_path):
        os.remove(out_path)

    t0 = time.time()
    aborted = None
    try:
        if args.reverse_only:
            stats = run_rev2(records, load_api_key(), args.basis, out_path,
                             progress_path, counters, start=start, log=log)
        else:
            stats = run(records, load_api_key(), out_path, progress_path,
                        counters, t=args.threshold,
                        reverse=(args.sample == "A"), start=start,
                        diag_path=diag_path, diag_max=args.diag, log=log)
    except OverRequestLimit as exc:
        aborted = str(exc)
        stats = {"done": start, "diag_written": 0}
        log("한도초과로 중단: %s" % aborted)

    elapsed = time.time() - t0
    win_end = probe_window()
    same = all(win_start.get(k) == win_end.get(k)
               for k in ("geocode_id", "mount_source", "md5_in_container",
                         "md5_on_host", "pg_id"))
    # md5·마운트 일치만으로는 부족하다(이번 사고에서 md5 는 3 자 전부 일치했다).
    # 측정 도중 재기동으로 사전 상태가 바뀌었는지를 별도 축으로 본다(R7).
    d_same = dict_same(win_start, win_end)
    log("측정 창 종료: 일치=%s  사전상태 일치=%s" % (same, d_same))
    if not d_same:
        log("경고: 측정 중 사전 상태가 바뀌었다(재기동 추정). 구간을 폐기하고 재측정하라.")

    report = {
        "tag": args.tag, "sample": args.sample, "n": len(records),
        "mode": "reverse_only" if args.reverse_only else "full",
        "basis": args.basis if args.reverse_only else None,
        "elapsed_s": round(elapsed, 1), "aborted": aborted,
        "window_start": win_start, "window_end": win_end, "window_same": same,
        # T047 §3.3 — 사후에 "이 측정은 사전이 걸린 상태였나"를 기계로 읽는 자리.
        "dict_gate_ok": gate_ok, "dict_gate_reason": gate_reason,
        "dict_gate_override": bool(args.allow_dict_degraded and not gate_ok),
        "dict_same": d_same,
        "geodist_fallback": geodist.fallback_count(),
        "counters": counters.snapshot(),
    }
    report.update(stats)
    rp = os.path.join(out_dir, "run_%s.json" % stem)
    with open(rp, "w") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
    log("완료 %d 건  %.1f s  →  %s" % (stats["done"] - start, elapsed, rp))
    if not same:
        log("경고: 측정 창이 어긋났다. §3.5 대로 구간을 폐기하고 재측정하라.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
