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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))       # PYTHONSAFEPATH=1 대비
from _common.csvheur import _nk, pick_coord                          # noqa: E402
from _common.region import parse_region_kr                          # noqa: E402  시도 검증 파서(공용)

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
NAME_HINTS  = ["명칭", "시설명", "사업장명", "상호명", "상호", "화장실명", "발급기명", "장소명",
               "센터명", "설치장소", "위치명", "소재지명", "보관소명", "대피시설명", "수목명", "업소명", "시설물명",
               ]
# ★ 기관명 계열은 NAME_HINTS 에서 분리한 **2순위 폴백**이다. 시설 자체의 이름이 아니라 '관리 주체'라,
#   진짜 이름 컬럼이 있으면 절대 이겨선 안 된다. 같은 리스트에 뒤로 두는 것만으로는 부족했다 —
#   find_col 은 (1) 전 힌트 정확일치 → (2) 전 힌트 부분일치 순이라, 헤더가 '자전거보관소명'이면
#   '보관소명'은 (1)에서 안 걸리고 '관리기관명'이 (1)에서 걸려 여전히 이긴다.
#   [실측 2026-09-01] 그 결과 부천 원미구 전 자전거보관소가 '원미구 건설안전과' 한 이름으로 적재됨
#   (원천엔 '상동호수공원'·'상원고 건너편'이 정상 존재). 리스트를 나눠 단계 자체를 분리한다.
NAME_FALLBACK_HINTS = ["관리기관명", "기관명"]
# 발급기명: 무인민원발급기 설치정보의 위치명(예 '종각역','종로구청'). 관리기관명(='서울특별시 종로구')보다 우선해야 라벨이 유의미.
DORO_HINTS  = ["소재지도로명주소", "도로명주소", "소재지도로명", "도로명전체주소", "설치장소주소"]
JIBUN_HINTS = ["소재지지번주소", "지번주소", "소재지지번", "소재지번주소", "소재지"]
LAT_HINTS   = ["위도", "latitude", "lat", "y좌표", "ycrd", "wgs84위도"]
LON_HINTS   = ["경도", "longitude", "lng", "lon", "x좌표", "xcrd", "wgs84경도"]
TMX_HINTS   = ["좌표정보(x)", "좌표정보x", "tm_x", "tmx", "x좌표값", "epsg5174x"]
TMY_HINTS   = ["좌표정보(y)", "좌표정보y", "tm_y", "tmy", "y좌표값", "epsg5174y"]


# _nk(헤더 키 정규화) 와 pick_coord(좌표 컬럼 값검증) 는 scripts/_common/csvheur.py 가 정본이다(위 import).
# postgis/load_facility.py 가 같은 값검증을 인덱스 계약으로 쓴다 — 실사고 기록은 그 모듈 docstring 에 있다.
# find_col 은 여기 남는다: 컬럼**명**을 돌려주며 소비자가 DictReader 라, 인덱스를 돌려주는
# load_facility.pick() 과 통합하면 양쪽 호출부를 다 고쳐야 한다(T028 §11 배제 기록).
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


# parse_region 은 _common/region.py 의 parse_region_kr 로 대체(11-build-localdata 와 동일 결함 —
# 실측 facility 90행: '경기도수원시'·'세종특별자치시종'·'원창로239번길' 이 시도로 적재).


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
    """인코딩 자동판별 — utf-8(BOM) 우선 strict 시도. 표준데이터는 cp949 가 다수.

    후보 목록과 실패 시 중단 정책은 postgis/load_facility.py `read_rows()` 관례를
    따른다(T028 §4.5 — 인코딩은 그쪽이 우수했다).
    종전에는 최후에 errors="replace" 로 깨진 문자를 그대로 상호명에 실었다. 그
    이름은 지오코딩 색인에 들어가 조용히 남으므로, 적재보다 중단이 낫다.

    빈 파일은 실패가 아니다 — rows == [] 로 돌려주고 main() 이 건너뛴다.
    load_facility 의 `if rows:` 재시도는 이식하지 않는다: 이식하면 0바이트 CSV 가
    후보를 모두 소진해 빌드 전체를 죽인다(수집이 0바이트를 남긴 전례가 있다).
    OSError 는 더 이상 삼키지 않는다 — 파일 부재를 인코딩 실패로 오인해 보고했다.
    """
    for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            with open(path, encoding=enc, newline="", errors="strict") as fp:
                return list(csv.DictReader(fp)), enc
        except (UnicodeDecodeError, LookupError):
            continue
    sys.exit(f"CSV 인코딩 판별 실패: {path}")


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
        # 시설명 계열을 정확·부분 일치까지 모두 소진한 뒤에야 기관명으로 폴백(위 ★ 참조).
        c_name = find_col(cols, NAME_HINTS) or find_col(cols, NAME_FALLBACK_HINTS)
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
                sido, sgg, emd = parse_region_kr(doro, jibun, org_code=r.get("개방자치단체코드"))       # 도로명 우선(09 의 navi 조인키)
                w.writerow([nm, label, sido, sgg, emd, "", "", "", "", "생활편의", doro_s, jibun_s]); ng += 1
            elif mode == "wgs":
                try:
                    lon = round(float(str(r.get(c_lon)).strip()), 6)
                    lat = round(float(str(r.get(c_lat)).strip()), 6)
                except (ValueError, TypeError):
                    continue
                if not (124 <= lon <= 132 and 33 <= lat <= 39):
                    skipped += 1; continue
                sido, sgg, emd = parse_region_kr(jibun, doro, org_code=r.get("개방자치단체코드"))
                w.writerow([nm, label, sido, sgg, emd, lon, lat, "", "", "생활편의", N(doro).strip(), N(jibun).strip()]); n += 1
            else:
                x = str(r.get(c_tmx) or "").strip(); y = str(r.get(c_tmy) or "").strip()
                if not x or not y:
                    continue
                tm_rows.append((nm, jibun, doro, r.get("개방자치단체코드"))); tm_coords.append((x, y))
        if tm_rows:
            for (nm, jibun, doro, org), (lon, lat) in zip(tm_rows, tm5174_to_wgs(tm_coords)):
                if lon is None or not (124 <= lon <= 132 and 33 <= lat <= 39):
                    skipped += 1; continue
                sido, sgg, emd = parse_region_kr(jibun, doro, org_code=org)
                w.writerow([nm, label, sido, sgg, emd, lon, lat, "", "", "생활편의", N(doro).strip(), N(jibun).strip()]); n += 1
        total += n; geopend += ng
        if n or ng:
            print(f"    +{n:,}{f' · 주소지오코딩대기 +{ng:,}' if ng else ''}", file=sys.stderr)
    print(f"OK: {OUT}  적재 {total:,} · 지오코딩대기 {geopend:,} · 제외 {skipped:,}", file=sys.stderr)


if __name__ == "__main__":
    main()
