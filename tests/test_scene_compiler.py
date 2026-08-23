import json
import tempfile
import unittest
from pathlib import Path

from scene.compiler import _polygon, compile_scene
from scene.models import EnuPoint, GeoAnchor, SceneFeature, SceneModel


def _building(feature_id, points):
    return SceneFeature(
        id=feature_id,
        category="building",
        footprint=[EnuPoint(x=x, y=y) for x, y in points],
        height=10,
    )


class SceneCompilerFootprintTests(unittest.TestCase):
    def test_accepts_small_valid_osm_building_part(self):
        feature = _building(
            "osm-building-1323090098",
            [(0, 0), (0.4017, -0.2986), (0.9992, 0.5086), (0.5975, 0.7961), (0, 0)],
        )

        polygon = _polygon(feature)

        self.assertTrue(polygon.is_valid)
        self.assertAlmostEqual(polygon.area, 0.4971, places=3)

    def test_rejects_degenerate_footprint(self):
        feature = _building("degenerate", [(0, 0), (1, 1), (2, 2)])

        with self.assertRaisesRegex(ValueError, "invalid footprint"):
            _polygon(feature)

    def test_compile_skips_only_irrecoverable_feature(self):
        scene = SceneModel(
            name="Footprint recovery",
            anchor=GeoAnchor(latitude=0, longitude=0),
            size_x=20,
            size_y=20,
            features=[
                _building("valid", [(1, 1), (3, 1), (3, 3), (1, 3)]),
                _building("degenerate", [(5, 5), (6, 6), (7, 7)]),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            compile_scene(scene, output, enable_osm2world=False)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

            self.assertTrue((output / "meshes" / "building-0.ply").is_file())
            self.assertFalse((output / "meshes" / "building-1.ply").exists())
            self.assertEqual(
                manifest["skipped_mesh_features"],
                [{"id": "degenerate", "reason": "Feature degenerate has an invalid footprint"}],
            )


if __name__ == "__main__":
    unittest.main()
