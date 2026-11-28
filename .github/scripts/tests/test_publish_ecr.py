"""Tests publication decisions with synthetic OCI data and mocked AWS tools."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "publish-ecr.py"
SPEC = importlib.util.spec_from_file_location("publish_ecr", SCRIPT)
assert SPEC and SPEC.loader
PUBLISH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PUBLISH)


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.layout = self.root / "layout"
        (self.layout / "blobs" / "sha256").mkdir(parents=True)
        config = self.blob({"os": "linux", "architecture": "amd64"})
        layer = self.blob(b"example layer")
        manifest = self.blob({"config": config, "layers": [layer]},
                             "application/vnd.oci.image.manifest.v1+json")
        self.digest = manifest["digest"]
        (self.layout / "index.json").write_text(json.dumps({"manifests": [manifest]}))
        self.archive = self.root / "image.tar"
        with tarfile.open(self.archive, "w") as tar:
            for path in self.layout.rglob("*"):
                if path.is_file():
                    tar.add(path, arcname=str(path.relative_to(self.layout)))
        checksum = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.report = self.root / "image-identity.txt"
        self.report.write_text(f"archive sha256:{checksum}\nbuild digest {self.digest}\n"
                               f"scanned linux/amd64 manifest {self.digest}\n")
        self.evidence = self.root / "evidence.json"
        self.environment = {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                            "AWS_SESSION_TOKEN": "test"}

    def blob(self, content, media_type="application/octet-stream"):
        data = content if isinstance(content, bytes) else json.dumps(content).encode()
        value = hashlib.sha256(data).hexdigest()
        (self.layout / "blobs" / "sha256" / value).write_bytes(data)
        return {"mediaType": media_type, "digest": f"sha256:{value}", "size": len(data)}

    def test_index_with_attestation_keeps_root_and_scanned_manifest(self):
        manifest = json.loads((self.layout / "index.json").read_text())["manifests"][0]
        attestation = self.blob({"attestation": True},
                                "application/vnd.oci.image.manifest.v1+json")
        attestation["annotations"] = {"vnd.docker.reference.type": "attestation-manifest"}
        index = self.blob({"manifests": [manifest, attestation]},
                          "application/vnd.oci.image.index.v1+json")
        (self.layout / "index.json").write_text(json.dumps({"manifests": [index]}))
        with tarfile.open(self.archive, "w") as tar:
            for path in self.layout.rglob("*"):
                if path.is_file():
                    tar.add(path, arcname=str(path.relative_to(self.layout)))
        checksum = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.report.write_text(f"archive sha256:{checksum}\nbuild digest {index['digest']}\n"
                               f"scanned linux/amd64 manifest {self.digest}\n")
        root, scanned = PUBLISH.check_archive(self.archive, self.report)
        self.assertEqual(root, index["digest"])
        self.assertEqual(scanned, self.digest)

    def call(self):
        with patch.dict(os.environ, self.environment, clear=True):
            PUBLISH.publish(self.archive, self.report, "identity-service", "a" * 40,
                            "us-east-1", "000000000000", self.evidence)

    def test_rejects_archive_changed_after_scan(self):
        with self.archive.open("ab") as stream:
            stream.write(b"changed")
        with patch.object(PUBLISH, "aws") as aws:
            with self.assertRaisesRegex(PUBLISH.PublishError, "differs from the scanned archive"):
                self.call()
            aws.assert_not_called()

    def test_rejects_scanned_manifest_mismatch(self):
        text = self.report.read_text().replace(
            "scanned linux/amd64 manifest " + self.digest,
            "scanned linux/amd64 manifest sha256:" + "0" * 64)
        self.report.write_text(text)
        with self.assertRaisesRegex(PUBLISH.PublishError, "differs from the scanned manifest"):
            self.call()

    def test_existing_identical_tag_is_idempotent(self):
        values = [json.dumps({"Account": "000000000000"}),
                  json.dumps({"repositories": [{"imageTagMutability": "IMMUTABLE"}]}),
                  json.dumps({"imageDetails": [{"imageDigest": self.digest}]})]
        with patch.object(PUBLISH, "aws", side_effect=values):
            with patch.object(PUBLISH, "execute") as execute:
                self.call()
                execute.assert_not_called()
        evidence = json.loads(self.evidence.read_text())
        self.assertEqual(evidence["digest"], self.digest)
        self.assertEqual(evidence["tag"], "sha-aaaaaaa")

    def test_rejects_tag_collision_and_mutable_repository(self):
        identity = json.dumps({"Account": "000000000000"})
        immutable = json.dumps({"repositories": [{"imageTagMutability": "IMMUTABLE"}]})
        for responses in ([identity, immutable, json.dumps({"imageDetails": [
                {"imageDigest": "sha256:" + "0" * 64}]})],
                          [identity, json.dumps({"repositories": [
                              {"imageTagMutability": "MUTABLE"}]})]):
            with self.subTest(responses=responses):
                with patch.object(PUBLISH, "aws", side_effect=responses):
                    with patch.object(PUBLISH, "execute") as execute:
                        with self.assertRaises(PUBLISH.PublishError):
                            self.call()
                        execute.assert_not_called()

    def test_push_requires_preserved_digest_and_verifies_ecr(self):
        values = [json.dumps({"Account": "000000000000"}),
                  json.dumps({"repositories": [{"imageTagMutability": "IMMUTABLE"}]}),
                  None, "temporary-token",
                  json.dumps({"imageDetails": [{"imageDigest": self.digest}]})]
        commands = []

        def execute(args, **kwargs):
            commands.append(args)
            if args[1] == "copy":
                Path(args[args.index("--digestfile") + 1]).write_text(self.digest)
            return ""

        with patch.object(PUBLISH, "aws", side_effect=values):
            with patch.object(PUBLISH, "execute", side_effect=execute):
                self.call()
        copy = next(args for args in commands if args[1] == "copy")
        self.assertIn("--preserve-digests", copy)
        self.assertIn("--all", copy)
        self.assertTrue(self.evidence.is_file())

    def test_missing_temporary_credentials_fails(self):
        self.environment.pop("AWS_SESSION_TOKEN")
        with patch.object(PUBLISH, "aws") as aws:
            with self.assertRaisesRegex(PUBLISH.PublishError, "temporary AWS credentials"):
                self.call()
            aws.assert_not_called()

    def test_wrong_account_fails_before_registry_operations(self):
        with patch.object(PUBLISH, "aws", return_value=json.dumps({"Account": "999999999999"})) as aws:
            with self.assertRaisesRegex(PUBLISH.PublishError, "configured QA account"):
                self.call()
            self.assertEqual(aws.call_count, 1)

    def test_copy_with_changed_digest_fails_without_evidence(self):
        values = [json.dumps({"Account": "000000000000"}),
                  json.dumps({"repositories": [{"imageTagMutability": "IMMUTABLE"}]}),
                  None, "temporary-token"]

        def execute(args, **kwargs):
            if args[1] == "copy":
                Path(args[args.index("--digestfile") + 1]).write_text("sha256:" + "0" * 64)
            return ""

        with patch.object(PUBLISH, "aws", side_effect=values):
            with patch.object(PUBLISH, "execute", side_effect=execute):
                with self.assertRaisesRegex(PUBLISH.PublishError, "differs from the scanned build"):
                    self.call()
        self.assertFalse(self.evidence.exists())


if __name__ == "__main__":
    unittest.main()
