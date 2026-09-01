import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from phy.a2a import GainEstimate, free_space_path_gain, path_gain
from phy.channel import Channel
from phy.sionna_rt import A2AChannelModel, HybridSionnaRtChannelModel, OnDemandChannelModel
from utils import config


class RecordingEventBus:
    def __init__(self):
        self.events = []

    def publish(self, event_type, sim_time_us, **data):
        self.events.append((event_type, sim_time_us, data))


class RecordingClient:
    def __init__(self, gain=0.25):
        self.gain = gain
        self.calls = []
        self.closed = False

    def solve(self, transmitter_positions, receiver_positions):
        self.calls.append((transmitter_positions, receiver_positions))
        transmitter_ids = list(transmitter_positions)
        receiver_ids = list(receiver_positions)
        return {
            "gains": [[self.gain for _ in receiver_ids] for _ in transmitter_ids],
            "transmitter_ids": transmitter_ids,
            "receiver_ids": receiver_ids,
            "solve_time_ms": 12.5,
        }

    def close(self):
        self.closed = True


class PairVisibility:
    def __init__(self, blocked_pairs=()):
        self.blocked_pairs = set(blocked_pairs)

    def has_line_of_sight(self, start, end):
        return (tuple(start), tuple(end)) not in self.blocked_pairs


class FixedProfile:
    fingerprint = "test"

    def ratio_interval(self, line_of_sight, distance_m):
        return {"lower": 0.5, "upper": 1.5}


def _drones():
    return [
        SimpleNamespace(identifier=0, coords=[0.0, 0.0, 10.0]),
        SimpleNamespace(identifier=1, coords=[10.0, 0.0, 10.0]),
        SimpleNamespace(identifier=2, coords=[20.0, 0.0, 10.0]),
    ]


class A2AChannelTests(unittest.TestCase):
    def test_free_space_model_matches_friis(self):
        self.assertTrue(math.isclose(
            path_gain(100.0, 2.4e9, "free_space", True),
            free_space_path_gain(100.0, 2.4e9),
            rel_tol=1e-12,
        ))

    @patch.object(config, "CARRIER_FREQUENCY", 2.4e9)
    def test_a2a_selects_los_and_nlos_models(self):
        drones = _drones()
        visibility = PairVisibility({(tuple(drones[1].coords), tuple(drones[2].coords))})
        model = A2AChannelModel(RecordingEventBus(), visibility, "free_space", "urban")

        estimates = model.estimates(0.0, drones, [0, 1], [2])

        self.assertTrue(estimates[(0, 2)].line_of_sight)
        self.assertFalse(estimates[(1, 2)].line_of_sight)
        self.assertGreater(estimates[(0, 2)].nominal, estimates[(1, 2)].nominal)
        self.assertEqual(estimates[(0, 2)].lower, estimates[(0, 2)].upper)

    @patch("phy.sionna_rt.CalibrationProfile.load", return_value=FixedProfile())
    def test_on_demand_uses_interval_then_collapses_resolved_links(self, _load):
        drones = _drones()
        client = RecordingClient(gain=0.25)
        model = OnDemandChannelModel(
            RecordingEventBus(), PairVisibility(), "free_space", "urban", "test", client
        )

        estimate = model.estimates(0.0, drones, [0], [2])[(0, 2)]
        self.assertEqual(estimate.lower, estimate.nominal * 0.5)
        self.assertEqual(estimate.upper, estimate.nominal * 1.5)
        self.assertEqual(client.calls, [])

        model.resolve_rt(0.0, drones, [(0, 2)])
        resolved = model.estimates(0.0, drones, [0], [2])[(0, 2)]
        self.assertEqual((resolved.nominal, resolved.lower, resolved.upper), (0.25, 0.25, 0.25))
        self.assertEqual(len(client.calls), 1)

        drones[0].coords[0] += 0.01
        moved = model.estimates(1.0, drones, [0], [2])[(0, 2)]
        self.assertNotEqual(moved.lower, moved.upper)
        self.assertEqual(moved.lower, moved.nominal * 0.5)

    @patch.object(config, "CARRIER_FREQUENCY", 2.4e9)
    def test_hybrid_uses_a2a_for_los_and_rt_only_for_nlos(self):
        drones = _drones()
        blocked = {(tuple(drones[1].coords), tuple(drones[2].coords))}
        client = RecordingClient(gain=0.25)
        event_bus = RecordingEventBus()
        model = HybridSionnaRtChannelModel(
            event_bus, PairVisibility(blocked), "free_space", client
        )

        estimates = model.estimates(0.0, drones, [0, 1], [2])

        self.assertTrue(math.isclose(
            estimates[(0, 2)].nominal,
            free_space_path_gain(20.0, 2.4e9),
            rel_tol=1e-12,
        ))
        self.assertEqual(estimates[(1, 2)].nominal, 0.25)
        self.assertEqual(list(client.calls[0][0]), [1])
        self.assertEqual(list(client.calls[0][1]), [2])
        self.assertEqual(event_bus.events[-1][2]["mode"], "hybrid")

    def test_cca_invokes_rt_only_when_margin_interval_crosses_zero(self):
        class EventModel:
            def __init__(self):
                self.crosses = False
                self.resolved = 0

            def estimates(self, _time, _drones, transmitter_ids, receiver_ids):
                pair = (transmitter_ids[0], receiver_ids[0])
                if self.resolved:
                    estimate = GainEstimate(1e-10, 1e-10, 1e-10, True)
                elif self.crosses:
                    estimate = GainEstimate(1e-10, 0.0, 2e-10, True)
                else:
                    estimate = GainEstimate(1e-13, 0.0, 1e-12, True)
                return {pair: estimate}

            def resolve_rt(self, _time, _drones, _pairs):
                self.resolved += 1

        channel = Channel.__new__(Channel)
        channel.env = SimpleNamespace(now=0.0)
        channel.simulator = SimpleNamespace(drones=_drones())
        channel.channel_model = EventModel()
        transmission = SimpleNamespace(transmitter_id=0, power_watt=0.1)
        channel.current_transmitters = lambda _channel_id: [transmission]

        self.assertFalse(channel.is_busy_for(_drones()[1], 1))
        self.assertEqual(channel.channel_model.resolved, 0)
        channel.channel_model.crosses = True
        self.assertTrue(channel.is_busy_for(_drones()[1], 1))
        self.assertEqual(channel.channel_model.resolved, 1)


if __name__ == "__main__":
    unittest.main()
