import math
import multiprocessing
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from phy.a2a import CalibrationProfile, GainEstimate, path_gain
from phy.sionna_worker import run_worker
from utils import config

class SionnaWorkerClient:
    def __init__(self):
        scene_path = Path(config.SIONNA_SCENE_PATH)
        if not scene_path.is_file():
            raise FileNotFoundError(f"Sionna scene does not exist: {scene_path}")
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe()
        self._process = context.Process(target=run_worker, args=(child_connection,), daemon=True)
        self._process.start()
        child_connection.close()
        self._connection = parent_connection
        self._connection.send({
            "type": "configure",
            "scene_path": str(scene_path.resolve()),
            "frequency_hz": config.CARRIER_FREQUENCY,
        })
        self._receive()

    def _receive(self):
        response = self._connection.recv()
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return response.get("result")

    def solve(self, transmitter_positions, receiver_positions):
        self._connection.send({
            "type": "snapshot",
            "transmitters": [
                {"id": int(identifier), "position": [float(value) for value in position]}
                for identifier, position in transmitter_positions.items()
            ],
            "receivers": [
                {"id": int(identifier), "position": [float(value) for value in position]}
                for identifier, position in receiver_positions.items()
            ],
            "max_depth": config.SIONNA_MAX_DEPTH,
            "samples_per_source": config.SIONNA_SAMPLES_PER_SOURCE,
            "bandwidth_hz": config.BANDWIDTH,
            "frequency_samples": config.SIONNA_FREQUENCY_SAMPLES,
            "seed": config.SIONNA_SEED,
            "los": config.SIONNA_LOS,
            "specular_reflection": config.SIONNA_SPECULAR_REFLECTION,
            "diffuse_reflection": config.SIONNA_DIFFUSE_REFLECTION,
            "refraction": config.SIONNA_REFRACTION,
            "diffraction": config.SIONNA_DIFFRACTION,
            "edge_diffraction": config.SIONNA_EDGE_DIFFRACTION,
        })
        return self._receive()

    def close(self):
        if self._process is None:
            return
        process = self._process
        if process.is_alive():
            try:
                self._connection.send({"type": "stop"})
            except (BrokenPipeError, EOFError, OSError):
                pass
            process.join(timeout=5)
        self._connection.close()
        process.close()
        self._process = None


@dataclass(slots=True)
class LinkSnapshot:
    gain: float
    sim_time_us: float
    transmitter_position: tuple[float, float, float]
    receiver_position: tuple[float, float, float]


class OnlineSionnaRtChannelModel:
    def __init__(self, event_bus, client=None):
        self.event_bus = event_bus
        self._client = client or SionnaWorkerClient()
        self._links = {}

    @staticmethod
    def _positions(drones):
        return {
            drone.identifier: tuple(float(value) for value in drone.coords)
            for drone in drones
        }

    @staticmethod
    def _unique(identifiers):
        return list(dict.fromkeys(int(identifier) for identifier in identifiers))

    def _is_stale(self, snapshot, sim_time_us, transmitter_position, receiver_position):
        return (
            snapshot is None
            or sim_time_us - snapshot.sim_time_us >= config.CHANNEL_SNAPSHOT_INTERVAL
            or math.dist(snapshot.transmitter_position, transmitter_position)
            >= config.CHANNEL_SNAPSHOT_DISPLACEMENT
            or math.dist(snapshot.receiver_position, receiver_position)
            >= config.CHANNEL_SNAPSHOT_DISPLACEMENT
        )

    def gains(self, sim_time_us, drones, transmitter_ids, receiver_ids):
        transmitter_ids = self._unique(transmitter_ids)
        receiver_ids = self._unique(receiver_ids)
        positions = self._positions(drones)
        requested_pairs = [
            (transmitter_id, receiver_id)
            for transmitter_id in transmitter_ids
            for receiver_id in receiver_ids
            if transmitter_id != receiver_id
        ]
        stale_pairs = [
            pair for pair in requested_pairs
            if self._is_stale(
                self._links.get(pair),
                sim_time_us,
                positions[pair[0]],
                positions[pair[1]],
            )
        ]
        if stale_pairs:
            stale_transmitters = self._unique(pair[0] for pair in stale_pairs)
            stale_receivers = self._unique(pair[1] for pair in stale_pairs)
            result = self._client.solve(
                {identifier: positions[identifier] for identifier in stale_transmitters},
                {identifier: positions[identifier] for identifier in stale_receivers},
            )
            solved_gains = np.asarray(result["gains"], dtype=float)
            for tx_index, transmitter_id in enumerate(result["transmitter_ids"]):
                for rx_index, receiver_id in enumerate(result["receiver_ids"]):
                    if transmitter_id == receiver_id:
                        continue
                    self._links[(transmitter_id, receiver_id)] = LinkSnapshot(
                        gain=float(solved_gains[tx_index, rx_index]),
                        sim_time_us=float(sim_time_us),
                        transmitter_position=positions[transmitter_id],
                        receiver_position=positions[receiver_id],
                    )
            self.event_bus.publish(
                "channel_snapshot",
                sim_time_us,
                mode="online",
                solve_time_ms=result["solve_time_ms"],
                transmitter_count=len(stale_transmitters),
                receiver_count=len(stale_receivers),
                link_count=len(stale_pairs),
            )
        return {pair: self._links[pair].gain for pair in requested_pairs}

    def estimates(self, sim_time_us, drones, transmitter_ids, receiver_ids):
        return {
            pair: GainEstimate(gain, gain, gain, False)
            for pair, gain in self.gains(
                sim_time_us, drones, transmitter_ids, receiver_ids
            ).items()
        }

    def close(self):
        self._client.close()


