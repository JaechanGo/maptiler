#!/usr/bin/env python3
"""검색(/geocode·/reverse) 라이브 검증 (빌드 자산: scripts/qc) — 중복·오류 데이터·속도. 사용: search_verify.py http://host:18080"""
import json, math, statistics, sys, time, urllib.parse, urllib.request, urllib.error

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://192.168.102.244:18080").rstrip("/")
TIMEOUT = 20
VALID_SIDO = {"서울특별시","부산광역시","대구광역시","인천광역시","대전광역시","울산광역시","세종특별자치시","경기도",
              "강원특별자치도","충청북도","충청남도","전북특별자치도","전남광주통합특별시","경상북도","경상남도",
              "제주특별자치도","광주광역시","전라남도","전라북도","강원도","제주도"}
BBOX = (124.5, 33.0, 132.6, 38.7)

QUERIES = {
  "역": ["상동역", "춘의역", "부천시청역", "서울역", "강남역", "부산역", "대전역", "광주송정역", "동대구역", "춘천역"],
  "POI": ["부천시청", "세브란스병원", "이마트 부천", "스타벅스 부천", "부천 파출소", "부천 초등학교", "부천 자전거보관소",
          "부천 공중화장실", "서울시청", "국립중앙박물관", "인천공항", "경기도청"],
  "주소": ["경기도 부천시 원미구 길주로 104", "서울특별시 중구 세종대로 110", "부천시 상동 543", "경기 부천시 소사구 경인로 214",
          "대전광역시 서구 둔산로 100", "제주특별자치도 제주시 문연로 6", "세종특별자치시 한누리대로 2130", "부산 해운대구 우동 1408"],
  "구명칭": ["광주광역시 서구 내방로 111", "전남광주통합특별시 서구 내방로 111", "강원도 춘천시 중앙로 1", "강원특별자치도 춘천시 중앙로 1",
           "전라북도 전주시 완산구 효자로 225", "전북특별자치도 전주시 완산구 효자로 225"],
  "짧은질의": ["상", "부천", "역", "시청", "병원"],
}

def get(path, params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    t = time.time()
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read().decode()), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, None, time.time() - t
    except Exception as e:
        return -1, str(e)[:60], time.time() - t

def dist_m(a, b):
    dx = (a[0] - b[0]) * 88800 * math.cos(math.radians(a[1])); dy = (a[1] - b[1]) * 111000
    return math.hypot(dx, dy)

def norm(s):
    return "".join(ch for ch in (s or "") if ch.isalnum()).lower()

issues = []; lat = {}; codes = {}
seen = {}
for cat, qs in QUERIES.items():
    for q in qs:
        code, j, dt = get("/geocode", {"q": q, "limit": 10})
        if code != 200 or not isinstance(j, dict):
            # 비200 은 1초 뒤 1회 재시도 — 스왑 직후 새 힙이 전부 콜드인 HDD 에선 첫 호출만 3s 를 넘기는 경우가 있다
            # ([실측 2026-09-04] '부천 파출소' 콜드 2.0s → 웜 0.12s). 재시도 성공은 'HTTP-콜드'(참고치), 연속 실패만 게이트 FAIL.
            time.sleep(1)
            code2, j2, dt2 = get("/geocode", {"q": q, "limit": 10})
            if code2 == 200 and isinstance(j2, dict):
                issues.append(("HTTP-콜드", q, f"1회차 {code} {dt:.1f}s → 재시도 200 {dt2:.1f}s"))
                code, j, dt = code2, j2, dt2
        lat.setdefault(cat, []).append(dt); codes[code] = codes.get(code, 0) + 1
        if code != 200 or not isinstance(j, dict):
            issues.append(("HTTP", q, f"{code} {dt:.1f}s(재시도 포함 2회) {j if not isinstance(j, dict) else ''}")); continue
        res = j.get("results", [])
        seen[q] = res
        if not res:
            issues.append(("결과없음", q, "")); continue
        # 중복: 정규화 상호 동일 + 30m 이내 / 상호+주소 전문 동일
        for i in range(len(res)):
            for k in range(i + 1, len(res)):
                a, b = res[i], res[k]
                if norm(a.get("name")) == norm(b.get("name")):
                    d = dist_m((a["lon"], a["lat"]), (b["lon"], b["lat"]))
                    fa = (a.get("address") or {}).get("full") or (a.get("address") or {}).get("road") or (a.get("address") or {}).get("parcel")
                    fb = (b.get("address") or {}).get("full") or (b.get("address") or {}).get("road") or (b.get("address") or {}).get("parcel")
                    same_addr = bool(fa) and fa == fb and norm(fa) != norm(a.get("name"))
                    if d < 30 or same_addr:
                        issues.append(("중복", q, f"{a.get('name')} [{a.get('kind')}/{a.get('source')}] vs [{b.get('kind')}/{b.get('source')}] {d:.0f}m"))
        names = {}
        for r in res: names.setdefault(norm(r.get("name")), []).append(r)
        for nm, grp in names.items():
            if len(grp) >= 3:
                issues.append(("동명다건", q, f"{grp[0].get('name')} ×{len(grp)} kinds={sorted(set(str(g.get('kind'))+'/'+str(g.get('source')) for g in grp))}"))
        # 오류 데이터
        for r in res:
            st = ((r.get("address") or {}).get("structure") or {})
            sido = st.get("sido"); sec = (r.get("display") or {}).get("secondary") or ""
            if not (BBOX[0] <= r["lon"] <= BBOX[2] and BBOX[1] <= r["lat"] <= BBOX[3]):
                issues.append(("좌표범위", q, f"{r.get('name')} {r['lon']},{r['lat']}"))
            if sido and sido not in VALID_SIDO:
                issues.append(("비정규시도", q, f"{r.get('name')} sido={sido}"))
            if r.get("kind") in ("biz", "facility") and not sec:
                issues.append(("지역표시없음", q, f"{r.get('name')} [{r.get('kind')}]"))
            nm = r.get("name") or ""
            if any(w in nm for w in ("건설안전과", "관리사무소", "담당자")) and r.get("kind") == "facility":
                issues.append(("시설명오매핑", q, nm))
            if not nm.strip():
                issues.append(("빈이름", q, json.dumps(r, ensure_ascii=False)[:80]))
        # 역지오코딩 일관성(top1): 결과 좌표의 시군구가 결과 표시 시군구와 맞는가
        top = res[0]
        c2, rv, dt2 = get("/reverse", {"lon": top["lon"], "lat": top["lat"]})
        lat.setdefault("reverse", []).append(dt2)
        if c2 == 200 and isinstance(rv, dict):
            rr = (rv.get("results") or rv.get("result") or [rv])
            rr = rr[0] if isinstance(rr, list) and rr else rv
            rst = ((rr.get("address") or {}).get("structure") or rr.get("structure") or {})
            rsgg = rst.get("sigungu") or ""
            tsgg = (((top.get("address") or {}).get("structure") or {}).get("sigungu")) or ""
            sec = (top.get("display") or {}).get("secondary") or ""
            if rsgg and (tsgg or sec) and rsgg not in (tsgg + " " + sec):
                issues.append(("좌표-시군구불일치", q, f"{top.get('name')} 표시={tsgg or sec} 역지오={rsgg}"))
        else:
            issues.append(("reverse-HTTP", q, f"{c2} {dt2:.1f}s"))

