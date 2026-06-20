#!/usr/bin/env python3
"""AED(자동심장충격기) 수집 — E-Gen openAPI 페이지네이션 → CSV (Phase 4).

data.go.kr 15000652 국립중앙의료원 AED 정보. 파일 직다운이 없어(openAPI 전용) build-studio collect 모델과
안 맞으므로 standalone. serviceKey(자동승인) 필요 → 발급 후 환경변수로. 좌표 포함이라 geom 바로 채워짐.
출력 CSV 를 load_facility.py 가 적재:
  DATAGO_SERVICE_KEY=... scripts/postgis/collect_aed.py --out ~/geocode-build/staged/facility_src/aed.csv
  scripts/postgis/load_facility.py --kind aed --csv ~/geocode-build/staged/facility_src/aed.csv --source data.go.kr:15000652
※serviceKey 는 data.go.kr 발급 디코딩키(자동 URL 인코딩). 엔드포인트/필드명이 바뀌면 BASE/FIELDS 조정.
"""
import argparse, csv, os, ssl, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

BASE = "http://apis.data.go.kr/B552657/AEDInfoInqireService/getEgytAedManageInfoInqire"
# item 필드(이름·주소·좌표) 후보 — 응답 스키마 변동 대비 복수 후보.
F_NAME = ("org", "buildPlace", "buildPlaceNm")
F_ADDR = ("buildAddress", "address", "addr")
F_LAT  = ("wgs84Lat", "lat", "latitude")
F_LON  = ("wgs84Lon", "lon", "longitude")


def _first(el, names):
    for n in names:
        c = el.find(n)
        if c is not None and (c.text or "").strip():
            return c.text.strip()
    return ""


def fetch_all(key, rows_per=1000, max_pages=2000):
    ctx = ssl._create_unverified_context()
    out = []; page = 1; total = None
    while page <= max_pages:
        qs = urllib.parse.urlencode({"serviceKey": key, "numOfRows": rows_per, "pageNo": page}, safe="%")
        # serviceKey 가 이미 URL 인코딩본일 수 있어 safe='%' 로 이중인코딩 회피. 디코딩키면 quote 필요할 수 있음.
        url = f"{BASE}?{qs}"
        try:
            xml = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                         timeout=60, context=ctx).read()
        except Exception as e:
            sys.exit(f"AED API 호출 실패(page {page}): {str(e)[:140]}")
        root = ET.fromstring(xml)
        rc = root.findtext(".//resultCode")
        if rc not in (None, "00", "0"):
            sys.exit(f"AED API 오류 resultCode={rc} msg={root.findtext('.//resultMsg')} "
                     "(serviceKey 등록/인코딩 확인 — 디코딩키면 --raw-key 옵션)")
        items = root.findall(".//item")
        for it in items:
            out.append({"name": _first(it, F_NAME), "addr": _first(it, F_ADDR),
                        "lat": _first(it, F_LAT), "lon": _first(it, F_LON)})
        if total is None:
            total = int(root.findtext(".//totalCount") or 0)
        print(f"  page {page}: +{len(items)} (누적 {len(out)}/{total})", file=sys.stderr)
        if not items or len(out) >= total:
            break
        page += 1; time.sleep(0.3)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--key", default=os.environ.get("DATAGO_SERVICE_KEY", ""))
    ap.add_argument("--raw-key", action="store_true", help="디코딩키를 quote 처리(이중인코딩 오류 시)")
    args = ap.parse_args()
    if not args.key:
        sys.exit("serviceKey 없음 — DATAGO_SERVICE_KEY 환경변수 또는 --key (data.go.kr AED 15000652 활용신청)")
    key = urllib.parse.quote(args.key, safe="") if args.raw_key else args.key

    rows = fetch_all(key)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    n = 0
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["설치기관", "주소", "위도", "경도"])   # load_facility 휴리스틱 감지
        for r in rows:
            w.writerow([r["name"], r["addr"], r["lat"], r["lon"]]); n += 1
    print(f"OK: AED {n:,}건 → {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
