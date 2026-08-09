#!/usr/bin/env python3
"""생활편의시설(공공) 표준 CSV → kind=facility 적재용 통합 CSV (재현 가능).
- 14종 스키마가 제각각이라 컬럼을 **휴리스틱 자동탐지**: 위경도(WGS84) 또는 좌표정보(X/Y)(EPSG:5174) + 명칭/주소.
- 폴더 구조: staged/facility/<항목key>/*.csv  (<항목key>→시설명은 facility-catalog.json 참조, 없으면 폴더명).
- 출력 = 상가포맷 호환 컬럼 + 대분류='생활편의'. poi-all 에 두면 09-gen-geocode.py 가 facility_clean.csv 를 kind=facility 로 적재.
- 좌표: 위도/경도(WGS84)면 그대로, 좌표정보(X/Y)면 EPSG:5174→4326 (gdaltransform, PROJ 정확 변환).
- 모든 문자열 NFC 정규화. 한국 bbox(경도 124~132, 위도 33~39) 밖 좌표 제외.
사용: python3 11b-build-facility.py <facility_DIR> <출력CSV>
"""
import csv, glob, json, os, re, subprocess, sys, unicodedata

N = lambda s: unicodedata.normalize("NFC", s or "")
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "staged/facility")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "poi-all/facility_clean.csv")

# 항목key → 시설명(한글) 라벨
LABELS = {}
try:
    _cj = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "facility-catalog.json"), encoding="utf-8"))
    LABELS = {it["key"]: it["name"] for it in _cj.get("items", [])}
except Exception:
    pass

# 컬럼 휴리스틱(소문자·공백제거 후 정확→부분 일치). 14종 표준데이터의 흔한 헤더 변형 포함.
NAME_HINTS  = ["명칭", "시설명", "사업장명", "상호명", "상호", "화장실명", "발급기명", "관리기관명", "기관명", "장소명",
               "센터명", "설치장소", "위치명", "소재지명", "보관소명", "대피시설명", "수목명", "업소명", "시설물명"]
# 발급기명: 무인민원발급기 설치정보의 위치명(예 '종각역','종로구청'). 관리기관명(='서울특별시 종로구')보다 우선해야 라벨이 유의미.
DORO_HINTS  = ["소재지도로명주소", "도로명주소", "소재지도로명", "도로명전체주소", "설치장소주소"]
JIBUN_HINTS = ["소재지지번주소", "지번주소", "소재지지번", "소재지번주소", "소재지"]
LAT_HINTS   = ["위도", "latitude", "lat", "y좌표", "ycrd", "wgs84위도"]
LON_HINTS   = ["경도", "longitude", "lng", "lon", "x좌표", "xcrd", "wgs84경도"]
TMX_HINTS   = ["좌표정보(x)", "좌표정보x", "tm_x", "tmx", "x좌표값", "epsg5174x"]
TMY_HINTS   = ["좌표정보(y)", "좌표정보y", "tm_y", "tmy", "y좌표값", "epsg5174y"]


def _nk(c):
    return re.sub(r"\s+", "", N(c).lower())


def find_col(cols, hints):
    keys = {_nk(c): c for c in cols if c}
    for h in hints:                      # 1) 정확 일치
        if _nk(h) in keys:
            return keys[_nk(h)]
    for h in hints:                      # 2) 부분 일치
        h2 = _nk(h)
        for k, orig in keys.items():
            if h2 in k:
                return orig
    return None


def pick_coord(cols, rows, hints, lo, hi):
    """좌표(위/경도) 컬럼 선택 — 이름이 힌트에 걸리는 후보 중 **표본값이 십진수(소수점)+유효범위(lo~hi)**
    인 비율이 가장 높은 컬럼을 채택. 한 파일에 위도(EPSG4326)·WGS84위도(십진수)와 위도(도)/(분)/(초)
    (DMS 분해)가 함께 있을 때, 이름 순서만 보면 정수 '도' 컬럼을 먼저 잡아 좌표를 정수격자로 뭉개는 사고가
    났음(예: 민방위대피시설 → lat 37, 진짜 37.577). 값으로 검증해 십진 컬럼을 우선. 십진 후보 없으면 None."""
    nh = [_nk(h) for h in hints]
    cand = [c for c in cols if c and any(h == _nk(c) or h in _nk(c) for h in nh)]
    best, best_score = None, 0.0
    for c in cand:
        good = seen = 0
        for r in rows[:200]:                 # 앞 200행 표본
            v = str(r.get(c) or "").strip()
            if not v:
                continue
            seen += 1
            try:
                f = float(v)
            except ValueError:
                continue
            if "." in v and lo <= f <= hi:   # 십진수 AND 유효범위
                good += 1
        score = good / seen if seen else 0.0
        if score > best_score:
            best, best_score = c, score
    return best


def parse_region(*addrs):
    for a in addrs:
        t = N(a or "").split()
        if len(t) >= 2:
            sgg = t[1] + (" " + t[2] if len(t) >= 3 and t[2].endswith("구") else "")  # 시+구(예: 수원시 영통구) — navi sigungu 포맷에 맞춤
            emd = next((x for x in t[2:5] if x.endswith(("동", "읍", "면", "리", "가"))), "")
            return t[0], sgg, emd
    return "", "", ""


