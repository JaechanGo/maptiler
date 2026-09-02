#!/usr/bin/env python3
"""LOCALDATA 지방행정인허가데이터(카테고리별 CSV) → 상가정보 포맷 통합 CSV (재현 가능).
- 비물리 업종(온라인/제조/도매/농축/공사/대행/행정)은 **카테고리 부분일치(NFC)** 로 제외.
- 영업중만(영업상태명=영업/정상). 좌표 EPSG:5174→4326 (gdaltransform, PROJ 정확 변환).
- 모든 문자열 **NFC 정규화**(한글 NFD/NFC 불일치로 매칭·검색 깨지는 함정 방지).
- 출력 = 상가정보 동일 컬럼 → 09-gen-geocode.py --poi-csv-dir 로 적재(kind=biz, region·업종 인덱싱).
사용: python3 build-localdata.py <인허가정보_DIR> <출력CSV>
"""
import csv, glob, os, re, subprocess, sys, unicodedata
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # PYTHONSAFEPATH=1 대비
from _common.region import parse_region_kr, SIDO_SOURCE, CANON_SIDO, LEGACY_SIDO   # noqa: E402  시도 검증 파서(공용)

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Downloads/인허가정보")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.environ.get("BUILD_HOME") or os.path.expanduser("~/geocode-build"), "localdata/localdata_clean.csv")
N = lambda s: unicodedata.normalize("NFC", s or "")

# 비물리(지도 장소 아님) 업종 — 카테고리/업종명에 이 키워드 포함 시 제외
EXC = [N(x) for x in ["통신판매","방문판매","다단계","전화권유","후원방문","제조","가공","소분","유통전문",
    "첨가물","사육","생산업","사료","종축","부화","인공수정","도축","집유","자동판매기","출판","인쇄",
    "공사","측정대행","컨설팅","관리대행","처리업","배출시설","수질오염","대기오염","원목","목재","제재",
    "지하수","정화조","분뇨","계량기","감리","설계업","승강기","물류","주선","직업소개","상조","옥외광고",
    "소독","청소","위생관리","보관","도매","저수조","고압가스","석연탄","운반","운송","수입"]]
def noise(cat): c=N(cat); return any(k in c for k in EXC)

# parse_region 은 _common/region.py 의 parse_region_kr 로 대체 — 첫 토큰을 검증 없이 시도로 올려
# 시도명 270종·PostGIS biz 1,188행 오염을 만든 원인(실측 2026-09-02). 근거는 그 모듈 docstring.

def convert(pairs):
    if not pairs: return []
    r = subprocess.run(["gdaltransform","-s_srs","EPSG:5174","-t_srs","EPSG:4326"],
                       input="\n".join(f"{x} {y}" for x,y in pairs), capture_output=True, text=True)
    out=[]
    for ln in r.stdout.splitlines():
        p=ln.split(); out.append((round(float(p[0]),6),round(float(p[1]),6)) if len(p)>=2 else (None,None))
    return out

def main():
    print(f"[region] 시도 집합 원천={SIDO_SOURCE} (현행 {len(CANON_SIDO)}·폐지 {len(LEGACY_SIDO)})", file=sys.stderr)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    files = sorted(glob.glob(os.path.join(SRC,"**","*.csv"), recursive=True))
    w = csv.writer(open(OUT,"w",encoding="utf-8",newline=""))
    w.writerow(["상호명","상권업종소분류명","시도명","시군구명","행정동명","경도","위도","전화번호","인허가일자","상권업종대분류명","도로명주소","지번주소"])
    total=0; dropped=0
    for f in files:
        업종 = N(re.sub(r"^[^_]+_","",os.path.basename(f)).replace(".csv",""))
        daebun = N(os.path.basename(os.path.dirname(f)))   # 대분류 = 카테고리 폴더(식품/건강/생활…)
        rows=[]; coords=[]
        try: fp=open(f, encoding="cp949", newline="", errors="replace")
        except OSError: continue
        for row in csv.DictReader(fp):
            if row.get("영업상태명") != "영업/정상": continue
            nm=N(row.get("사업장명")).strip()
            x=(row.get("좌표정보(X)") or "").strip(); y=(row.get("좌표정보(Y)") or "").strip()
            if not nm or not x or not y: continue
            cat=N(row.get("업태구분명")).strip() or 업종
            full=f"{cat} {업종}"
            if noise(full): dropped+=1; continue          # 비물리 제외
            sido,sgg,emd=parse_region_kr(row.get("지번주소"), row.get("도로명주소"), org_code=row.get("개방자치단체코드"))   # 검증 실패 시 코드 폴백→빈값(추정 금지)
            doro=N(row.get("도로명주소")).strip(); jibun=N(row.get("지번주소")).strip()   # ER dedup 건물키 조인용 — 보존
            phone=N(row.get("전화번호")).strip(); opened=(row.get("인허가일자") or "").strip()
            rows.append([nm, full, sido, sgg, emd, phone, opened, daebun, doro, jibun]); coords.append((x,y))
        fp.close()
        ll=convert(coords); n=0
        for r,(lon,lat) in zip(rows,ll):
            if lon is None or not (124<=lon<=132 and 33<=lat<=39): continue
            w.writerow([r[0],r[1],r[2],r[3],r[4],lon,lat,r[5],r[6],r[7],r[8],r[9]]); n+=1
        total+=n
        if n: print(f"  {업종:24s} +{n:,}", file=sys.stderr)
    print(f"OK: {OUT}  유지 {total:,} · 제외(비물리) {dropped:,}", file=sys.stderr)

if __name__ == "__main__":
    main()
