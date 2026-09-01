import asyncio
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from api.runtime import RunSettings, runtime
from path_planning import PlanningRequest, PlanningResult, available_planners, plan_trajectory
from phy.a2a import LOS_MODELS, NLOS_MODELS, calibration_root, profile_fingerprint
from phy.calibration import calibration_runtime
from routing.parameters import ROUTING_PARAMETER_DEFINITIONS, resolve_routing_parameters
from scene.compiler import compile_scene
from scene.models import GeoBounds, SceneModel
from scene.osm_importer import fetch_osm_scene
from scene.terrain import attach_terrain
from utils import config


class StartRequest(BaseModel):
    seed: int = 2025
    node_count: int = Field(default=8, ge=2, le=50)
    duration_seconds: float = Field(default=20, gt=0, le=3600)
    playback_speed: float = Field(default=1, gt=0, le=100)
    uav_speed_mps: float = Field(default=10, gt=0, le=100)
    uav_min_altitude_m: float | None = Field(default=None, ge=0, le=10000)
    uav_max_altitude_m: float | None = Field(default=None, gt=0, le=10000)
    initial_energy_j: float = Field(default=20000, gt=0, le=1e9)
    traffic_pattern: str = Field(default="Poisson", pattern="^(Uniform|Poisson)$")
    packet_arrival_rate: float = Field(default=5, gt=0, le=1000)
    routing: str = "Greedy"
    routing_parameters: dict[str, float] = Field(default_factory=dict)
    mac: str = "CSMA_CA"
    mobility: str = "GaussMarkov3D"
    channel_mode: Literal["online", "hybrid", "on_demand", "a2a"] = "online"
    los_a2a_model: Literal["free_space", "log_distance"] = "free_space"
    nlos_a2a_model: Literal["urban", "suburban"] = "urban"
    calibration_profile: str | None = None
    samples_per_source: int = Field(default=100000, ge=100, le=10000000)
    sionna_max_depth: int = Field(default=4, ge=0, le=32)
    sionna_frequency_samples: int = Field(default=32, ge=1, le=4096)
    sionna_los: bool = True
    sionna_specular_reflection: bool = True
    sionna_diffuse_reflection: bool = False
    sionna_refraction: bool = False
    sionna_diffraction: bool = False
    sionna_edge_diffraction: bool = False
    channel_snapshot_interval_ms: float = Field(default=100, gt=0, le=60000)
    channel_snapshot_displacement_m: float = Field(default=1, gt=0, le=1000)
    calibration_links: int = Field(default=5000, ge=100, le=1000000)
    calibration_coverage: float = Field(default=0.95, ge=0.8, lt=1.0)

    @model_validator(mode="after")
    def validate_altitude_range(self):
        if (
            self.uav_min_altitude_m is not None
            and self.uav_max_altitude_m is not None
            and self.uav_min_altitude_m >= self.uav_max_altitude_m
        ):
            raise ValueError("Maximum UAV altitude must be greater than minimum UAV altitude")
        return self


class CalibrationRequest(StartRequest):
    pass


class OsmImportRequest(BaseModel):
    name: str = "OSM Scene"
    bounds: GeoBounds


class SceneBuildState(BaseModel):
    id: str
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    progress: int = Field(default=0, ge=0, le=100)
    stage: str = "Queued"
    error: str | None = None
    scene: SceneModel | None = None


