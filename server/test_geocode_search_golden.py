#!/usr/bin/env python3
"""geocode 검색 회귀 골드셋 — 기능(정답) + 성능(임계) 라이브 검증.

test_geocode_api.py(DB불요 단위) 와 달리, 이 골드셋은 **기동된 게이트웨이/geocode API 를 직접 호출**해
한국 주소 검색의 다양한 입력 패턴(도로명 표기변형·지번·건물명 단지+동·POI·행정구역·다중토큰·우편번호·엣지)을
정답(top kind/name 부분일치)과 성능임계로 자동 판정한다. perf-audit·표기패리티 후속 회귀 측정용.

실행:
  python3 server/test_geocode_search_golden.py
  GEOCODE_API_URL=http://localhost:18080 python3 server/test_geocode_search_golden.py   # URL 재정의
종료코드: FAIL(EMPTY/WRONG/TIMEOUT/ERROR) 1건이라도 있으면 1, 전부 PASS/SLOW면 0.

판정: PASS / SLOW(>MAX_OK) / FAIL_EMPTY / FAIL_WRONG / FAIL_TIMEOUT(>TIMEOUT) / FAIL_ERROR
"""
import os, sys, json, time, urllib.request, urllib.parse

BASE = os.environ.get("GEOCODE_API_URL", "http://localhost:18080").rstrip("/") + "/geocode"
TIMEOUT = float(os.environ.get("GEOCODE_TIMEOUT", "5.0"))   # 데모 체감 상한 — 초과는 사실상 '안 됨'
MAX_OK = float(os.environ.get("GEOCODE_MAX_OK", "1.0"))     # 이 이상이면 SLOW

# (카테고리, query, expect)  — expect 키: kind / contains(top name) / min_count / empty
CASES = [
    # ── 도로명주소 (표기 변형 망라: 띄어쓰기/붙여쓰기/복합도로/번지붙음) ──
    ("도로명", "테헤란로 152",              dict(kind="addr", contains="테헤란로 152")),
    ("도로명", "서울 강남구 테헤란로 152",    dict(kind="addr", contains="테헤란로 152")),
    ("도로명", "세종대로 110",              dict(kind="addr", contains="세종대로 110")),
    ("도로명", "과천대로 7나길 9",           dict(kind="addr", contains="과천대로7나길 9")),
    ("도로명", "과천대로7나길9",             dict(kind="addr", contains="과천대로7나길 9")),
    ("도로명", "테헤란로152",               dict(kind="addr", contains="테헤란로 152")),
    ("도로명", "백범로 35",                 dict(kind="addr", contains="백범로 35")),
    # ── 지번주소 ──
    ("지번", "상동 514-8",                 dict(kind="addr", contains="상동 514-8")),
    ("지번", "수원시 영통구 매탄동 1-11",     dict(kind="addr", contains="매탄동 1-11")),
    ("지번", "제주시 노형동 922",           dict(kind="addr", contains="노형동 922")),
    ("지번", "강남구 역삼동 736-1",         dict(kind="addr", contains="역삼동 736-1")),
    # ── 건물명 단지+동 (단지명+동 검색축) ──
    ("건물명", "다정한마을 2105동",          dict(kind="addr", contains="2105동")),
    ("건물명", "한밭자이아파트 111동",        dict(kind="addr", contains="한밭자이아파트 111동")),
    ("건물명", "삼성동 한밭자이아파트 111동",  dict(kind="addr", contains="한밭자이아파트 111동")),
    ("건물명", "다정한마을",                dict(contains="다정한마을")),
    ("건물명", "다정한마을 9999동",          dict(contains="다정한마을")),   # 없는 동 → 단지 fallback
    # ── POI/역/지명 ──
    ("POI", "강남역",                      dict(kind="station", contains="강남역")),
    ("POI", "서울역",                      dict(kind="station", contains="서울역")),
    ("POI", "강남파이낸스센터",             dict(contains="강남파이낸스센터")),
    ("POI", "스타벅스 강남",                dict(min_count=1)),
    # ── 행정구역 ──
    ("행정", "경기도 화성시 장안면",          dict(min_count=1)),
    ("행정", "종로1가",                     dict(contains="종로1가")),
    ("행정", "장안면",                      dict(contains="장안면")),
    ("행정", "강남구 역삼동",               dict(min_count=1)),
    # ── 다중토큰 지역명 (perf-audit 2위 — 성능 취약) ──
    ("다중", "경기도 화성시 만세구 장안면",    dict(min_count=1)),
    ("다중", "서울 강남구 스타벅스",          dict(min_count=1)),
    ("다중", "부산 해운대구 스타벅스",        dict(min_count=1)),
    # ── 한글 짧은/엣지 ──
    ("엣지", "강남",                       dict(min_count=1)),
    ("엣지", "역삼",                       dict(min_count=1)),
    ("엣지", "상동",                       dict(min_count=1)),
    # ── 우편번호/특수 ──
    ("우편", "06236",                      dict(min_count=1)),     # 강남파이낸스센터 우편번호
    ("우편", "13824",                      dict(min_count=1)),
    ("특수", "",                           dict(empty=True)),
    ("특수", "!!!",                        dict(empty=True)),
]

FAIL = {"FAIL_EMPTY", "FAIL_WRONG", "FAIL_TIMEOUT", "FAIL_ERROR"}


def call(q):
    url = BASE + "?" + urllib.parse.urlencode({"q": q, "limit": 8})
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            d = json.load(r)
        return d.get("results", []), time.time() - t0, None
    except Exception as e:
        return None, time.time() - t0, str(e)[:30]


def judge(res, dt, err, exp):
    if err is not None:
        return "FAIL_TIMEOUT" if "timed out" in err else "FAIL_ERROR"
    if exp.get("empty"):
        return "PASS" if not res else "FAIL_WRONG"
    if not res:
        return "FAIL_EMPTY"
    top = res[0]
    if "min_count" in exp and len(res) < exp["min_count"]:
        return "FAIL_EMPTY"
    if "kind" in exp and top.get("kind") != exp["kind"]:
        return "FAIL_WRONG"
    if "contains" in exp and exp["contains"] not in (top.get("name") or ""):
        return "FAIL_WRONG"
    return "SLOW" if dt > MAX_OK else "PASS"


def main():
    cnt, rows = {}, []
    for cat, q, exp in CASES:
        res, dt, err = call(q)
        v = judge(res, dt, err, exp)
        cnt[v] = cnt.get(v, 0) + 1
        top = (res[0].get("name") if res else (err or "-"))
        if top and len(top) > 40:
            top = top[:40] + "…"
        rows.append((v, cat, dt, q, top))
    for v, cat, dt, q, top in rows:
        print(f"  [{v:13}] {dt:5.2f}s {cat:4} | {q!r:30} -> {top}")
    total = len(CASES)
    passed = cnt.get("PASS", 0)
    print("\n=== 골드셋 결과 ===")
    print(f"  총 {total}건 · " + " · ".join(f"{k}={v}" for k, v in sorted(cnt.items())))
    print(f"  PASS {passed}/{total} = {100 * passed // total}%")
    return 1 if any(cnt.get(f) for f in FAIL) else 0


if __name__ == "__main__":
    sys.exit(main())
