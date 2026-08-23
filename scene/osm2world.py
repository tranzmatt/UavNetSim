"""OSM enrichment and optional OSM2World rendering.

The simulator uses the compact :class:`SceneModel` as its source of truth. This
module creates a standards-shaped OSM view of that model for visual rendering;
the generated file is deliberately not used to build the Sionna collision or
radio meshes.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

from scene.models import SceneFeature, SceneModel, SceneRendering


OSM_NAMESPACE = "http://openstreetmap.org/osm/0.6"
ET.register_namespace("", OSM_NAMESPACE)

MATERIAL_TAGS = {
    "itu_concrete": "concrete",
    "itu_brick": "brick",
    "itu_glass": "glass",
    "itu_metal": "metal",
    "itu_wood": "wood",
}
ROOF_MATERIALS = {
    "concrete": "concrete",
    "brick": "roof_tiles",
    "glass": "glass",
    "metal": "metal",
    "wood": "wood",
}
ROOF_SHAPES = {"flat", "gabled", "hipped", "pyramidal", "skillion", "mansard", "dome", "round"}


def _project_root():
    return Path(__file__).resolve().parents[1]


def _project_java():
    """Return the project-local Java runtime when it has been installed."""
    java_root = _project_root() / "tools" / "java"
    candidates = [java_root / "bin" / "java.exe", java_root / "bin" / "java"]
    candidates.extend(java_root.glob("*/bin/java.exe"))
    candidates.extend(java_root.glob("*/bin/java"))
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _project_osm2world_jars():
    package_root = _project_root() / "tools" / "osm2world"
    return sorted(
        (path.resolve() for path in package_root.rglob("*.jar") if path.is_file()),
        key=lambda path: ("osm2world" not in path.name.lower(), len(path.parts), path.name),
    )


def _java_command():
    configured_java = os.environ.get("JAVA")
    if configured_java:
        configured_path = Path(configured_java).expanduser()
        if configured_path.is_file():
            return str(configured_path.resolve())
        if shutil.which(configured_java):
            return configured_java
    bundled_java = _project_java()
    return str(bundled_java) if bundled_java else "java"


def _polygon_dimensions(feature: SceneFeature):
    points = [(point.x, point.y) for point in feature.footprint]
    if len(points) < 2:
        return 1.0, 1.0, 0.0
    edges = [
        (math.dist(start, end), start, end)
        for start, end in zip(points, points[1:] + points[:1])
    ]
    longest, start, end = max(edges, key=lambda item: item[0])
    shortest = max(1.0, min(edge[0] for edge in edges))
    direction = (math.degrees(math.atan2(end[0] - start[0], end[1] - start[1])) + 360.0) % 360.0
    return max(1.0, longest), shortest, direction


def _default_roof_shape(feature: SceneFeature):
    longest, shortest, _direction = _polygon_dimensions(feature)
    aspect_ratio = longest / shortest
    if feature.height >= 60.0:
        return "flat"
    if aspect_ratio >= 1.6:
        return "gabled"
    return "hipped"


def complete_osm_tags(feature: SceneFeature) -> dict[str, str]:
    """Return OSM tags while leaving unspecified visual materials to OSM2World."""
    tags = {str(key): str(value) for key, value in feature.osm_tags.items()}
    if feature.category == "building":
        material = MATERIAL_TAGS.get(feature.material)
        tags.setdefault("building", "yes")
        tags.setdefault("height", f"{max(feature.height, 1.0):.2f}")
        tags.setdefault("min_height", "0")
        levels = max(1, round(max(feature.height, 3.2) / 3.2))
        tags.setdefault("building:levels", str(levels))
        tags.setdefault("building:levels:aboveground", tags["building:levels"])
        if feature.source != "openstreetmap" and material:
            tags.setdefault("building:material", material)
            tags.setdefault("facade:material", material)

        roof_shape = tags.get("roof:shape", "").lower()
        if roof_shape not in ROOF_SHAPES:
            roof_shape = _default_roof_shape(feature)
        tags["roof:shape"] = roof_shape
        if feature.source != "openstreetmap" and material:
            tags.setdefault("roof:material", ROOF_MATERIALS.get(material, "concrete"))
        if roof_shape == "flat":
            tags.setdefault("roof:height", "0")
            tags.setdefault("roof:levels", "0")
        else:
            roof_height = min(6.0, max(2.0, feature.height * 0.16))
            tags.setdefault("roof:height", f"{roof_height:.2f}")
            tags.setdefault("roof:levels", "1")
            longest, shortest, direction = _polygon_dimensions(feature)
            tags.setdefault("roof:direction", f"{direction:.1f}")
            tags.setdefault("roof:orientation", "along" if longest / shortest >= 1.5 else "across")
            angle = math.degrees(math.atan2(roof_height, max(0.5, shortest / 2.0)))
            tags.setdefault("roof:angle", f"{min(45.0, max(5.0, angle)):.1f}")
        tags.setdefault("layer", "0")
    elif feature.category == "road":
        tags.setdefault("highway", "service")
        tags.setdefault("surface", "asphalt")
        tags.setdefault("lanes", "2")
        tags.setdefault("width", "8")
        tags.setdefault("layer", "0")
    elif feature.category == "water":
        tags.setdefault("natural", "water")
        tags.setdefault("water", "lake")
        tags.setdefault("surface", "water")
    elif feature.category == "terrain":
        tags.setdefault("natural", "grassland")
        tags.setdefault("surface", "grass")
    tags.setdefault("source", feature.source)
    return tags


def enrich_scene(scene: SceneModel) -> SceneModel:
    """Add OSM metadata without changing simulation geometry or material IDs."""
    features = [
        feature.model_copy(update={"osm_tags": complete_osm_tags(feature)})
        for feature in scene.features
    ]
    return scene.model_copy(update={"features": features, "rendering": None})


def _inverse_enu(x, y, scene: SceneModel):
    latitude = scene.anchor.latitude + y / 110574.0
    metres_per_degree_longitude = math.cos(math.radians(scene.anchor.latitude)) * 111320.0
    longitude = scene.anchor.longitude + x / metres_per_degree_longitude
    return latitude, longitude


def _node_id(index):
    return str(-1_000_000_000 - index)


def _way_id(index):
    return str(-2_000_000_000 - index)


def write_osm(scene: SceneModel, path) -> Path:
    """Write a valid OSM 0.6 XML view of the scene."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = ET.Element(
        f"{{{OSM_NAMESPACE}}}osm",
        {"version": "0.6", "generator": "UavNetSim OSM2World adapter"},
    )
    northeast_latitude, northeast_longitude = _inverse_enu(scene.size_x, scene.size_y, scene)
    ET.SubElement(root, f"{{{OSM_NAMESPACE}}}bounds", {
        "minlat": str(scene.anchor.latitude),
        "minlon": str(scene.anchor.longitude),
        "maxlat": str(northeast_latitude),
        "maxlon": str(northeast_longitude),
    })

    node_index = 0
    for feature_index, feature in enumerate(scene.features):
        if len(feature.footprint) < 2:
            continue
        tags = complete_osm_tags(feature)
        points = list(feature.footprint)
        is_closed = feature.category != "road"
        if is_closed and len(points) > 1 and points[0] == points[-1]:
            points = points[:-1]
        node_ids = []
        for point in points:
            node_id = _node_id(node_index)
            node_index += 1
            latitude, longitude = _inverse_enu(point.x, point.y, scene)
            node = ET.SubElement(root, f"{{{OSM_NAMESPACE}}}node", {
                "id": node_id,
                "lat": f"{latitude:.9f}",
                "lon": f"{longitude:.9f}",
                "version": "1",
            })
            if point.z:
                ET.SubElement(node, f"{{{OSM_NAMESPACE}}}tag", {"k": "ele", "v": f"{point.z:.2f}"})
            node_ids.append(node_id)
        if is_closed and node_ids:
            node_ids.append(node_ids[0])
        way = ET.SubElement(root, f"{{{OSM_NAMESPACE}}}way", {
            "id": _way_id(feature_index),
            "version": "1",
        })
        for node_id in node_ids:
            ET.SubElement(way, f"{{{OSM_NAMESPACE}}}nd", {"ref": node_id})
        for key, value in sorted(tags.items()):
            ET.SubElement(way, f"{{{OSM_NAMESPACE}}}tag", {"k": key, "v": value})

    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _configured_command(jar=None):
    jar = jar or os.environ.get("OSM2WORLD_JAR")
    if not jar:
        project_jars = _project_osm2world_jars()
        if project_jars:
            jar = str(project_jars[0])
    executable = os.environ.get("OSM2WORLD_BIN") or os.environ.get("OSM2WORLD_EXECUTABLE")
    if jar:
        jar_path = Path(jar).expanduser().resolve()
        if jar_path.is_file():
            return [
                _java_command(),
                "-Dfile.encoding=UTF-8",
                "--add-exports", "java.base/java.lang=ALL-UNNAMED",
                "--add-exports", "java.desktop/sun.awt=ALL-UNNAMED",
                "--add-exports", "java.desktop/sun.java2d=ALL-UNNAMED",
                "-jar", str(jar_path),
            ]
        return None
    if executable:
        executable_path = Path(executable).expanduser()
        if executable_path.is_file():
            return [str(executable_path.resolve())]
        return [executable] if shutil.which(executable) else None
    for candidate in ("osm2world", "osm2world.bat", "osm2world.exe"):
        if shutil.which(candidate):
            return [candidate]
    return None