@dataclass(slots=True)
class A2ALinkSnapshot:
    estimate: GainEstimate
    sim_time_us: float
    transmitter_position: tuple[float, float, float]
    receiver_position: tuple[float, float, float]
    resolved_by_rt: bool = False


class A2AChannelModel(OnlineSionnaRtChannelModel):
    def __init__(self, event_bus, airspace, los_model, nlos_model):
        self.event_bus = event_bus
        self.airspace = airspace
        self.los_model = los_model
        self.nlos_model = nlos_model
        self._links = {}

    def _estimate(self, transmitter_position, receiver_position):
        los = self.airspace.has_line_of_sight(transmitter_position, receiver_position)
        model = self.los_model if los else self.nlos_model
        gain = path_gain(
            math.dist(transmitter_position, receiver_position),
            config.CARRIER_FREQUENCY,
            model,
            los,
        )
        return GainEstimate(gain, gain, gain, los)

    def estimates(self, sim_time_us, drones, transmitter_ids, receiver_ids):
        transmitter_ids = self._unique(transmitter_ids)
        receiver_ids = self._unique(receiver_ids)
        positions = self._positions(drones)
        requested_pairs = [
            (transmitter_id, receiver_id)
            for transmitter_id in transmitter_ids
            for receiver_id in receiver_ids
            if transmitter_id != receiver_id
        ]
        stale_pairs = [
            pair for pair in requested_pairs
            if self._is_stale(
                self._links.get(pair),
                sim_time_us,
                positions[pair[0]],
                positions[pair[1]],
            )
        ]
        if not stale_pairs:
            return {pair: self._links[pair].estimate for pair in requested_pairs}

        for pair in stale_pairs:
            self._links[pair] = A2ALinkSnapshot(
                estimate=self._estimate(positions[pair[0]], positions[pair[1]]),
                sim_time_us=float(sim_time_us),
                transmitter_position=positions[pair[0]],
                receiver_position=positions[pair[1]],
            )
        if stale_pairs:
            self.event_bus.publish(
                "channel_snapshot",
                sim_time_us,
                mode="a2a",
                link_count=len(stale_pairs),
                los_link_count=sum(self._links[pair].estimate.line_of_sight for pair in stale_pairs),
                nlos_link_count=sum(not self._links[pair].estimate.line_of_sight for pair in stale_pairs),
                solve_time_ms=0.0,
            )
        return {pair: self._links[pair].estimate for pair in requested_pairs}

    def gains(self, sim_time_us, drones, transmitter_ids, receiver_ids):
        return {
            pair: estimate.nominal
            for pair, estimate in self.estimates(
                sim_time_us, drones, transmitter_ids, receiver_ids
            ).items()
        }

    def close(self):
        return


class HybridSionnaRtChannelModel(A2AChannelModel):
    def __init__(self, event_bus, airspace, los_model="free_space", client=None):
        super().__init__(event_bus, airspace, los_model, "urban")
        self._client = client

    def estimates(self, sim_time_us, drones, transmitter_ids, receiver_ids):
        transmitter_ids = self._unique(transmitter_ids)
        receiver_ids = self._unique(receiver_ids)
        positions = self._positions(drones)
        requested_pairs = [
            (transmitter_id, receiver_id)
            for transmitter_id in transmitter_ids
            for receiver_id in receiver_ids
            if transmitter_id != receiver_id
        ]
        stale_pairs = [
            pair for pair in requested_pairs
            if self._is_stale(
                self._links.get(pair),
                sim_time_us,
                positions[pair[0]],
                positions[pair[1]],
            )
        ]
        if not stale_pairs:
            return {pair: self._links[pair].estimate for pair in requested_pairs}

        los_pairs = []
        nlos_pairs = []
        for pair in stale_pairs:
            target = (
                los_pairs
                if self.airspace.has_line_of_sight(positions[pair[0]], positions[pair[1]])
                else nlos_pairs
            )
            target.append(pair)

        for pair in los_pairs:
            gain = path_gain(
                math.dist(positions[pair[0]], positions[pair[1]]),
                config.CARRIER_FREQUENCY,
                self.los_model,
                True,
            )
            self._links[pair] = A2ALinkSnapshot(
                estimate=GainEstimate(gain, gain, gain, True),
                sim_time_us=float(sim_time_us),
                transmitter_position=positions[pair[0]],
                receiver_position=positions[pair[1]],
            )

        solve_time_ms = 0.0
        if nlos_pairs:
            if self._client is None:
                self._client = SionnaWorkerClient()
            transmitters = self._unique(pair[0] for pair in nlos_pairs)
            receivers = self._unique(pair[1] for pair in nlos_pairs)
            result = self._client.solve(
                {identifier: positions[identifier] for identifier in transmitters},
                {identifier: positions[identifier] for identifier in receivers},
            )
            matrix = np.asarray(result["gains"], dtype=float)
            tx_indices = {
                int(identifier): index
                for index, identifier in enumerate(result["transmitter_ids"])
            }
            rx_indices = {
                int(identifier): index
                for index, identifier in enumerate(result["receiver_ids"])
            }
            for pair in nlos_pairs:
                gain = float(matrix[tx_indices[pair[0]], rx_indices[pair[1]]])
                self._links[pair] = A2ALinkSnapshot(
                    estimate=GainEstimate(gain, gain, gain, False),
                    sim_time_us=float(sim_time_us),
                    transmitter_position=positions[pair[0]],
                    receiver_position=positions[pair[1]],
                )
            solve_time_ms = float(result["solve_time_ms"])

        self.event_bus.publish(
            "channel_snapshot",
            sim_time_us,
            mode="hybrid",
            solve_time_ms=solve_time_ms,
            link_count=len(stale_pairs),
            los_link_count=len(los_pairs),
            nlos_link_count=len(nlos_pairs),
        )
        return {pair: self._links[pair].estimate for pair in requested_pairs}

    def close(self):
        if self._client is not None:
            self._client.close()


