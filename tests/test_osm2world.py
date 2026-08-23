import json
import struct
import tempfile
import unittest
from pathlib import Path

from scene.osm2world import _configured_command, _validate_rendered_model


def _write_glb(path, document, encoding="utf-8"):
    payload = json.dumps(document, ensure_ascii=False).encode(encoding)
    payload += b" " * (-len(payload) % 4)
    total_length = 12 + 8 + len(payload)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total_length)
        + struct.pack("<I4s", len(payload), b"JSON")
        + payload
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


if __name__ == "__main__":
    unittest.main()
