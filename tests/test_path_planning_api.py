import tempfile
import unittest
from pathlib import Path

from api.app import create_plan, planners
from path_planning.models import PlanningRequest
from path_planning.service import plan_trajectory
from scene.models import EnuPoint, GeoAnchor, SceneFeature, SceneModel


class PathPlanningApiTests(unittest.TestCase):
    def setUp(self):
        self.scene = SceneModel(
            name="Planning test",
            anchor=GeoAnchor(latitude=0.0, longitude=0.0),
            size_x=100.0,
            size_y=100.0,
            features=[
                SceneFeature(
                    id="block",
                    category="building",
                    footprint=[
                        EnuPoint(x=40, y=30), EnuPoint(x=60, y=30),
                        EnuPoint(x=60, y=70), EnuPoint(x=40, y=70),
                    ],
                    height=40,
                )
            ],
        )
        self.tempdir = tempfile.TemporaryDirectory()
        self.scene_path = Path(self.tempdir.name) / "scene.json"
        self.scene_path.write_text(self.scene.model_dump_json(), encoding="utf-8")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_registry_exposes_dynamic_parameter_schema(self):
        metadata = planners()

        self.assertEqual(metadata[0].id, "astar_3d")
        self.assertIn("grid_resolution_m", metadata[0].parameters)

    def test_astar_routes_around_active_scene_building(self):
        request = PlanningRequest(
            start=EnuPoint(x=10, y=50, z=20),
            goal=EnuPoint(x=90, y=50, z=20),
            max_altitude_m=35,
            safety_clearance_m=1,
            parameters={"grid_resolution_m": 10, "path_smoothing": True},
        )

        result = plan_trajectory(request, self.scene_path)

        self.assertEqual(result.status, "success")
        self.assertGreater(len(result.path), 2)
        self.assertGreater(result.standard_metrics["path_length_m"], 80)
        self.assertGreater(result.diagnostics["expanded_nodes"], 0)

    def test_rejects_unknown_algorithm_parameter(self):
        request = PlanningRequest(
            start=EnuPoint(x=10, y=10, z=20),
            goal=EnuPoint(x=90, y=90, z=20),
            parameters={"private_objective": 1},
        )

        with self.assertRaisesRegex(ValueError, "Unsupported planner parameters"):
            plan_trajectory(request, self.scene_path)


if __name__ == "__main__":
    unittest.main()
