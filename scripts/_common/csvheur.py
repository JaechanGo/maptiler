"""CSV 컬럼 휴리스틱 정본 — 좌표 컬럼 선정 (T028 §4.5).

공공데이터 CSV 는 출처마다 헤더가 제각각이라 두 스크립트가 각자 휴리스틱을
갖고 있었다. 축별로 우열이 갈렸기에 **각 축의 우수한 쪽만** 모은다.

    좌표 선정  : 11b-build-facility.py 가 우수 → 이 모듈(커밋 8)
    인코딩 폴백: postgis/load_facility.py 가 우수 → 11b 로 이식(커밋 9)

■ 이름만 보고 고르면 좌표가 정수 격자로 뭉개진다
  한 파일에 십진 좌표(`WGS84위도` 37.5776)와 DMS 분해 좌표(`위도(도)` 37 /
  `위도(분)` / `위도(초)`)가 함께 실리는 형식이 있다. 헤더 키워드 순서만
  보면 `위도` 가 `위도(도)` 에 먼저 걸려 **정수 '도' 컬럼**을 잡는다.
  민방위대피시설에서 실제로 lat 이 37 로 적재된 사고가 났다(진짜 37.577).
  그래서 후보의 **표본값**을 보고 소수점 유무 + 유효범위로 점수를 매긴다.

■ 후보가 없을 때의 정책은 소비자마다 다르다 — 통합하지 않는다
  11b 는 TM 좌표(EPSG:5174) 폴백이 있어 None 이 안전하다(`pick_coord`).
  load_facility 는 폴백이 없어, 점수를 못 얻으면 종전 선택을 유지해야 한다
  (`pick_coord_index` 를 부르는 쪽에서 폴백). 이 차이는 의도된 것이며
  test_csvheur.py 가 양쪽을 고정한다.

■ 후보 수집(헤더 매칭)은 공유하지 않는다
  11b `find_col()` 은 컬럼**명**을, load_facility `pick()` 은 컬럼**인덱스**를
  돌려주고 소비자도 DictReader / `\\copy` 로 다르다. 통합하면 양쪽 호출부를
  모두 고쳐야 해 T028 의 "동작 불변" 원칙을 깬다(§11 배제 기록).
  그래서 `pick_coord_index` 는 **후보 목록을 인자로 받는다** — 누가 어떻게
  후보를 골랐는지는 관여하지 않는다는 뜻이다.
"""
import re
import unicodedata

SAMPLE_ROWS = 200   # 11b-build-facility.py:61 원문 — 앞 200행만 표본으로 본다


# 헤더 키 정규화(NFC + 공백 제거 + 소문자). 11b-build-facility.py:36-37 원문
def _nk(c):
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", c or "").lower())


def decimal_score(values, lo, hi):
    """표본값이 '십진수 AND lo~hi 범위' 인 비율. 11b-build-facility.py:60-72 원문.

    소수점 유무를 문자열로 본다(`"." in v`). float 로 바꾼 뒤에는 37 과 37.0 을
    구별할 수 없어 DMS '도' 컬럼을 걸러내지 못하기 때문이다.
    빈 값은 분모에서 뺀다 — 결측이 많은 컬럼이 그 이유만으로 지지 않게.
    """
    good = seen = 0
    for v in values:
        v = str(v if v is not None else "").strip()
        if not v:
            continue
        seen += 1
        try:
            f = float(v)
        except ValueError:
            continue
        if "." in v and lo <= f <= hi:   # 십진수 AND 유효범위
            good += 1
    return good / seen if seen else 0.0


def pick_coord(cols, rows, hints, lo, hi):
    """좌표 컬럼**명** 선택 — 11b-build-facility.py:53-77 원문(csv.DictReader 소비자용).

    이름이 힌트에 걸리는 후보 중 표본값 점수가 가장 높은 컬럼을 채택한다.
    십진 후보가 없으면 None — 호출부가 TM 좌표로 폴백한다.
    동점이면 헤더 등장 순서가 앞선 컬럼이 이긴다(`>` 비교, 원문 동작).
    """
    nh = [_nk(h) for h in hints]
    cand = [c for c in cols if c and any(h == _nk(c) or h in _nk(c) for h in nh)]
    best, best_score = None, 0.0
    for c in cand:
        score = decimal_score((r.get(c) for r in rows[:SAMPLE_ROWS]), lo, hi)
        if score > best_score:
            best, best_score = c, score
    return best


def pick_coord_index(cand, rows, lo, hi):
    """좌표 컬럼**인덱스** 선택 — csv.reader 소비자용(postgis/load_facility.py).

    `cand` 는 호출부가 자기 규칙으로 이미 추린 **후보 인덱스 목록**이며,
    그 순서가 곧 호출부의 우선순위다(동점 시 앞선 후보가 이긴다).
    점수를 얻은 후보가 하나도 없으면 None — 호출부가 종전 선택을 유지한다.
    행이 짧아 인덱스가 비면 빈 값으로 친다(원문 `g()` 의 방어와 같은 취지).
    """
    best, best_score = None, 0.0
    for i in cand:
        vals = (r[i] if i < len(r) else "" for r in rows[:SAMPLE_ROWS])
        score = decimal_score(vals, lo, hi)
        if score > best_score:
            best, best_score = i, score
    return best
