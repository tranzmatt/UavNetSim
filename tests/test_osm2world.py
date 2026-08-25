import json
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from scene.models import EnuPoint, GeoAnchor, SceneFeature, SceneModel
from scene.osm2world import _compact_building_glb, _configured_command, _validate_rendered_model, write_osm


def _write_glb(path, document, encoding="utf-8", binary=b""):
    payload = json.dumps(document, ensure_ascii=False).encode(encoding)
    payload += b" " * (-len(payload) % 4)
    binary += b"\x00" * (-len(binary) % 4)
    binary_chunk = struct.pack("<I4s", len(binary), b"BIN\x00") + binary if binary else b""
    total_length = 12 + 8 + len(payload) + len(binary_chunk)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total_length)
        + struct.pack("<I4s", len(payload), b"JSON")
        + payload
        + binary_chunk
    )


class Osm2WorldTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_configured_java_command_forces_utf8(self):
        jar = self.directory / "osm2world.jar"
        jar.touch()

        command = _configured_command(jar)

        self.assertEqual(command[1], "-Dfile.encoding=UTF-8")
        self.assertEqual(command[-2:], ["-jar", str(jar.resolve())])

    def test_validate_rendered_model_accepts_utf8_glb(self):
        model = self.directory / "scene.glb"
        _write_glb(model, {"asset": {"version": "2.0"}, "nodes": [{"name": "文咸中心"}]})

        self.assertIsNone(_validate_rendered_model(model, "glb"))

    def test_validate_rendered_model_rejects_gbk_glb(self):
        model = self.directory / "scene.glb"
        _write_glb(
            model,
            {"asset": {"version": "2.0"}, "nodes": [{"name": "文咸中心"}]},
            "gbk",
        )

        error = _validate_rendered_model(model, "glb")

        self.assertIsNotNone(error)
        self.assertIn("UTF-8", error)

    def test_validate_rendered_model_rejects_invalid_json(self):
        model = self.directory / "scene.glb"
        payload = b'{"asset":{"version":"2.0"},"nodes":['
        payload += b" " * (-len(payload) % 4)
        model.write_bytes(
            struct.pack("<4sII", b"glTF", 2, 20 + len(payload))
            + struct.pack("<I4s", len(payload), b"JSON")
            + payload
        )

        error = _validate_rendered_model(model, "glb")

        self.assertIsNotNone(error)
        self.assertIn("invalid", error)

    def test_visualization_osm_can_exclude_roads_without_changing_scene(self):
        scene = SceneModel(
            name="Visualization layers",
            anchor=GeoAnchor(latitude=0, longitude=0),
            size_x=20,
            size_y=20,
            features=[
                SceneFeature(
                    id="building",
                    category="building",
                    footprint=[EnuPoint(x=1, y=1), EnuPoint(x=3, y=1), EnuPoint(x=3, y=3)],
                    height=10,
                ),
                SceneFeature(
                    id="road",
                    category="road",
                    footprint=[EnuPoint(x=0, y=10), EnuPoint(x=20, y=10)],
                ),
            ],
        )
        path = self.directory / "visualization.osm"

        write_osm(scene, path, included_categories={"building"})

        root = ET.parse(path).getroot()
        tag_values = {
            tag.attrib.get("k")
            for tag in root.iter()
            if tag.tag.endswith("tag")
        }
        self.assertIn("building", tag_values)
        self.assertNotIn("highway", tag_values)
        self.assertEqual([feature.category for feature in scene.features], ["building", "road"])

    def test_compact_building_glb_removes_road_mesh_and_binary_data(self):
        model = self.directory / "scene.glb"
        document = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [
                {"name": "OSM2World scene", "children": [1, 2]},
                {"name": "Road example", "mesh": 0},
                {"name": "Building example", "mesh": 1},
            ],
            "meshes": [
                {"primitives": [{"attributes": {"POSITION": 0}}]},
                {"primitives": [{"attributes": {"POSITION": 1}}]},
            ],
            "accessors": [
                {"bufferView": 0, "componentType": 5121, "count": 4, "type": "SCALAR"},
                {"bufferView": 1, "componentType": 5121, "count": 4, "type": "SCALAR"},
            ],
            "bufferViews": [
                {"buffer": 0, "byteOffset": 0, "byteLength": 4},
                {"buffer": 0, "byteOffset": 4, "byteLength": 4},
            ],
            "buffers": [{"byteLength": 8}],
        }
        _write_glb(model, document, binary=b"ROADBLDG")
        original_size = model.stat().st_size

        _compact_building_glb(model)

        compact, binary_offset, binary_length = __import__(
            "scene.osm2world", fromlist=["_read_glb_document"]
        )._read_glb_document(model)
        self.assertLess(model.stat().st_size, original_size)
        self.assertEqual([node.get("name") for node in compact["nodes"]], ["OSM2World scene", "Building example"])
        self.assertEqual(len(compact["meshes"]), 1)
        with model.open("rb") as stream:
            stream.seek(binary_offset)
            self.assertEqual(stream.read(binary_length), b"BLDG")


if __name__ == "__main__":
    unittest.main()
