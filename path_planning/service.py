import heapq
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from path_planning.models import (
    PlannerMetadata,
    PlannerParameter,
    PlanningRequest,
    PlanningResult,
)
from scene.airspace import Airspace
from scene.models import EnuPoint
from utils import config


ASTAR_METADATA = PlannerMetadata(
    id="astar_3d",
    name="3D A*",
    description="Grid-based collision-free planning in the active 3D scene.",
    parameters={
        "grid_resolution_m": PlannerParameter(
            type="number",
            label="Grid resolution",
            default=20.0,
            minimum=2.0,
            maximum=100.0,
            step=1.0,
            unit="m",
            description="Distance between adjacent search nodes.",
        ),
        "heuristic_weight": PlannerParameter(
            type="number",
            label="Heuristic weight",
            default=1.0,
            minimum=0.0,
            maximum=5.0,
            step=0.1,
            description="Higher values search faster but may produce a longer path.",
        ),
        "path_smoothing": PlannerParameter(
            type="boolean",
            label="Path smoothing",
            default=True,
            description="Remove waypoints that are unnecessary for collision avoidance.",
        ),
    },
)


def _resolve_parameters(metadata: PlannerMetadata, supplied: dict[str, Any]) -> dict[str, Any]:
    unknown = set(supplied) - set(metadata.parameters)
    if unknown:
        raise ValueError(f"Unsupported planner parameters: {', '.join(sorted(unknown))}")
    resolved = {}
    for name, definition in metadata.parameters.items():
        value = supplied.get(name, definition.default)
        if definition.type == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"Planner parameter '{name}' must be a boolean")
        elif definition.type in {"number", "integer"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Planner parameter '{name}' must be numeric")
            value = int(value) if definition.type == "integer" else float(value)
            if definition.minimum is not None and value < definition.minimum:
                raise ValueError(f"Planner parameter '{name}' must be at least {definition.minimum}")
            if definition.maximum is not None and value > definition.maximum:
                raise ValueError(f"Planner parameter '{name}' must be at most {definition.maximum}")
        resolved[name] = value
    return resolved


def _path_length(path):
    return sum(math.dist(left, right) for left, right in zip(path, path[1:]))


def _smooth_path(path, airspace):
    if len(path) <= 2:
        return path
    smoothed = [path[0]]
    current = 0
    while current < len(path) - 1:
        candidate = len(path) - 1
        while candidate > current + 1:
            if airspace.path_is_free(path[current], path[candidate]):
                break
            candidate -= 1
        smoothed.append(path[candidate])
        current = candidate
    return smoothed


def _astar_plan(request: PlanningRequest, airspace: Airspace, parameters: dict[str, Any]):
    started = time.perf_counter()
    start = [request.start.x, request.start.y, request.start.z]
    goal = [request.goal.x, request.goal.y, request.goal.z]
    if not airspace.position_is_free(start):
        raise ValueError("Start is outside the available airspace or intersects an obstacle")
    if not airspace.position_is_free(goal):
        raise ValueError("Goal is outside the available airspace or intersects an obstacle")

    resolution = parameters["grid_resolution_m"]
    if airspace.path_is_free(start, goal):
        raw_path = [start, goal]
        expanded_nodes = 0
    else:
        minimum = (
            math.ceil(airspace.boundary_clearance / resolution),
            math.ceil(airspace.boundary_clearance / resolution),
            math.ceil(airspace.min_flight_height / resolution),
        )
        maximum = (
            math.floor((airspace.size_x - airspace.boundary_clearance) / resolution),
            math.floor((airspace.size_y - airspace.boundary_clearance) / resolution),
            math.floor(airspace.max_flight_height / resolution),
        )

        def position(index):
            return tuple(coordinate * resolution for coordinate in index)

        free_cache = {}

        def is_free(index):
            if not all(low <= value <= high for value, low, high in zip(index, minimum, maximum)):
                return False
            if index not in free_cache:
                free_cache[index] = airspace.position_is_free(position(index))
            return free_cache[index]

        candidates = []
        for ix in range(minimum[0], maximum[0] + 1):
            for iy in range(minimum[1], maximum[1] + 1):
                for iz in range(minimum[2], maximum[2] + 1):
                    index = (ix, iy, iz)
                    if is_free(index):
                        candidates.append(index)

        def connectors(endpoint):
            connected = []
            for index in sorted(candidates, key=lambda item: math.dist(position(item), endpoint)):
                distance = math.dist(position(index), endpoint)
                if len(connected) >= 12 or distance > resolution * 2.5:
                    break
                if airspace.path_is_free(endpoint, position(index)):
                    connected.append((index, distance))
            return connected

        starts = connectors(start)
        goals = dict(connectors(goal))
        if not starts or not goals:
            raise ValueError("No search grid node can be connected to the start or goal")

        directions = [
            (dx, dy, dz)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for dz in (-1, 0, 1)
            if (dx, dy, dz) != (0, 0, 0)
        ]
        frontier = []
        cost = {}
        parent = {}
        counter = 0
        weight = parameters["heuristic_weight"]
        for index, initial_cost in starts:
            cost[index] = initial_cost
            parent[index] = None
            priority = initial_cost + weight * math.dist(position(index), goal)
            heapq.heappush(frontier, (priority, counter, index))
            counter += 1

        reached = None
        reached_cost = math.inf
        expanded_nodes = 0
        while frontier:
            priority, _, current = heapq.heappop(frontier)
            expected = cost[current] + weight * math.dist(position(current), goal)
            if priority > expected + 1e-9:
                continue
            if cost[current] >= reached_cost:
                continue
            expanded_nodes += 1
            if current in goals:
                reached = current
                reached_cost = cost[current] + goals[current]
                break
            current_position = position(current)
            for delta in directions:
                neighbor = tuple(value + step for value, step in zip(current, delta))
                if not is_free(neighbor):
                    continue
                neighbor_position = position(neighbor)
                if not airspace.path_is_free(current_position, neighbor_position):
                    continue
                next_cost = cost[current] + math.dist(current_position, neighbor_position)
                if next_cost >= cost.get(neighbor, math.inf):
                    continue
                cost[neighbor] = next_cost
                parent[neighbor] = current
                next_priority = next_cost + weight * math.dist(neighbor_position, goal)
                heapq.heappush(frontier, (next_priority, counter, neighbor))
                counter += 1

        if reached is None:
            raise ValueError("No collision-free path was found with the current planner settings")
        indices = []
        while reached is not None:
            indices.append(reached)
            reached = parent[reached]
        indices.reverse()
        raw_path = [start, *(list(position(index)) for index in indices), goal]

    final_path = _smooth_path(raw_path, airspace) if parameters["path_smoothing"] else raw_path
    airspace.validate_path(final_path)
    distance = _path_length(final_path)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return PlanningResult(
        planner_id=ASTAR_METADATA.id,
        path=[EnuPoint(x=point[0], y=point[1], z=point[2]) for point in final_path],
        standard_metrics={
            "path_length_m": distance,
            "estimated_flight_time_s": distance / request.uav_speed_mps,
            "planning_time_ms": elapsed_ms,
            "waypoint_count": len(final_path),
        },
        diagnostics={
            "expanded_nodes": expanded_nodes,
            "raw_waypoint_count": len(raw_path),
            "grid_resolution_m": resolution,
        },
    )


_PLANNERS: dict[str, tuple[PlannerMetadata, Callable]] = {
    ASTAR_METADATA.id: (ASTAR_METADATA, _astar_plan),
}


def available_planners():
    return [entry[0] for entry in _PLANNERS.values()]


def plan_trajectory(request: PlanningRequest, scene_path: Path | None = None):
    try:
        metadata, planner = _PLANNERS[request.planner_id]
    except KeyError as error:
        raise ValueError(f"Unsupported planner: {request.planner_id}") from error
    parameters = _resolve_parameters(metadata, request.parameters)
    scene_path = scene_path or config.PROJECT_ROOT / "artifacts" / "scene" / "scene.json"
    airspace = Airspace.from_file(
        scene_path,
        max_height=config.MAP_HEIGHT,
        building_clearance=request.safety_clearance_m,
        boundary_clearance=max(0.1, request.safety_clearance_m),
        min_flight_height=request.min_altitude_m,
        max_flight_height=request.max_altitude_m,
    )
    return planner(request, airspace, parameters)
