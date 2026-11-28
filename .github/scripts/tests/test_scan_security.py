"""Checks the image identity verification and the findings summary. Docker is not needed."""

import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scan-security.py"
SPEC = importlib.util.spec_from_file_location("scan_security", SCRIPT)
assert SPEC and SPEC.loader
SCAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCAN)

MANIFEST = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"


class ImageIdentityTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.layout = Path(temp.name)
        (self.layout / "blobs" / "sha256").mkdir(parents=True)

    def blob(self, content, media_type="application/octet-stream", **extra):
        data = content if isinstance(content, bytes) else json.dumps(content).encode()
        value = hashlib.sha256(data).hexdigest()
        (self.layout / "blobs" / "sha256" / value).write_bytes(data)
        return {"mediaType": media_type, "digest": f"sha256:{value}", "size": len(data), **extra}

    def image(self, architecture="amd64"):
        config = self.blob({"os": "linux", "architecture": architecture})
        return self.blob({"config": config, "layers": [self.blob(b"layer " + architecture.encode())]},
                         MANIFEST)

    def write_index(self, *manifests):
        (self.layout / "index.json").write_text(json.dumps({"manifests": list(manifests)}))

    def test_accepts_the_built_image(self):
        image = self.image()
        self.write_index(image)
        self.assertEqual(SCAN.select_image(self.layout, image["digest"]), image["digest"])

    def test_skips_provenance_and_keeps_only_the_image(self):
        image = self.image()
        attestation = self.blob({"provenance": True}, MANIFEST, annotations={
            "vnd.docker.reference.type": "attestation-manifest"})
        index = self.blob({"manifests": [image, attestation]}, INDEX)
        self.write_index(index)
        self.assertEqual(SCAN.select_image(self.layout, index["digest"]), image["digest"])
        written = json.loads((self.layout / "index.json").read_text())
        self.assertEqual([m["digest"] for m in written["manifests"]], [image["digest"]])

    def test_rejects_an_image_other_than_the_built_one(self):
        self.write_index(self.image())
        with self.assertRaisesRegex(SCAN.ScanError, "not the image that was built"):
            SCAN.select_image(self.layout, "sha256:" + "0" * 64)

    def test_rejects_a_modified_layer(self):
        image = self.image()
        self.write_index(image)
        layer = hashlib.sha256(b"layer amd64").hexdigest()
        (self.layout / "blobs" / "sha256" / layer).write_bytes(b"changed")
        with self.assertRaisesRegex(SCAN.ScanError, "does not match its digest"):
            SCAN.select_image(self.layout, image["digest"])

    def test_rejects_another_architecture_and_several_images(self):
        self.write_index(self.image("arm64"))
        with self.assertRaisesRegex(SCAN.ScanError, "linux/amd64"):
            SCAN.select_image(self.layout, "")
        self.write_index(self.blob({"manifests": [self.image(), self.image("arm64")]}, INDEX))
        with self.assertRaisesRegex(SCAN.ScanError, "exactly one image"):
            SCAN.select_image(self.layout, "")


class GateTest(unittest.TestCase):
    def test_report_without_findings_passes(self):
        self.assertTrue(SCAN.gate("clean", {"Results": [{"Target": "app.jar"}]}, None))
        self.assertTrue(SCAN.gate("empty", {}, None))

    def test_findings_block_and_never_print_secret_values(self):
        report = {"Results": [
            {"Target": "app.jar", "Vulnerabilities": [{
                "Severity": "HIGH", "VulnerabilityID": "CVE-1", "PkgName": "lib",
                "InstalledVersion": "1", "FixedVersion": "2"}]},
            {"Target": "settings.env", "Secrets": [{
                "RuleID": "github-pat", "StartLine": 1, "Match": "SENSITIVE"}]}]}
        self.assertFalse(SCAN.gate("dirty", report, None))
        lines = SCAN.findings(report)
        self.assertEqual(len(lines), 2)
        self.assertNotIn("SENSITIVE", "".join(lines))

    def test_failed_trivy_run_is_an_error_and_not_a_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(SCAN.subprocess, "run") as run:
                run.return_value.returncode = 1
                with self.assertRaises(SCAN.ScanError):
                    SCAN.trivy(Path(temp), "fs", SCAN.SECRET_ARGS, "/work/repository")

class ExceptionsTest(unittest.TestCase):
    TODAY = datetime.date(2027, 1, 1)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / "exceptions.json"

    def load(self, *entries, text=None):
        content = text or json.dumps({"vulnerabilities": list(entries), "secrets": []})
        self.file.write_text(content, encoding="utf-8")
        return SCAN.load_exceptions(self.file, self.TODAY)

    def entry(self, **changes):
        return {"id": "CVE-2027-1", "statement": "Not reachable (MPC-1)",
                "expired_at": "2027-02-01", **changes}

    def test_repository_file_is_valid(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        SCAN.load_exceptions(SCAN.EXCEPTIONS_FILE, today)

    def test_id_reason_and_expiry_are_required(self):
        for field in ("id", "statement", "expired_at"):
            entry = self.entry()
            del entry[field]
            with self.subTest(field=field):
                with self.assertRaisesRegex(SCAN.ScanError, f"missing: {field}"):
                    self.load(entry)

    def test_rejects_invalid_values_and_unknown_fields(self):
        for changes in ({"id": ""}, {"id": "a b"}, {"statement": "   "},
                        {"statement": "two\n::error::lines"}, {"expired_at": "tomorrow"},
                        {"expired_at": None}, {"paths": []}, {"expiredAt": "2027-01-02"}):
            with self.subTest(changes=changes):
                with self.assertRaises(SCAN.ScanError):
                    self.load(self.entry(**changes))

    def test_duration_is_limited_to_90_days(self):
        self.assertTrue(self.load(self.entry(expired_at="2027-04-01"))[0]["active"])
        with self.assertRaisesRegex(SCAN.ScanError, "more than 90 days"):
            self.load(self.entry(expired_at="2027-04-02"))

    def test_expired_entries_are_not_applied(self):
        loaded = self.load(self.entry(expired_at="2027-01-01"))
        self.assertFalse(loaded[0]["active"])
        self.assertNotIn("CVE-2027-1", SCAN.ignore_file(loaded))

    def test_every_exception_is_listed(self):
        loaded = self.load(self.entry(), self.entry(id="CVE-2027-2", expired_at="2026-12-31"))
        reports = self.file.parent / "reports"
        with patch.dict(SCAN.os.environ, {}, clear=True):
            SCAN.show_exceptions(loaded, reports)
        listed = (reports / "exceptions.txt").read_text(encoding="utf-8")
        self.assertIn("ACTIVE vulnerabilities CVE-2027-1 until 2027-02-01", listed)
        self.assertIn("EXPIRED (not applied) vulnerabilities CVE-2027-2", listed)

    def test_active_entries_reach_trivy_with_an_unquoted_date(self):
        text = SCAN.ignore_file(self.load(self.entry(paths=["app.jar"])))
        self.assertIn('- id: "CVE-2027-1"', text)
        self.assertIn("expired_at: 2027-02-01\n", text)
        self.assertIn('paths: ["app.jar"]', text)
        self.assertIn("secrets: []", text)

    def test_rejects_duplicated_keys_and_unknown_sections(self):
        for text in ('{"vulnerabilities": [], "vulnerabilities": [], "secrets": []}',
                     '{"vulnerabilities": [], "secrets": [], "licenses": []}'):
            with self.subTest(text=text):
                with self.assertRaises(SCAN.ScanError):
                    self.load(text=text)


if __name__ == "__main__":
    unittest.main()