# 구명칭/신명칭 동등성
pairs = [("광주광역시 서구 내방로 111", "전남광주통합특별시 서구 내방로 111"),
         ("강원도 춘천시 중앙로 1", "강원특별자치도 춘천시 중앙로 1"),
         ("전라북도 전주시 완산구 효자로 225", "전북특별자치도 전주시 완산구 효자로 225")]
for a, b in pairs:
    ra, rb = seen.get(a) or [], seen.get(b) or []
    if ra and rb:
        d = dist_m((ra[0]["lon"], ra[0]["lat"]), (rb[0]["lon"], rb[0]["lat"]))
        sa = ((ra[0].get("address") or {}).get("structure") or {}).get("sido")
        sb = ((rb[0].get("address") or {}).get("structure") or {}).get("sido")
        if d > 50 or sa != sb:
            issues.append(("구/신명칭 불일치", a, f"{d:.0f}m sido {sa} vs {sb}"))
        else:
            print(f"  ✓ 구/신명칭 동등: {a} ≡ {b} → {sa} ({d:.0f}m)")

# 속도: 반복 20회(warm)
rep = []
for _ in range(20):
    for q in ("상동역", "부천시청", "경기도 부천시 원미구 길주로 104"):
        code, j, dt = get("/geocode", {"q": q, "limit": 5}); rep.append((code, dt))
ok = [d for c, d in rep if c == 200]
print("\n== 속도(warm, 60회) ==")
if ok:
    ok.sort(); print(f"  p50 {ok[len(ok)//2]*1000:.0f}ms · p95 {ok[int(len(ok)*0.95)-1]*1000:.0f}ms · max {ok[-1]*1000:.0f}ms · 비200 {len(rep)-len(ok)}회")
for cat, ds in lat.items():
    ds = sorted(ds); print(f"  {cat:8s} n={len(ds):2d} p50 {ds[len(ds)//2]*1000:6.0f}ms  max {ds[-1]*1000:6.0f}ms")
print("  HTTP 코드 분포:", codes)

print("\n== 이슈 ==")
from collections import Counter
cnt = Counter(t for t, _, _ in issues)
print("  유형별:", dict(cnt) if cnt else "없음")
for t, q, d in issues[:60]:
    print(f"  [{t}] {q} — {d}")
print(f"\n총 질의 {sum(len(v) for v in QUERIES.values())} · 이슈 {len(issues)}")

# 게이트: HTTP 비200(재시도까지 실패)·좌표범위·비정규시도·시설명오매핑·빈이름·구/신명칭 불일치가 하나라도 있으면 FAIL.
# 중복/동명다건/HTTP-콜드(재시도 성공)는 참고치.
_hard = {"HTTP", "좌표범위", "비정규시도", "시설명오매핑", "빈이름", "구/신명칭 불일치", "reverse-HTTP"}
_bad = [i for i in issues if i[0] in _hard]
print("GATE:", "PASS" if not _bad else f"FAIL ({len(_bad)})")
sys.exit(0 if not _bad else 1)