def _validate_rendered_model(path: Path, extension: str) -> str | None:
    """Return an error when a JSON-based OSM2World model is not valid UTF-8."""
    if extension not in {"glb", "gltf"}:
        return None
    try:
        if extension == "gltf":
            payload = path.read_bytes().decode("utf-8")
        else:
            with path.open("rb") as stream:
                header = stream.read(12)
                if len(header) != 12:
                    return "GLB header is truncated"
                magic, version, declared_length = struct.unpack("<4sII", header)
                if magic != b"glTF" or version != 2:
                    return "output is not a glTF 2.0 binary"
                if declared_length != path.stat().st_size:
                    return "GLB declared length does not match the file size"
                chunk_header = stream.read(8)
                if len(chunk_header) != 8:
                    return "GLB JSON chunk header is truncated"
                chunk_length, chunk_type = struct.unpack("<I4s", chunk_header)
                if chunk_type != b"JSON":
                    return "GLB first chunk is not JSON"
                chunk = stream.read(chunk_length)
                if len(chunk) != chunk_length:
                    return "GLB JSON chunk is truncated"
                payload = chunk.decode("utf-8").rstrip(" \t\r\n\x00")
        document = json.loads(payload)
    except UnicodeDecodeError as error:
        return f"model JSON is not valid UTF-8: {error}"
    except (OSError, json.JSONDecodeError) as error:
        return f"model JSON is invalid: {error}"
    if not isinstance(document, dict) or not isinstance(document.get("asset"), dict):
        return "model JSON does not contain a glTF asset object"
    return None