app = FastAPI(title="UavNetSim v2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
scene_builds: dict[str, SceneBuildState] = {}
scene_build_lock = Lock()


def _activate_scene(scene, progress=None, asset_version=None):
    output = config.PROJECT_ROOT / "artifacts" / "scene"
    compile_scene(scene, output, progress, asset_version=asset_version)
    config.SIONNA_SCENE_PATH = str(output / "scene.xml")
    config.MAP_LENGTH = scene.size_x
    config.MAP_WIDTH = scene.size_y
    terrain_height = max((point.z for point in scene.terrain.vertices), default=0.0) if scene.terrain else 0.0
    config.MAP_HEIGHT = terrain_height + config.AIRSPACE_HEIGHT_ABOVE_TERRAIN
    return SceneModel.model_validate_json((output / "scene.json").read_text(encoding="utf-8"))


def _update_scene_build(build_id, **changes):
    with scene_build_lock:
        scene_builds[build_id] = scene_builds[build_id].model_copy(update=changes)


def _scene_build_in_progress():
    with scene_build_lock:
        return any(build.status in {"queued", "running"} for build in scene_builds.values())


def _run_scene_build(build_id, request):
    try:
        _update_scene_build(build_id, status="running", progress=2, stage="Starting scene build")

        def import_progress(value, stage):
            _update_scene_build(build_id, progress=5 + round(value * 25), stage=stage)

        scene = fetch_osm_scene(request.bounds, request.name, import_progress)

        def terrain_progress(value, stage):
            _update_scene_build(build_id, progress=30 + round(value * 25), stage=stage)

        scene = attach_terrain(scene, terrain_progress)

        def compile_progress(value, stage):
            _update_scene_build(build_id, progress=55 + round(value * 43), stage=stage)

        scene = _activate_scene(scene, compile_progress, asset_version=f"build-{build_id}")
        _update_scene_build(
            build_id,
            status="completed",
            progress=100,
            stage="Scene ready",
            scene=scene,
        )
    except Exception as error:
        _update_scene_build(build_id, status="failed", stage="Scene build failed", error=str(error))


def _scene_path():
    path = config.PROJECT_ROOT / "artifacts" / "scene" / "scene.json"
    if not path.is_file():
        default_scene = config.PROJECT_ROOT / "scenarios" / "default_scene.json"
        scene = SceneModel.model_validate_json(default_scene.read_text(encoding="utf-8"))
        _activate_scene(scene)
    return path


@app.get("/api/options")
def options():
    return {
        "routing": ["Greedy", "DSDV", "GRAD", "OPAR", "QRouting", "QFANET", "QGeo", "QMR", "Baseline_DRL"],
        "routing_parameters": ROUTING_PARAMETER_DEFINITIONS,
        "mac": ["CSMA_CA", "Pure_Aloha", "TDMA"],
        "mobility": ["GaussMarkov3D", "RandomWalk3D", "RandomWaypoint3D"],
        "traffic_pattern": ["Uniform", "Poisson"],
        "channel_mode": ["online", "hybrid", "on_demand", "a2a"],
        "los_a2a_model": list(LOS_MODELS),
        "nlos_a2a_model": list(NLOS_MODELS),
    }


@app.get("/api/planners")
def planners():
    return available_planners()


@app.post("/api/planning/plan", response_model=PlanningResult)
def create_plan(request: PlanningRequest):
    if _scene_build_in_progress():
        raise HTTPException(409, "Wait for the current scene build to finish")
    try:
        return plan_trajectory(request, _scene_path())
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@app.get("/api/scene", response_model=SceneModel)
def get_scene():
    return SceneModel.model_validate_json(_scene_path().read_text(encoding="utf-8"))


@app.post("/api/scene/import", response_model=SceneModel)
def import_scene(scene: SceneModel):
    if runtime.status in {"running", "paused", "starting", "preparing", "stopping"}:
        raise HTTPException(409, "Stop the simulation before changing the scene")
    if calibration_runtime.status in {"queued", "running"}:
        raise HTTPException(409, "Wait for calibration to finish before changing the scene")
    if _scene_build_in_progress():
        raise HTTPException(409, "Wait for the current scene build to finish")
    return _activate_scene(scene)


@app.post("/api/scene/osm", response_model=SceneBuildState, status_code=202)
def import_osm(request: OsmImportRequest, background_tasks: BackgroundTasks):
    if runtime.status in {"running", "paused", "starting", "preparing", "stopping"}:
        raise HTTPException(409, "Stop the simulation before changing the scene")
    if calibration_runtime.status in {"queued", "running"}:
        raise HTTPException(409, "Wait for calibration to finish before changing the scene")
    with scene_build_lock:
        if any(build.status in {"queued", "running"} for build in scene_builds.values()):
            raise HTTPException(409, "Another scene is already being built")
        build = SceneBuildState(id=uuid4().hex)
        scene_builds[build.id] = build
    background_tasks.add_task(_run_scene_build, build.id, request)
    return build


@app.get("/api/scene/osm/{build_id}", response_model=SceneBuildState)
def scene_build_state(build_id: str):
    with scene_build_lock:
        build = scene_builds.get(build_id)
    if build is None:
        raise HTTPException(404, "Scene build not found")
    return build


@app.post("/api/simulation/start")
def start_simulation(request: StartRequest):
    if _scene_build_in_progress():
        raise HTTPException(409, "Wait for the current scene build to finish")
    if calibration_runtime.status in {"queued", "running"}:
        raise HTTPException(409, "Wait for calibration to finish")
    try:
        _apply_channel_settings(request)
        if request.channel_mode == "on_demand":
            expected_profile = profile_fingerprint(
                request.los_a2a_model,
                request.nlos_a2a_model,
                _calibration_domain(request),
            )
            if request.calibration_profile != expected_profile:
                raise ValueError("On-demand mode requires a matching calibration profile")
            profile_path = calibration_root() / expected_profile / "interval-table.json"
            if not profile_path.is_file():
                raise ValueError("The selected calibration profile does not exist")
        settings = request.model_dump()
        settings.pop("calibration_links")
        settings.pop("calibration_coverage")
        settings["routing_parameters"] = resolve_routing_parameters(
            request.routing,
            request.routing_parameters,
        )
        runtime.start(RunSettings(**settings))
    except (RuntimeError, ValueError) as error:
        raise HTTPException(409, str(error)) from error
    return runtime.state()


def _apply_channel_settings(request):
    config.SIONNA_SAMPLES_PER_SOURCE = request.samples_per_source
    config.SIONNA_MAX_DEPTH = request.sionna_max_depth
    config.SIONNA_FREQUENCY_SAMPLES = request.sionna_frequency_samples
    config.SIONNA_LOS = request.sionna_los
    config.SIONNA_SPECULAR_REFLECTION = request.sionna_specular_reflection
    config.SIONNA_DIFFUSE_REFLECTION = request.sionna_diffuse_reflection
    config.SIONNA_REFRACTION = request.sionna_refraction
    config.SIONNA_DIFFRACTION = request.sionna_diffraction
    config.SIONNA_EDGE_DIFFRACTION = request.sionna_edge_diffraction
    config.CHANNEL_SNAPSHOT_INTERVAL = request.channel_snapshot_interval_ms * 1e3
    config.CHANNEL_SNAPSHOT_DISPLACEMENT = request.channel_snapshot_displacement_m
    config.MOBILITY_MODEL = request.mobility


def _calibration_domain(request):
    return {
        "min_altitude_m": request.uav_min_altitude_m,
        "max_altitude_m": request.uav_max_altitude_m,
        "target_links": request.calibration_links,
        "sampling": "uniform_free_airspace_v1",
        "batch_nodes": 8,
        "coverage": request.calibration_coverage,
    }


@app.post("/api/calibration/start", status_code=202)
def start_calibration(request: CalibrationRequest):
    if runtime.status in {"running", "paused", "starting", "stopping"}:
        raise HTTPException(409, "Stop the simulation before calibrating")
    if _scene_build_in_progress():
        raise HTTPException(409, "Wait for the current scene build to finish")
    try:
        _apply_channel_settings(request)
        calibration_runtime.start(request.model_dump())
    except (RuntimeError, ValueError) as error:
        raise HTTPException(409, str(error)) from error
    return calibration_runtime.state()


@app.get("/api/calibration/state")
def calibration_state():
    return calibration_runtime.state()


@app.post("/api/calibration/profile")
def calibration_profile(request: CalibrationRequest):
    if runtime.status in {"running", "paused", "starting", "stopping"}:
        raise HTTPException(409, "Calibration profiles cannot change during a simulation")
    _apply_channel_settings(request)
    fingerprint = profile_fingerprint(
        request.los_a2a_model,
        request.nlos_a2a_model,
        _calibration_domain(request),
    )
    path = calibration_root() / fingerprint / "interval-table.json"
    return {"fingerprint": fingerprint, "available": path.is_file()}


@app.post("/api/simulation/pause")
def pause_simulation():
    try:
        runtime.pause()
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return runtime.state()


@app.post("/api/simulation/resume")
def resume_simulation():
    try:
        runtime.resume()
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return runtime.state()


@app.post("/api/simulation/stop")
def stop_simulation():
    runtime.stop()
    return runtime.state()


@app.get("/api/simulation/state")
def simulation_state():
    return runtime.state()


@app.get("/api/events")
def events(after: int = 0):
    return runtime.event_bus.since(after)


@app.websocket("/api/ws")
async def event_stream(websocket: WebSocket):
    await websocket.accept()
    event_bus = runtime.event_bus
    run_id = runtime.run_id
    sequence = 0
    try:
        while True:
            if run_id != runtime.run_id:
                event_bus = runtime.event_bus
                run_id = runtime.run_id
                sequence = 0
            events = event_bus.since(sequence)
            if events:
                sequence = events[-1]["sequence"]
                await websocket.send_json({
                    "events": events,
                    "state": runtime.state(),
                })
            await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        return


frontend = config.PROJECT_ROOT / "frontend" / "dist"
artifacts = config.PROJECT_ROOT / "artifacts"
artifacts.mkdir(parents=True, exist_ok=True)


@app.get("/artifacts/{asset_path:path}")
def artifact_file(asset_path: str):
    candidate = (artifacts / asset_path).resolve()
    if artifacts not in candidate.parents or not candidate.is_file():
        raise HTTPException(404, "Artifact not found")
    return FileResponse(candidate, headers={"Cache-Control": "no-store"})


if frontend.is_dir():
    app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    @app.get("/{path:path}")
    def frontend_app(path: str):
        candidate = frontend / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(frontend / "index.html")
