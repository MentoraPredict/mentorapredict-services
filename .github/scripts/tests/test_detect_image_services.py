"""Checks image selection without depending on the current repository contents."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "detect-image-services.py"
SPEC = importlib.util.spec_from_file_location("detect_image_services", SCRIPT)
assert SPEC and SPEC.loader
DETECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DETECT)


class ImageSelectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(DETECT, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        for name, dockerfile in (("identity-service", True), ("other-service", False),
                                 ("sample-service", True)):
            directory = self.root / "services" / name
            directory.mkdir(parents=True)
            (directory / "build.gradle.kts").touch()
            if dockerfile:
                (directory / "Dockerfile").touch()

    def test_selects_changed_services_and_excludes_example(self):
        self.assertEqual(DETECT.select(["services/identity-service/src/A.java"]),
                         ["identity-service"])
        self.assertEqual(DETECT.select(["services/sample-service/Dockerfile"]), [])
        self.assertEqual(DETECT.select(["README.md"]), [])

    def test_shared_change_selects_all_non_example_services(self):
        for path in ("libs/platform/a", "gradle/libs.versions.toml", ".dockerignore"):
            with self.subTest(path=path):
                self.assertEqual(DETECT.select([path]), ["identity-service", "other-service"])

    def test_missing_dockerfile_is_reported_for_all_and_specific_service(self):
        for selection in ("ALL-SERVICES", "other-service"):
            with self.subTest(selection=selection):
                output = io.StringIO()
                with patch.dict(os.environ, {"SERVICIO": selection}, clear=True):
                    with contextlib.redirect_stdout(output):
                        DETECT.main()
                fields = dict(line.split("=", 1) for line in output.getvalue().splitlines())
                self.assertEqual(json.loads(fields["sin_dockerfile"]), ["other-service"])
                expected = ["ALL-SERVICES"] if selection == "ALL-SERVICES" else [selection]
                self.assertEqual(json.loads(fields["pruebas"]), expected)

    def test_explicit_sample_or_unknown_service_is_rejected(self):
        for selection in ("sample-service", "../identity-service", "unknown-service"):
            with self.subTest(selection=selection):
                with patch.dict(os.environ, {"SERVICIO": selection}, clear=True):
                    with self.assertRaises(ValueError):
                        DETECT.main()

    def test_push_selects_changed_service_from_git_diff(self):
        output = io.StringIO()
        env = {"EVENTO": "push", "BASE": "abc123", "SERVICIO": ""}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(DETECT.subprocess, "check_output",
                              return_value=b"services/identity-service/src/A.java\0"):
                with contextlib.redirect_stdout(output):
                    DETECT.main()
        fields = dict(line.split("=", 1) for line in output.getvalue().splitlines())
        self.assertEqual(json.loads(fields["servicios"]), ["identity-service"])
        self.assertEqual(json.loads(fields["sin_dockerfile"]), [])
        self.assertEqual(json.loads(fields["pruebas"]), ["identity-service"])

    def test_push_shared_change_runs_full_ci(self):
        output = io.StringIO()
        env = {"EVENTO": "push", "BASE": "abc123", "SERVICIO": ""}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(DETECT.subprocess, "check_output", return_value=b"libs/platform/a\0"):
                with contextlib.redirect_stdout(output):
                    DETECT.main()
        fields = dict(line.split("=", 1) for line in output.getvalue().splitlines())
        self.assertEqual(json.loads(fields["pruebas"]), ["ALL-SERVICES"])

    def test_push_unrelated_change_has_no_targets(self):
        output = io.StringIO()
        env = {"EVENTO": "push", "BASE": "abc123", "SERVICIO": ""}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(DETECT.subprocess, "check_output", return_value=b"README.md\0"):
                with contextlib.redirect_stdout(output):
                    DETECT.main()
        fields = dict(line.split("=", 1) for line in output.getvalue().splitlines())
        self.assertEqual(json.loads(fields["pruebas"]), [])
        self.assertEqual(json.loads(fields["servicios"]), [])

    def test_invalid_project_directory_fails_selection(self):
        directory = self.root / "services" / "Bad service"
        directory.mkdir()
        (directory / "build.gradle.kts").touch()
        with self.assertRaises(ValueError):
            DETECT.candidates()


if __name__ == "__main__":
    unittest.main()