def tm5174_to_wgs(pairs):
    if not pairs:
        return []
    r = subprocess.run(["gdaltransform", "-s_srs", "EPSG:5174", "-t_srs", "EPSG:4326"],
                       input="\n".join(f"{x} {y}" for x, y in pairs), capture_output=True, text=True)
    out = []
    for ln in r.stdout.splitlines():
        p = ln.split()
        out.append((round(float(p[0]), 6), round(float(p[1]), 6)) if len(p) >= 2 else (None, None))
    return out


def read_csv(path):
    """인코딩 자동판별 — utf-8(BOM) 우선 strict 시도 후 cp949. 표준데이터는 cp949 가 다수."""
    for enc in ("utf-8-sig", "cp949"):
        try:
            with open(path, encoding=enc, newline="", errors="strict") as fp:
                return list(csv.DictReader(fp)), enc
        except (UnicodeError, OSError):
            continue
    with open(path, encoding="cp949", newline="", errors="replace") as fp:
        return list(csv.DictReader(fp)), "cp949(replace)"


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    files = sorted(glob.glob(os.path.join(SRC, "**", "*.csv"), recursive=True))
    w = csv.writer(open(OUT, "w", encoding="utf-8", newline=""))
    w.writerow(["상호명", "상권업종소분류명", "시도명", "시군구명", "행정동명",
                "경도", "위도", "전화번호", "인허가일자", "상권업종대분류명", "도로명주소", "지번주소"])
    total = 0; skipped = 0; geopend = 0
    for f in files:
        rel = os.path.relpath(f, SRC).split(os.sep)
        item = rel[0] if len(rel) > 1 else os.path.splitext(os.path.basename(f))[0]
        label = LABELS.get(item, N(item))
        rows, enc = read_csv(f)
        if not rows:
            continue
        cols = list(rows[0].keys())
        c_name = find_col(cols, NAME_HINTS)
        c_lat = pick_coord(cols, rows, LAT_HINTS, 33, 39)    # 값검증(십진+범위)으로 DMS '도' 컬럼 회피
        c_lon = pick_coord(cols, rows, LON_HINTS, 124, 132)
        c_tmx, c_tmy = find_col(cols, TMX_HINTS), find_col(cols, TMY_HINTS)
        c_doro, c_jibun = find_col(cols, DORO_HINTS), find_col(cols, JIBUN_HINTS)
        mode = "wgs" if (c_lat and c_lon) else ("tm5174" if (c_tmx and c_tmy) else None)
        # 좌표 컬럼이 없어도 이름·주소가 있으면 좌표 빈칸으로 출력 → 09-gen-geocode 가 navi 도로명주소로 지오코딩
        geocode = (not mode) and bool(c_name) and bool(c_doro or c_jibun)
        print(f"  [{label}] {os.path.basename(f)} enc={enc} name={c_name} coord={mode or ('addr-geocode' if geocode else None)}", file=sys.stderr)
        if not mode and not geocode:
            print(f"    ! 좌표·주소 모두 없음 — 건너뜀 (헤더: {cols[:10]})", file=sys.stderr)
            continue
        tm_rows = []; tm_coords = []; n = 0; ng = 0
        for r in rows:
            nm = (N(r.get(c_name)).strip() if c_name else "") or label
            jibun = r.get(c_jibun) if c_jibun else ""; doro = r.get(c_doro) if c_doro else ""
            if geocode:
                doro_s = N(doro).strip(); jibun_s = N(jibun).strip()
                if not (doro_s or jibun_s):
                    skipped += 1; continue                       # 주소도 비면 못 씀
                sido, sgg, emd = parse_region(doro, jibun)       # 도로명 우선(09 의 navi 조인키)
                w.writerow([nm, label, sido, sgg, emd, "", "", "", "", "생활편의", doro_s, jibun_s]); ng += 1
            elif mode == "wgs":
                try:
                    lon = round(float(str(r.get(c_lon)).strip()), 6)
                    lat = round(float(str(r.get(c_lat)).strip()), 6)
                except (ValueError, TypeError):
                    continue
                if not (124 <= lon <= 132 and 33 <= lat <= 39):
                    skipped += 1; continue
                sido, sgg, emd = parse_region(jibun, doro)
                w.writerow([nm, label, sido, sgg, emd, lon, lat, "", "", "생활편의", N(doro).strip(), N(jibun).strip()]); n += 1
            else:
                x = str(r.get(c_tmx) or "").strip(); y = str(r.get(c_tmy) or "").strip()
                if not x or not y:
                    continue
                tm_rows.append((nm, jibun, doro)); tm_coords.append((x, y))
        if tm_rows:
            for (nm, jibun, doro), (lon, lat) in zip(tm_rows, tm5174_to_wgs(tm_coords)):
                if lon is None or not (124 <= lon <= 132 and 33 <= lat <= 39):
                    skipped += 1; continue
                sido, sgg, emd = parse_region(jibun, doro)
                w.writerow([nm, label, sido, sgg, emd, lon, lat, "", "", "생활편의", N(doro).strip(), N(jibun).strip()]); n += 1
        total += n; geopend += ng
        if n or ng:
            print(f"    +{n:,}{f' · 주소지오코딩대기 +{ng:,}' if ng else ''}", file=sys.stderr)
    print(f"OK: {OUT}  적재 {total:,} · 지오코딩대기 {geopend:,} · 제외 {skipped:,}", file=sys.stderr)


if __name__ == "__main__":
    main()
