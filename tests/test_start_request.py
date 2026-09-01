import unittest

from pydantic import ValidationError

from api.app import StartRequest, _calibration_domain


class StartRequestTests(unittest.TestCase):
    def test_accepts_valid_uav_altitude_range(self):
        request = StartRequest(uav_min_altitude_m=80.0, uav_max_altitude_m=160.0)

        self.assertEqual(request.uav_min_altitude_m, 80.0)
        self.assertEqual(request.uav_max_altitude_m, 160.0)

    def test_rejects_reversed_uav_altitude_range(self):
        with self.assertRaises(ValidationError):
            StartRequest(uav_min_altitude_m=160.0, uav_max_altitude_m=80.0)

    def test_offline_mode_is_removed(self):
        with self.assertRaises(ValidationError):
            StartRequest(channel_mode="offline")

    def test_accepts_new_a2a_modes(self):
        self.assertEqual(StartRequest(channel_mode="a2a").channel_mode, "a2a")
        self.assertEqual(StartRequest(channel_mode="on_demand").channel_mode, "on_demand")
        self.assertEqual(StartRequest(channel_mode="hybrid").channel_mode, "hybrid")

    def test_calibration_domain_is_independent_of_mobility_and_speed(self):
        first = StartRequest(
            node_count=4,
            seed=1,
            mobility="GaussMarkov3D",
            uav_speed_mps=5,
            uav_min_altitude_m=20,
            uav_max_altitude_m=120,
        )
        second = StartRequest(
            node_count=20,
            seed=99,
            mobility="RandomWaypoint3D",
            uav_speed_mps=50,
            uav_min_altitude_m=20,
            uav_max_altitude_m=120,
        )

        self.assertEqual(_calibration_domain(first), _calibration_domain(second))


if __name__ == "__main__":
    unittest.main()
