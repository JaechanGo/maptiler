#!/usr/bin/env python3
"""T046 §4.1·C6 — EPSG:5179(UTM-K) → WGS84. **빌더의 함수를 그대로 재사용한다.**

## 왜 다시 쓰지 않는가

원천 좌표를 WGS84 로 바꾸는 로직을 여기서 새로 구현하면, 그 구현의 오차가 곧
측정 오차가 된다. 그러면 "우리 지오코더가 틀렸다"와 "내 좌표변환이 틀렸다"를
구분할 수 없다. 그래서 `scripts/09-gen-geocode.py` 가 실제 빌드에 쓰는
`utmk_to_wgs84` 를 **문자 그대로 읽어다 실행한다**.

빌더를 import 하지 않고 텍스트를 잘라 exec 하는 이유는, 빌더가 모듈 수준에서
DB 접속·인자 파싱을 하기 때문이다. **빌더를 실행하는 것은 금지**되어 있으므로
(재빌드 금지) 필요한 정의 블록만 떼어낸다.

## 무결성 장치

`SOURCE_CHUNKS` 의 각 원소는 빌더 원문의 **부분문자열**이어야 하고
(`test_utmk.py::test_reused_source_is_verbatim_substring_of_builder`),
`SOURCE_SHA256` 은 그 다이제스트다. 빌더의 변환식이 바뀌면 다이제스트가 바뀌고,
리포트에 실린 값과 대조하면 "어느 버전으로 측정했는가"가 드러난다.

추출은 행번호가 아니라 **앵커 문자열**로 한다. 빌더에 행이 삽입돼도 어긋나지 않고,
앵커가 사라지면 조용히 엉뚱한 블록을 집는 대신 명시적으로 실패한다.

## 타원체

이 블록은 EPSG:5179 정의대로 GRS80(`1/f = 298.257222101`)을 쓴다.
`geodist.py` 는 WGS84(`298.257223563`)를 쓴다 — 대조 기지값의 출처가
PostGIS `geography` 이기 때문이다. 목적이 다른 별개의 상수이므로 통일하지 않는다.
"""
import hashlib
import math
import os

__all__ = [
    "utmk_to_wgs84",
    "compile_variant",
    "SOURCE_PATH",
    "SOURCE_CHUNKS",
    "SOURCE_SHA256",
]

SOURCE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                 "09-gen-geocode.py")
)

_ANCHOR_BEGIN = "# ---- EPSG:5179(UTM-K) → WGS84 (Snyder inverse TM, 무의존) ----"
_ANCHOR_END = "    return round(math.degrees(lon),6), round(math.degrees(lat),6)"


def _read_builder():
    with open(SOURCE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def _extract(text):
    """앵커 사이를 잘라낸다. 앵커가 없으면 조용히 넘어가지 않고 죽는다."""
    i = text.find(_ANCHOR_BEGIN)
    if i < 0:
        raise RuntimeError(
            "빌더에서 시작 앵커를 찾지 못했다: %r (%s)" % (_ANCHOR_BEGIN, SOURCE_PATH)
        )
    j = text.find(_ANCHOR_END, i)
    if j < 0:
        raise RuntimeError(
            "빌더에서 종료 앵커를 찾지 못했다: %r (%s)" % (_ANCHOR_END, SOURCE_PATH)
        )
    return text[i:j + len(_ANCHOR_END)]


SOURCE_CHUNKS = [_extract(_read_builder())]
SOURCE_SHA256 = hashlib.sha256(
    "\n".join(SOURCE_CHUNKS).encode("utf-8")
).hexdigest()


def _compile(source, name):
    """추출한 블록을 격리 네임스페이스에서 실행하고 함수를 꺼낸다.

    블록은 `math` 만 참조한다 — 빌더의 다른 전역에 의존하지 않는다는 사실을
    주입 네임스페이스를 최소화해 강제한다. 새 의존이 생기면 `NameError` 로 드러난다.
    """
    ns = {"math": math}
    exec(compile(source, name, "exec"), ns)  # noqa: S102 — 재사용이 목적이다
    try:
        return ns["utmk_to_wgs84"]
    except KeyError:
        raise RuntimeError("추출한 블록에 utmk_to_wgs84 정의가 없다") from None


utmk_to_wgs84 = _compile("\n".join(SOURCE_CHUNKS), "%s:utmk_to_wgs84" % SOURCE_PATH)


def compile_variant(substitutions):
    """재사용 소스를 문자열 치환해 **변형본**을 만든다(음성 테스트 전용).

    `utmk_to_wgs84` 자체는 건드리지 않는다. 치환 대상이 원문에 없으면
    치환이 조용히 무시되어 "변형본인데 정상 동작"하는 위음성이 생기므로,
    없으면 `KeyError` 로 죽인다.
    """
    src = "\n".join(SOURCE_CHUNKS)
    for old, new in substitutions.items():
        if old not in src:
            raise KeyError("치환 대상이 재사용 소스에 없다: %r" % (old,))
        src = src.replace(old, new)
    return _compile(src, "<utmk-variant>")
