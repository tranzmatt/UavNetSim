import hashlib
import importlib.metadata
import json
import math
from dataclasses import dataclass
from pathlib import Path

from utils import config


SPEED_OF_LIGHT_M_S = 299_792_458.0
LOS_MODELS = {
    "free_space": {"path_loss_exponent": 2.0, "excess_loss_db": 0.0},
    "log_distance": {"path_loss_exponent": 2.1, "excess_loss_db": 1.0},
}
NLOS_MODELS = {
    "urban": {"path_loss_exponent": 3.0, "excess_loss_db": 12.0},
    "suburban": {"path_loss_exponent": 2.6, "excess_loss_db": 8.0},
}
DISTANCE_BINS_M = (50.0, 100.0, 200.0, 400.0, float("inf"))


def free_space_path_gain(distance_m, frequency_hz):
    distance_m = float(distance_m)
    frequency_hz = float(frequency_hz)
    if distance_m <= 0 or frequency_hz <= 0:
        raise ValueError("Distance and frequency must be positive")
    wavelength = SPEED_OF_LIGHT_M_S / frequency_hz
    return (wavelength / (4 * math.pi * distance_m)) ** 2


def path_gain(distance_m, frequency_hz, model, line_of_sight):
    models = LOS_MODELS if line_of_sight else NLOS_MODELS
    try:
        parameters = models[model]
    except KeyError as error:
        kind = "LoS" if line_of_sight else "NLoS"
        raise ValueError(f"Unsupported {kind} A2A model: {model}") from error
    reference_distance = 1.0
    reference_gain = free_space_path_gain(reference_distance, frequency_hz)
    distance = max(reference_distance, float(distance_m))
    return (
        reference_gain
        * (distance / reference_distance) ** (-parameters["path_loss_exponent"])
        * 10 ** (-parameters["excess_loss_db"] / 10)
    )


def distance_bin(distance_m):
    distance = float(distance_m)
    for index, upper_bound in enumerate(DISTANCE_BINS_M):
        if distance <= upper_bound:
            return str(index)
    return str(len(DISTANCE_BINS_M) - 1)


def rt_settings():
    scene_path = Path(config.SIONNA_SCENE_PATH)
    scene_digest = hashlib.sha256()
    for asset in (scene_path.with_name("scene.json"), scene_path):
        scene_digest.update(asset.name.encode("utf-8"))
        scene_digest.update(asset.read_bytes())
    return {
        "scene_sha256": scene_digest.hexdigest(),
        "sionna_rt_version": importlib.metadata.version("sionna-rt"),
        "carrier_frequency": config.CARRIER_FREQUENCY,
        "bandwidth": config.BANDWIDTH,
        "max_depth": config.SIONNA_MAX_DEPTH,
        "samples_per_source": config.SIONNA_SAMPLES_PER_SOURCE,
        "frequency_samples": config.SIONNA_FREQUENCY_SAMPLES,
        "seed": config.SIONNA_SEED,
        "los": config.SIONNA_LOS,
        "specular_reflection": config.SIONNA_SPECULAR_REFLECTION,
        "diffuse_reflection": config.SIONNA_DIFFUSE_REFLECTION,
        "refraction": config.SIONNA_REFRACTION,
        "diffraction": config.SIONNA_DIFFRACTION,
        "edge_diffraction": config.SIONNA_EDGE_DIFFRACTION,
    }


def profile_settings(los_model, nlos_model, calibration_domain):
    return {
        "version": 2,
        "rt": rt_settings(),
        "a2a": {"los_model": los_model, "nlos_model": nlos_model},
        "distance_bins_m": [None if math.isinf(value) else value for value in DISTANCE_BINS_M],
        "domain": calibration_domain,
    }


def profile_fingerprint(los_model, nlos_model, calibration_domain):
    payload = profile_settings(los_model, nlos_model, calibration_domain)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def calibration_root():
    return Path(config.SIONNA_SCENE_PATH).parent / "calibration"


@dataclass(frozen=True, slots=True)
class GainEstimate:
    nominal: float
    lower: float
    upper: float
    line_of_sight: bool


class CalibrationProfile:
    def __init__(self, payload):
        self.payload = payload
        self.fingerprint = payload["fingerprint"]
        self.intervals = payload["intervals"]

    @classmethod
    def load(cls, fingerprint):
        path = calibration_root() / fingerprint / "interval-table.json"
        if not path.is_file():
            raise ValueError("No matching calibration profile. Calibrate this scene and channel configuration first.")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("fingerprint") != fingerprint:
            raise ValueError("Calibration profile fingerprint is invalid")
        return cls(payload)

    def ratio_interval(self, line_of_sight, distance_m):
        kind = "los" if line_of_sight else "nlos"
        bins = self.intervals[kind]
        return bins.get(distance_bin(distance_m), bins["global"])