def render_osm2world(scene: SceneModel, output_directory, progress=None, jar=None, enabled=True,
                     asset_version=None):
    """Write enriched OSM and optionally invoke OSM2World.

    Rendering is best-effort by design. A missing Java/JAR or an unsupported
    OSM2World output format must never prevent the Sionna scene from compiling.
    """
    progress = progress or (lambda _value, _stage: None)
    output_directory = Path(output_directory)
    asset_version = asset_version or f"build-{uuid4().hex}"
    render_directory = output_directory / "osm2world"
    render_directory.mkdir(parents=True, exist_ok=True)
    for stale_path in render_directory.glob("scene.*"):
        if stale_path.name != "scene.osm":
            stale_path.unlink(missing_ok=True)
    osm_path = write_osm(scene, (render_directory / "scene.osm").resolve())
    output_directory = output_directory.resolve()
    render_directory = render_directory.resolve()
    relative_osm = osm_path.relative_to(output_directory).as_posix()
    base = {
        "renderer": "osm2world",
        "status": "not_available" if enabled else "not_requested",
        "osm_file": relative_osm,
        "model_file": None,
        "model_format": None,
        "asset_version": asset_version,
        "message": "OSM2World was not requested",
    }
    if not enabled:
        return SceneRendering(**base)
    command = _configured_command(jar)
    if command is None:
        base["message"] = "OSM2World JAR or executable not found; generated OSM is ready"
        return SceneRendering(**base)

    timeout = max(1, int(os.environ.get("OSM2WORLD_TIMEOUT_SECONDS", "180")))
    command_cwd = render_directory
    if "-jar" in command:
        jar_index = command.index("-jar") + 1
        if jar_index < len(command):
            command_cwd = Path(command[jar_index]).resolve().parent
    progress(0.94, "Rendering detailed buildings with OSM2World")
    errors = []
    process_environment = os.environ.copy()
    java_options = process_environment.get("JAVA_TOOL_OPTIONS", "")
    if "-Dfile.encoding=" not in java_options:
        process_environment["JAVA_TOOL_OPTIONS"] = (
            f"{java_options} -Dfile.encoding=UTF-8".strip()
        )
    for extension in ("glb", "gltf", "obj"):
        output_path = render_directory / f"scene.{extension}"
        output_path.unlink(missing_ok=True)
        attempts = (
            command + ["convert", f"--input={osm_path}", f"--output={output_path}"],
            command + ["--input", str(osm_path), "--output", str(output_path)],
            command + [f"--input={osm_path}", f"--output={output_path}"],
            command + [str(osm_path), "-o", str(output_path)],
        )
        for attempt in attempts:
            try:
                result = subprocess.run(
                    attempt,
                    cwd=command_cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=process_environment,
                    timeout=timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(str(error))
                continue
            if result.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0:
                validation_error = _validate_rendered_model(output_path, extension)
                if validation_error:
                    errors.append(f"{extension}: {validation_error}")
                    output_path.unlink(missing_ok=True)
                    break
                relative_model = output_path.relative_to(output_directory).as_posix()
                return SceneRendering(
                    renderer="osm2world",
                    status="rendered",
                    osm_file=relative_osm,
                    model_file=relative_model,
                    model_format=extension,
                    asset_version=asset_version,
                    message="OSM2World model generated",
                )
            details = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
            errors.append(f"{extension}: {details[-500:]}")
    base["status"] = "failed"
    base["message"] = "OSM2World failed: " + " | ".join(errors)[-1500:]
    return SceneRendering(**base)
