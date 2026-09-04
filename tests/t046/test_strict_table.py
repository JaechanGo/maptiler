#!/usr/bin/env python3
"""T046 N-1 — 완화 전(`_strict`) 병기 집계의 단위 테스트. 순수 함수, 외부 의존 없음.

이 표는 §6.7.3 과 **같은 분모** 위에 서야 의미가 있다. 그래서 다음을 못박는다.
  · 선택 규약 — `layer` 일치 + `basis_ok`. 기준 좌표를 못 만든 건을 세지 않는다
  · 판정불가(`eq=None`)는 분모에서 빠진다(부재를 일치로 세지 않는다)
  · 완화가 뒤집은 건수 — 불일치 → 일치만 센다
  · **자기검증** — 완화가 일치를 깨는 역전, 완화 전후 분모 차이는 0 이어야 하고
    0 이 아니면 표가 그 사실을 숨기지 않고 드러내야 한다

실행: /usr/bin/python3 -m unittest discover -s tests/t046 -t tests/t046 -v
"""
import unittest

import _ctx  # noqa: F401  (sys.path 주입)

from strict_table import (  # noqa: E402
    PREFIX_AXES,
    axis_strict_pair,
    render,
    select,
)


def _row(layer="jibun", basis_ok=True, strict=None, norm=None, axis="sido"):
    """축 하나만 채운 최소 판정 레코드."""
    return {
        "layer": layer,
        "basis_ok": basis_ok,
        "axes": {
            axis + "_strict": {"eq": strict},
            axis: {"eq": norm},
        },
    }


class SelectTest(unittest.TestCase):
    def test_층이_다르면_뺀다(self):
        rows = [_row(layer="jibun"), _row(layer="road")]
        self.assertEqual(len(select(rows, "jibun")), 1)
        self.assertEqual(len(select(rows, "road")), 1)

    def test_기준_좌표를_못_만든_건은_뺀다(self):
        rows = [_row(basis_ok=True), _row(basis_ok=False)]
        self.assertEqual(len(select(rows, "jibun")), 1)

    def test_basis_ok_가_없으면_뺀다(self):
        rows = [{"layer": "jibun", "axes": {}}]
        self.assertEqual(select(rows, "jibun"), [])


class AxisStrictPairTest(unittest.TestCase):
    def test_완화가_뒤집은_건만_센다(self):
        rows = [
            _row(strict=False, norm=True),   # 뒤집힘
            _row(strict=False, norm=True),   # 뒤집힘
            _row(strict=True, norm=True),    # 둘 다 일치 — 뒤집힘 아님
            _row(strict=False, norm=False),  # 둘 다 불일치 — 뒤집힘 아님
        ]
        d = axis_strict_pair(rows, "sido")
        self.assertEqual(d["flipped"], 2)
        self.assertEqual(d["judgeable"], 4)
        self.assertEqual(d["eq_strict"], 1)
        self.assertEqual(d["eq_norm"], 3)
        self.assertAlmostEqual(d["rate_strict"], 0.25)
        self.assertAlmostEqual(d["rate_norm"], 0.75)

    def test_판정불가는_분모에서_빠진다(self):
        rows = [
            _row(strict=True, norm=True),
            _row(strict=None, norm=None),   # 한쪽 부재 — 판정불가
        ]
        d = axis_strict_pair(rows, "sido")
        self.assertEqual(d["judgeable"], 1)
        self.assertEqual(d["judgeable_norm"], 1)
        self.assertAlmostEqual(d["rate_strict"], 1.0)

    def test_판정불가를_일치로_세지_않는다(self):
        rows = [_row(strict=None, norm=None)] * 3
        d = axis_strict_pair(rows, "sido")
        self.assertEqual(d["judgeable"], 0)
        self.assertEqual(d["eq_strict"], 0)
        self.assertIsNone(d["rate_strict"])

    def test_역전을_감지한다(self):
        """완화는 조건을 넓히기만 한다. 일치를 깨면 축 정의가 깨진 것이다."""
        rows = [_row(strict=True, norm=False)]
        d = axis_strict_pair(rows, "sido")
        self.assertEqual(d["reversed"], 1)
        self.assertEqual(d["flipped"], 0)

    def test_판정가능_분모_차이를_감지한다(self):
        rows = [_row(strict=True, norm=None)]
        d = axis_strict_pair(rows, "sido")
        self.assertEqual(d["judgeable"], 1)
        self.assertEqual(d["judgeable_norm"], 0)
        self.assertEqual(d["judgeable_mismatch"], 1)

    def test_한쪽만_판정가능한_건은_뒤집힘으로_세지_않는다(self):
        rows = [_row(strict=None, norm=True)]
        d = axis_strict_pair(rows, "sido")
        self.assertEqual(d["flipped"], 0)
        self.assertEqual(d["reversed"], 0)

    def test_네_축_전부_이름이_맞는다(self):
        """`_strict` 가 존재하는 축은 계층 4 축뿐이다."""
        self.assertEqual([ax for _l, ax in PREFIX_AXES],
                         ["sido", "sgg", "emd", "ri_code"])


class RenderTest(unittest.TestCase):
    def _data(self, strict, norm, reversed_row=False):
        rows = ([_row(strict=strict, norm=norm)]
                + ([_row(strict=True, norm=False)] if reversed_row else []))
        return {("src", "jibun"): {
            "n": len(rows),
            "axes": {ax: axis_strict_pair(
                [_row(strict=strict, norm=norm, axis=ax)]
                + ([_row(strict=True, norm=False, axis=ax)]
                   if reversed_row else []), ax)
                for _l, ax in PREFIX_AXES},
        }}

    def test_완화_전이_정규화_전이_아님을_표에_적는다(self):
        doc = render(self._data(False, True))
        self.assertIn("정규화 전이 아니라 완화 전", doc)

    def test_정상이면_자기검증_통과를_적는다(self):
        doc = render(self._data(False, True))
        self.assertIn("완화가 일치를 깬 건 **0**", doc)
        self.assertNotIn("자기검증 실패", doc)

    def test_역전이_있으면_숨기지_않고_드러낸다(self):
        doc = render(self._data(False, True, reversed_row=True))
        self.assertIn("자기검증 실패", doc)
        self.assertIn("역전 1", doc)

    def test_없는_칸은_열로_내지_않는다(self):
        doc = render(self._data(False, True))
        self.assertIn("`src` 지번", doc)
        self.assertNotIn("`fwd` 도로명", doc)


if __name__ == "__main__":
    unittest.main()
