import json
import math
import os
import random
import tempfile
import threading
from collections import defaultdict
from pathlib import Path

import numpy as np

from phy.a2a import (
    DISTANCE_BINS_M,
    calibration_root,
    distance_bin,
    path_gain,
    profile_fingerprint,
    profile_settings,
)
from phy.sionna_rt import SionnaWorkerClient
from scene.airspace import Airspace
from utils import config


CALIBRATION_BATCH_NODES = 8


class CalibrationCancelled(RuntimeError):
    pass


def _conformal_interval(values, coverage):
    values = np.asarray(values, dtype=float)
    if not len(values):
        raise ValueError("Calibration group has no samples")
    center = float(np.median(values))
    tail = (1.0 - float(coverage)) / 2.0
    rank = math.ceil((len(values) + 1) * (1.0 - tail))
    if rank > len(values):
        raise ValueError(
            "Not enough LoS/NLoS samples for the requested coverage; increase Sample links"
        )
    lower_scores = np.sort(np.maximum(center - values, 0.0))
    upper_scores = np.sort(np.maximum(values - center, 0.0))
    return max(0.0, center - float(lower_scores[rank - 1])), center + float(upper_scores[rank - 1])


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


class CalibrationRuntime:
    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self.status = "idle"
        self.progress = 0
        self.stage = None
        self.error = None
        self.profile = None

    def start(self, settings):
        if self.status in {"queued", "running"}:
            raise RuntimeError("Calibration is already running")
        self.status = "queued"
        self.progress = 0
        self.stage = "Sampling static links"
        self.error = None
        self.profile = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(settings,), daemon=True)
        self._thread.start()

    def _run(self, settings):
        client = None
        try:
            self.status = "running"
            scene_path = Path(config.SIONNA_SCENE_PATH).with_name("scene.json")
            airspace = Airspace.from_file(
                scene_path,
                max_height=config.MAP_HEIGHT,
                building_clearance=config.UAV_BUILDING_CLEARANCE,
                boundary_clearance=config.UAV_BOUNDARY_CLEARANCE,
                min_flight_height=settings["uav_min_altitude_m"],
                max_flight_height=settings["uav_max_altitude_m"],
            )
            domain = {
                "min_altitude_m": settings["uav_min_altitude_m"],
                "max_altitude_m": settings["uav_max_altitude_m"],
                "target_links": settings["calibration_links"],
                "sampling": "uniform_free_airspace_v1",
                "batch_nodes": CALIBRATION_BATCH_NODES,
                "coverage": settings["calibration_coverage"],
            }
            fingerprint = profile_fingerprint(settings["los_a2a_model"], settings["nlos_a2a_model"], domain)
            ratios = {"los": defaultdict(list), "nlos": defaultdict(list)}
            paired = []
            target_links = settings["calibration_links"]
            client = SionnaWorkerClient()
            identifiers = list(range(CALIBRATION_BATCH_NODES))
            links_per_batch = CALIBRATION_BATCH_NODES * (CALIBRATION_BATCH_NODES - 1)
            total_batches = math.ceil(target_links / links_per_batch)
            for batch_index in range(total_batches):
                if self._stop.is_set():
                    raise CalibrationCancelled()
                positions = airspace.random_positions(
                    settings["seed"] + batch_index * CALIBRATION_BATCH_NODES,
                    CALIBRATION_BATCH_NODES,
                    config.UAV_INITIAL_SEPARATION,
                )
                self.stage = f"Static link batch {batch_index + 1}/{total_batches}"
                result = client.solve(
                    {identifier: positions[identifier] for identifier in identifiers},
                    {identifier: positions[identifier] for identifier in identifiers},
                )
                matrix = np.asarray(result["gains"], dtype=float)
                batch_pairs = [
                    (transmitter, receiver)
                    for transmitter in identifiers
                    for receiver in identifiers
                    if transmitter != receiver
                ]
                random.Random(settings["seed"] + batch_index).shuffle(batch_pairs)
                for transmitter, receiver in batch_pairs:
                    if len(paired) >= target_links:
                        break
                    tx_position = positions[transmitter]
                    rx_position = positions[receiver]
                    distance = math.dist(tx_position, rx_position)
                    los = airspace.has_line_of_sight(tx_position, rx_position)
                    model = settings["los_a2a_model"] if los else settings["nlos_a2a_model"]
                    nominal = path_gain(distance, config.CARRIER_FREQUENCY, model, los)
                    rt_gain = max(0.0, float(matrix[transmitter, receiver]))
                    ratio = rt_gain / nominal
                    kind = "los" if los else "nlos"
                    bin_id = distance_bin(distance)
                    ratios[kind][bin_id].append(ratio)
                    ratios[kind]["global"].append(ratio)
                    paired.append((
                        batch_index,
                        transmitter,
                        receiver,
                        *tx_position,
                        *rx_position,
                        distance,
                        int(los),
                        rt_gain,
                    ))
                self.progress = min(99, round(100 * (batch_index + 1) / total_batches))

            intervals = {"los": {}, "nlos": {}}
            for kind in intervals:
                global_interval = _conformal_interval(ratios[kind]["global"], settings["calibration_coverage"])
                intervals[kind]["global"] = {"lower": global_interval[0], "upper": global_interval[1], "samples": len(ratios[kind]["global"])}
                for index in range(len(DISTANCE_BINS_M)):
                    values = ratios[kind][str(index)]
                    try:
                        chosen = _conformal_interval(values, settings["calibration_coverage"])
                    except ValueError:
                        chosen = global_interval
                    intervals[kind][str(index)] = {"lower": chosen[0], "upper": chosen[1], "samples": len(values)}

            output = calibration_root() / fingerprint
            profile = {
                "fingerprint": fingerprint,
                "settings": profile_settings(settings["los_a2a_model"], settings["nlos_a2a_model"], domain),
                "intervals": intervals,
            }
            _atomic_json(output / "interval-table.json", profile)
            _atomic_json(output / "manifest.json", {**profile["settings"], "fingerprint": fingerprint})
            _atomic_json(output / "summary.json", {
                "fingerprint": fingerprint,
                "paired_samples": len(paired),
                "coverage": settings["calibration_coverage"],
                "groups": {
                    kind: {bin_id: value["samples"] for bin_id, value in table.items()}
                    for kind, table in intervals.items()
                },
            })
            output.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(output / "paired-samples.npz", samples=np.asarray(paired, dtype=float))
            self.profile = {"fingerprint": fingerprint, "path": str(output), "samples": len(paired)}
            self.progress = 100
            self.stage = "Calibration ready"
            self.status = "completed"
        except CalibrationCancelled:
            self.status = "cancelled"
            self.stage = "Calibration cancelled"
        except BaseException as error:
            self.error = f"{type(error).__name__}: {error}"
            self.stage = "Calibration failed"
            self.status = "failed"
        finally:
            if client is not None:
                client.close()

    def state(self):
        return {
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "error": self.error,
            "profile": self.profile,
        }


calibration_runtime = CalibrationRuntime()