class OnDemandChannelModel(A2AChannelModel):
    def __init__(self, event_bus, airspace, los_model, nlos_model, profile_fingerprint, client=None):
        super().__init__(event_bus, airspace, los_model, nlos_model)
        self.profile = CalibrationProfile.load(profile_fingerprint)
        self._client = client

    def _estimate(self, transmitter_position, receiver_position):
        base = super()._estimate(transmitter_position, receiver_position)
        interval = self.profile.ratio_interval(
            base.line_of_sight,
            math.dist(transmitter_position, receiver_position),
        )
        return GainEstimate(
            nominal=base.nominal,
            lower=base.nominal * interval["lower"],
            upper=base.nominal * interval["upper"],
            line_of_sight=base.line_of_sight,
        )

    def estimates(self, sim_time_us, drones, transmitter_ids, receiver_ids):
        transmitter_ids = self._unique(transmitter_ids)
        receiver_ids = self._unique(receiver_ids)
        positions = self._positions(drones)
        requested_pairs = [
            (transmitter_id, receiver_id)
            for transmitter_id in transmitter_ids
            for receiver_id in receiver_ids
            if transmitter_id != receiver_id
        ]
        for pair in requested_pairs:
            snapshot = self._links.get(pair)
            exact_rt_match = (
                snapshot is not None
                and snapshot.resolved_by_rt
                and snapshot.transmitter_position == positions[pair[0]]
                and snapshot.receiver_position == positions[pair[1]]
            )
            if exact_rt_match:
                continue
            self._links[pair] = A2ALinkSnapshot(
                estimate=self._estimate(positions[pair[0]], positions[pair[1]]),
                sim_time_us=float(sim_time_us),
                transmitter_position=positions[pair[0]],
                receiver_position=positions[pair[1]],
            )
        return {pair: self._links[pair].estimate for pair in requested_pairs}

    def resolve_rt(self, sim_time_us, drones, pairs):
        pairs = list(dict.fromkeys((int(tx), int(rx)) for tx, rx in pairs if tx != rx))
        if not pairs:
            return
        positions = self._positions(drones)
        if self._client is None:
            self._client = SionnaWorkerClient()
        transmitters = self._unique(pair[0] for pair in pairs)
        receivers = self._unique(pair[1] for pair in pairs)
        result = self._client.solve(
            {identifier: positions[identifier] for identifier in transmitters},
            {identifier: positions[identifier] for identifier in receivers},
        )
        gains = np.asarray(result["gains"], dtype=float)
        tx_indices = {int(identifier): index for index, identifier in enumerate(result["transmitter_ids"])}
        rx_indices = {int(identifier): index for index, identifier in enumerate(result["receiver_ids"])}
        for pair in pairs:
            gain = float(gains[tx_indices[pair[0]], rx_indices[pair[1]]])
            previous = self._links.get(pair)
            los = previous.estimate.line_of_sight if previous else self.airspace.has_line_of_sight(positions[pair[0]], positions[pair[1]])
            self._links[pair] = A2ALinkSnapshot(
                estimate=GainEstimate(gain, gain, gain, los),
                sim_time_us=float(sim_time_us),
                transmitter_position=positions[pair[0]],
                receiver_position=positions[pair[1]],
                resolved_by_rt=True,
            )
        self.event_bus.publish(
            "channel_snapshot",
            sim_time_us,
            mode="on_demand",
            solve_time_ms=float(result["solve_time_ms"]),
            link_count=len(pairs),
            transmitter_count=len(transmitters),
            receiver_count=len(receivers),
        )

    def close(self):
        if self._client is not None:
            self._client.close()
