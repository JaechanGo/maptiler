"""텍스트 정규화 정본 (T028 §4).

빌드 파이프라인 전역에 흩어져 있던 정규화 함수의 단일 원천이다.

■ NFC / NFKC 발산은 버그가 아니라 알고 감수한 선택이다
  biznrm 은 역사적으로 두 판이 공존해 왔다.

      09-gen-geocode.py : NFC  — geocode.sqlite 의 biz 중복(대표) 판정 키
      dedup_er.py       : NFKC — 개체결합(ER) 의 core 키

  전수 대조 결과 실제로 9,061행이 갈라진다(places 16,282,127행 전수 스캔). 유형은
  한 행을 첫 해당 유형에만 세는 배타 분류이며 합이 정확히 9,061 이다.

      PARENTHESIZED 4,026 · FULLWIDTH 3,648 · ROMAN 735 · HANGUL 402 · OTHER 250

  ※ 계획서 §8 V3 의 "FULLWIDTH 4,915 > PARENTHESIZED 4,028 > ROMAN > HANGUL 657"
    순서는 한 행이 여러 유형에 걸리면 각각 세는 **비배타** 집계 기준이다. 그 수치는
    서로 합산되지 않으며(셋만 더해도 9,600 > 9,061), 총 발산 행수는 양쪽 모두 9,061 이다.

  즉 두 함수는 **이미 서로 다른 데이터를 만들어 왔고**,
  09 가 만든 sqlite 는 NFC 판으로 적재돼 있다. 여기서 한쪽으로 통합하면
  6.6GB 원천을 재빌드하기 전까지는 드러나지 않는 조용한 회귀가 된다.

  그래서 통합하지 않고 이름으로 분리해 보존한다.

      biznrm_nfkc  ← 정본. 신규 코드는 이것을 쓴다.
      biznrm_nfc   ← 호환. 재빌드 전까지 09-gen-geocode.py 전용.

  ※ 과거 09:139 에 있던 "12-build-poi.sh _nrm 와 동일" 주석은 허위였다.
     해당 파일에 _nrm 자체가 없었고 파일도 T028 에서 폐기됐다. NFC 를 정당화하던
     근거는 존재하지 않았다는 뜻이며, 그럼에도 위 실측 발산 때문에 존치한다.

■ corenrm 은 반드시 biznrm_nfkc 에 바인딩한다
  dedup_er.py 원본이 자기 파일의 NFKC 판 biznrm 을 내부 호출했다. NFC 판에
  잘못 붙이면 is_primary 판정이 조용히 바뀐다. test_textnorm.py 가 고정한다.

■ _PUNCT 와 _BIZ_PUNCT 는 통합하지 않는다
  정규식 의미는 같으나(문자클래스 끝의 '-' 는 리터럴이므로 `\\-` 와 등가)
  원문 문자열이 다르다. "원문 그대로" 원칙과 사본 대조 테스트를 위해 분리 유지.

■ server/geocode-api.py · server/geocode-api-pg.py 는 이 모듈을 import 하지 않는다
  컨테이너 빌드 컨텍스트가 server/ 라 scripts/ 를 COPY 할 수 없다(§4.4).
  두 파일은 인라인 사본을 유지하며, 동기화는 test_textnorm.py 의 T3 가 강제한다.
"""
import re
import unicodedata

# dedup_er.py:21 원문
_CORP = re.compile(r"(주식회사|유한회사|유한책임회사|합자회사|합명회사|재단법인|사단법인|의료법인|\(주\)|㈜|\(유\)|\(재\)|\(사\))")
# 지점표시 토큰(공백분리 마지막 토큰에만 적용 → '파리바게뜨신촌점' 같은 무공백 상호를 깎지 않음)
# dedup_er.py:22-23 원문. corenrm 외에 dedup_er.branch_of 도 참조한다
_BRANCH_TOK = re.compile(r"^(본점|직영점|가맹점|영업소|지점|\d{1,3}호점|.{1,5}점)$")
# dedup_er.py:24 원문 — biznrm_nfkc / corenrm 전용
_PUNCT = re.compile(r"[\s()\[\]{}<>（）【】·.,/&\-]+")
# 09-gen-geocode.py:139 원문 — biznrm_nfc 전용. 위와 합치지 말 것
_BIZ_PUNCT = re.compile(r"[\s()\[\]{}<>（）【】·.,/&-]+")


# 주소 표기 정규화. 09-gen-geocode.py(커밋 6 에서 여기로 흡수) / geocode-api.py:57 /
# geocode-api-pg.py:36 원문 — 뒤 둘은 컨테이너 경계 때문에 인라인 사본을 유지한다.
# ※ 한 줄 정의를 유지할 것 — T3 사본 대조가 `def norm(s): ...` 한 줄을 추출해
#   AST 구조까지 비교한다. 함수 내부 docstring 을 넣으면 구조가 달라져 깨진다.
def norm(s): return re.sub(r"\s+"," ",unicodedata.normalize("NFC",s or "")).strip()
# 축약 비교용(마침표·공백 제거). geocode-api.py:58 / geocode-api-pg.py:37 원문.
def rnorm(s): return re.sub(r"[.\s]","",unicodedata.normalize("NFC",s or ""))


# 정본. dedup_er.py:27-28 원문
def biznrm_nfkc(s):
    return _PUNCT.sub("", unicodedata.normalize("NFKC", s or "")).lower()


# 호환. 09-gen-geocode.py:142 원문. 재빌드 전까지 09 전용 — 신규 코드는 쓰지 말 것
def biznrm_nfc(s):
    return _BIZ_PUNCT.sub("", unicodedata.normalize("NFC", s or "")).lower()


# 법인격·지점표시를 걷어낸 상호 핵심. dedup_er.py:30-37 원문(biznrm → biznrm_nfkc)
def corenrm(s):
    t = unicodedata.normalize("NFKC", s or "")
    t = _CORP.sub("", t)
    toks = t.split()
    if len(toks) >= 2 and _BRANCH_TOK.match(toks[-1]):   # 마지막 공백토큰이 지점표시면 제거
        toks = toks[:-1]
    core = biznrm_nfkc(" ".join(toks))
    return core or biznrm_nfkc(s)                      # 전부 깎이면 원본 폴백
