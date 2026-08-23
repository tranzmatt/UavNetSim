import unittest

import numpy as np

from scene.terrain import _repair_elevation_outliers


class TerrainElevationTests(unittest.TestCase):
    def test_repairs_extreme_dem_holes_without_flattening_normal_relief(self):
        elevations = np.linspace(0.0, 130.0, 100).reshape(10, 10)
        elevations[4:7, 5:8] = np.array(
            [
                [-6985.64, -6769.23, -4220.89],
                [-4078.90, -3590.42, -2521.20],
                [-2042.39, -921.69, -395.96],
            ]
        )

        repaired = _repair_elevation_outliers(elevations)

        self.assertTrue(np.isfinite(repaired).all())
        self.assertGreaterEqual(float(repaired.min()), 0.0)
        self.assertLessEqual(float(repaired.max()), 130.0)
        self.assertEqual(repaired[0, 0], elevations[0, 0])
        self.assertEqual(repaired[-1, -1], elevations[-1, -1])

    def test_preserves_consistent_below_sea_level_terrain(self):
        elevations = np.linspace(-430.0, -380.0, 81).reshape(9, 9)

        repaired = _repair_elevation_outliers(elevations)

        np.testing.assert_array_equal(repaired, elevations)

    def test_rejects_grid_without_valid_elevations(self):
        with self.assertRaisesRegex(RuntimeError, "no valid data"):
            _repair_elevation_outliers(np.full((3, 3), np.nan))


if __name__ == "__main__":
    unittest.main()
