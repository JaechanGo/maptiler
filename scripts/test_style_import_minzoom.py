#!/usr/bin/env python3
"""가져온 스타일(STYLE_IMPORT) 경로의 poi 소스 minzoom 계약 — apply_poi_source_minzoom 단위테스트."""
import importlib.util, pathlib, unittest

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("style_objects", HERE / "style_objects.py")
so = importlib.util.module_from_spec(spec); spec.loader.exec_module(so)


def _style(minzoom=11, src="poi"):
    return {"sources": {src: {"type": "vector", "tiles": ["/dyn/poi_mvt/{z}/{x}/{y}"], "minzoom": minzoom, "maxzoom": 22}},
            "layers": [{"id": "poi-label", "type": "symbol", "source": src, "source-layer": "poi", "layout": {}}]}


class ImportedStylePoiMinzoom(unittest.TestCase):
    def test_imported_minzoom_11_is_raised_to_theme_default_15(self):
        st = _style(11)
        self.assertEqual(so.apply_poi_source_minzoom(st, {"poi_tiers": []}), 15)
        self.assertEqual(st["sources"]["poi"]["minzoom"], 15)

    def test_theme_tiers_drive_the_value(self):
        st = _style(11)
        theme = {"poi_tiers": [{"key": "t1", "label": "a", "minzoom": 14}, {"key": "t2", "label": "b", "minzoom": 16}]}
        self.assertEqual(so.apply_poi_source_minzoom(st, theme), 14)

    def test_already_matching_returns_none_and_keeps_layers(self):
        st = _style(15); layers_before = [dict(l) for l in st["layers"]]
        self.assertIsNone(so.apply_poi_source_minzoom(st, None))
        self.assertEqual(st["layers"], layers_before)

    def test_missing_poi_source_is_noop(self):
        st = {"sources": {}, "layers": []}
        self.assertIsNone(so.apply_poi_source_minzoom(st, None))


if __name__ == "__main__":
    unittest.main()
