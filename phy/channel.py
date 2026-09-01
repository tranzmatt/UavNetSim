import copy
import itertools
import math
from dataclasses import dataclass

import simpy

from phy.sionna_rt import (
    A2AChannelModel,
    HybridSionnaRtChannelModel,
    OnDemandChannelModel,
    OnlineSionnaRtChannelModel,
)
from utils import config


@dataclass(slots=True)
class Transmission:
    identifier: int
    packet: object
    transmitter_id: int
    receiver_ids: tuple[int, ...]
    channel_id: int
    power_watt: float
    start_time: float
    end_time: float


@dataclass(slots=True)
class Reception:
    packet: object
    transmitter_id: int
    sinr_db: float
    signal_dbm: float


class Channel:
    def __init__(self, env, simulator):
        self.env = env
        self.simulator = simulator
        self.inboxes = {}
        self.transmissions = []
        self._identifiers = itertools.count(1)
        if config.CHANNEL_MODE == "online":
            self.channel_model = OnlineSionnaRtChannelModel(simulator.event_bus)
        elif config.CHANNEL_MODE == "hybrid":
            self.channel_model = HybridSionnaRtChannelModel(
                simulator.event_bus,
                simulator.airspace,
                config.LOS_A2A_MODEL,
            )
        elif config.CHANNEL_MODE == "a2a":
            self.channel_model = A2AChannelModel(
                simulator.event_bus,
                simulator.airspace,
                config.LOS_A2A_MODEL,
                config.NLOS_A2A_MODEL,
            )
        elif config.CHANNEL_MODE == "on_demand":
            self.channel_model = OnDemandChannelModel(
                simulator.event_bus,
                simulator.airspace,
                config.LOS_A2A_MODEL,
                config.NLOS_A2A_MODEL,
                config.CALIBRATION_PROFILE,
            )
        else:
            raise ValueError(f"Unsupported channel mode: {config.CHANNEL_MODE}")

    def create_inbox_for_receiver(self, identifier):
        inbox = simpy.Store(self.env)
        self.inboxes[identifier] = inbox
        return inbox

    def transmit(self, packet, transmitter_id, receiver_ids):
        duration = packet.packet_length / config.BIT_RATE * 1e6
        transmission = Transmission(
            identifier=next(self._identifiers),
            packet=copy.copy(packet),
            transmitter_id=transmitter_id,
            receiver_ids=tuple(receiver_ids),
            channel_id=packet.channel_id,
            power_watt=config.TRANSMITTING_POWER,
            start_time=self.env.now,
            end_time=self.env.now + duration,
        )
        self._prune()
        self.transmissions.append(transmission)
        self.simulator.event_bus.publish(
            "packet_tx_started",
            self.env.now,
            transmission_id=transmission.identifier,
            packet_id=packet.packet_id,
            packet_type=type(packet).__name__,
            source=transmitter_id,
            destinations=list(receiver_ids),
            channel=packet.channel_id,
            duration_us=duration,
        )
        for receiver_id in receiver_ids:
            if receiver_id != transmitter_id:
                self.env.process(self._deliver(transmission, receiver_id))

    def _prune(self):
        retention = 2 * (config.AVERAGE_PAYLOAD_LENGTH + config.PHY_HEADER_LENGTH) / config.BIT_RATE * 1e6
        cutoff = self.env.now - retention
        self.transmissions = [item for item in self.transmissions if item.end_time >= cutoff]

    @staticmethod
    def _overlap(first, second):
        return max(0.0, min(first.end_time, second.end_time) - max(first.start_time, second.start_time))

    @staticmethod
    def _dbm(power_watt):
        if power_watt <= 0:
            return -200.0
        return 10 * math.log10(power_watt * 1000)

    def _channel_overlaps(self, first_channel, second_channel):
        return abs(first_channel - second_channel) < 5

    def _estimates(self, transmitter_ids, receiver_id):
        return self.channel_model.estimates(
            self.env.now,
            self.simulator.drones,
            transmitter_ids,
            [receiver_id],
        )

    @staticmethod
    def _crosses_zero(lower, upper):
        return lower < 0 <= upper

    def _resolve_if_needed(self, lower, upper, pairs):
        if not self._crosses_zero(lower, upper):
            return False
        resolver = getattr(self.channel_model, "resolve_rt", None)
        if resolver is None:
            return False
        resolver(self.env.now, self.simulator.drones, pairs)
        return True

    def _evaluate(self, target, receiver_id):
        target_duration = target.end_time - target.start_time
        overlapping = []
        for candidate in self.transmissions:
            if candidate.identifier == target.identifier:
                continue
            if not self._channel_overlaps(target.channel_id, candidate.channel_id):
                continue
            overlap = self._overlap(target, candidate)
            if overlap <= 0:
                continue
            overlapping.append((candidate, overlap / target_duration))
        transmitter_ids = [target.transmitter_id]
        transmitter_ids.extend(candidate.transmitter_id for candidate, _ in overlapping)
        estimates = self._estimates(transmitter_ids, receiver_id)
        desired_pair = (target.transmitter_id, receiver_id)
        desired = estimates[desired_pair]
        beta = 10 ** (config.SINR_THRESHOLD_DB / 10)
        lower_margin = target.power_watt * desired.lower - beta * config.noise_power_watt()
        upper_margin = target.power_watt * desired.upper - beta * config.noise_power_watt()
        relevant_pairs = [desired_pair]
        for candidate, overlap_ratio in overlapping:
            if candidate.transmitter_id == receiver_id:
                lower_gain = upper_gain = 1.0
            else:
                pair = (candidate.transmitter_id, receiver_id)
                relevant_pairs.append(pair)
                estimate = estimates[pair]
                lower_gain, upper_gain = estimate.lower, estimate.upper
            coefficient = beta * candidate.power_watt * overlap_ratio
            lower_margin -= coefficient * upper_gain
            upper_margin -= coefficient * lower_gain
        if self._resolve_if_needed(lower_margin, upper_margin, relevant_pairs):
            estimates = self._estimates(transmitter_ids, receiver_id)
            desired = estimates[desired_pair]
        desired_gain = desired.nominal
        signal_power = target.power_watt * desired_gain
        interference_power = 0.0
        interferers = []
        for candidate, overlap_ratio in overlapping:
            gain = (
                1.0
                if candidate.transmitter_id == receiver_id
                else estimates[(candidate.transmitter_id, receiver_id)].nominal
            )
            contribution = candidate.power_watt * gain * overlap_ratio
            interference_power += contribution
            interferers.append({
                "node_id": candidate.transmitter_id,
                "overlap_ratio": overlap_ratio,
                "power_dbm": self._dbm(contribution),
            })
        denominator = config.noise_power_watt() + interference_power
        sinr_db = 10 * math.log10(signal_power / denominator) if signal_power > 0 else -200.0
        return sinr_db, self._dbm(signal_power), interferers

    def _deliver(self, transmission, receiver_id):
        yield self.env.timeout(transmission.end_time - self.env.now)
        sinr_db, signal_dbm, interferers = self._evaluate(transmission, receiver_id)
        success = sinr_db >= config.SINR_THRESHOLD_DB
        self.simulator.metrics.record_phy_result(success, bool(interferers))
        event_type = "packet_rx_succeeded" if success else "packet_rx_failed"
        self.simulator.event_bus.publish(
            event_type,
            self.env.now,
            transmission_id=transmission.identifier,
            packet_id=transmission.packet.packet_id,
            packet_type=type(transmission.packet).__name__,
            source=transmission.transmitter_id,
            destination=receiver_id,
            channel=transmission.channel_id,
            sinr_db=sinr_db,
            signal_dbm=signal_dbm,
            interferers=interferers,
            reason=None if success else "sinr_below_threshold",
        )
        if success:
            reception = Reception(
                packet=copy.copy(transmission.packet),
                transmitter_id=transmission.transmitter_id,
                sinr_db=sinr_db,
                signal_dbm=signal_dbm,
            )
            yield self.inboxes[receiver_id].put(reception)

    def current_transmitters(self, channel_id=None):
        result = []
        for transmission in self.transmissions:
            if transmission.start_time <= self.env.now < transmission.end_time:
                if channel_id is None or self._channel_overlaps(channel_id, transmission.channel_id):
                    result.append(transmission)
        return result

    def is_busy_for(self, drone, channel_id):
        transmissions = [
            transmission for transmission in self.current_transmitters(channel_id)
            if transmission.transmitter_id != drone.identifier
        ]
        if not transmissions:
            return False
        transmitter_ids = [transmission.transmitter_id for transmission in transmissions]
        estimates = self._estimates(transmitter_ids, drone.identifier)
        sensed_power = lower_power = upper_power = 0.0
        pairs = []
        for transmission in transmissions:
            pair = (transmission.transmitter_id, drone.identifier)
            pairs.append(pair)
            estimate = estimates[pair]
            sensed_power += transmission.power_watt * estimate.nominal
            lower_power += transmission.power_watt * estimate.lower
            upper_power += transmission.power_watt * estimate.upper
        threshold = 10 ** ((config.CCA_THRESHOLD_DBM - 30) / 10)
        if self._resolve_if_needed(lower_power - threshold, upper_power - threshold, pairs):
            estimates = self._estimates(transmitter_ids, drone.identifier)
            sensed_power = sum(
                transmission.power_watt
                * estimates[(transmission.transmitter_id, drone.identifier)].nominal
                for transmission in transmissions
            )
        return self._dbm(sensed_power) >= config.CCA_THRESHOLD_DBM

    def point_to_point_sinr(self, receiver_id, transmitter_id, channel_id):
        transmissions = [
            transmission for transmission in self.current_transmitters(channel_id)
            if transmission.transmitter_id != transmitter_id
        ]
        transmitter_ids = [transmitter_id]
        transmitter_ids.extend(transmission.transmitter_id for transmission in transmissions)
        estimates = self._estimates(transmitter_ids, receiver_id)
        desired_pair = (transmitter_id, receiver_id)
        desired = estimates[desired_pair]
        beta = 10 ** (config.SINR_THRESHOLD_DB / 10)
        lower_margin = config.TRANSMITTING_POWER * desired.lower - beta * config.noise_power_watt()
        upper_margin = config.TRANSMITTING_POWER * desired.upper - beta * config.noise_power_watt()
        pairs = [desired_pair]
        for transmission in transmissions:
            if transmission.transmitter_id == receiver_id:
                lower_gain = upper_gain = 1.0
            else:
                pair = (transmission.transmitter_id, receiver_id)
                pairs.append(pair)
                estimate = estimates[pair]
                lower_gain, upper_gain = estimate.lower, estimate.upper
            lower_margin -= beta * transmission.power_watt * upper_gain
            upper_margin -= beta * transmission.power_watt * lower_gain
        if self._resolve_if_needed(lower_margin, upper_margin, pairs):
            estimates = self._estimates(transmitter_ids, receiver_id)
        gain = estimates[desired_pair].nominal
        signal = config.TRANSMITTING_POWER * gain
        interference = 0.0
        for transmission in transmissions:
            other_gain = (
                1.0
                if transmission.transmitter_id == receiver_id
                else estimates[(transmission.transmitter_id, receiver_id)].nominal
            )
            interference += transmission.power_watt * other_gain
        if signal <= 0:
            return -200.0
        return 10 * math.log10(signal / (config.noise_power_watt() + interference))

    def close(self):
        self.channel_model.close()
