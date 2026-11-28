"""Publish a previously scanned OCI archive to its service's private ECR repository."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


class PublishError(Exception):
    pass


def digest(value: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise PublishError("Invalid SHA-256 digest.")
    return value


def identity(path: Path) -> tuple[str, str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    prefixes = ("archive ", "build digest ", "scanned linux/amd64 manifest ")
    if len(lines) != len(prefixes) or any(not line.startswith(prefix)
                                          for line, prefix in zip(lines, prefixes)):
        raise PublishError("The scan identity report is incomplete or malformed.")
    return tuple(digest(line[len(prefix):]) for line, prefix in zip(lines, prefixes))


def check_archive(archive: Path, report: Path) -> tuple[str, str]:
    archive_digest, build_digest, scanned_digest = identity(report)
    with archive.open("rb") as stream:
        actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != archive_digest:
        raise PublishError("The OCI archive differs from the scanned archive.")

    scan_path = Path(__file__).with_name("scan-security.py")
    spec = importlib.util.spec_from_file_location("scan_security", scan_path)
    if not spec or not spec.loader:
        raise PublishError("Cannot load the OCI identity verifier.")
    scan = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scan)
    with tempfile.TemporaryDirectory() as temp:
        layout = Path(temp) / "layout"
        with tarfile.open(archive) as tar:
            tar.extractall(layout, filter="data")
        try:
            selected = scan.select_image(layout, build_digest)
        except scan.ScanError as error:
            raise PublishError(str(error)) from error
        if selected != scanned_digest:
            raise PublishError("The OCI image manifest differs from the scanned manifest.")
    return build_digest, scanned_digest


def execute(args: list[str], *, stdin: str | None = None,
            allowed_error: str | None = None) -> str | None:
    result = subprocess.run(args, input=stdin, capture_output=True, text=True, check=False)
    if result.returncode:
        if allowed_error and allowed_error in result.stderr:
            return None
        # Do not echo command output: authentication failures may contain sensitive values.
        match = re.search(r"\(([A-Za-z][A-Za-z0-9]+(?:Exception|Error)?)\)", result.stderr)
        code = f" ({match.group(1)})" if match else ""
        raise PublishError(f"{args[0]} failed{code}; exit code {result.returncode}.")
    return result.stdout.strip()


def aws(region: str, *args: str, allowed_error: str | None = None) -> str | None:
    return execute(["aws", "--region", region, "--no-cli-pager", *args],
                   allowed_error=allowed_error)


def publish(archive: Path, report: Path, service: str, commit: str,
            region: str, account: str, evidence: Path) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", service) or service == "sample-service":
        raise PublishError("Invalid or excluded service name.")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PublishError("Commit SHA must contain 40 lowercase hexadecimal characters.")
    if region not in {"us-east-1", "us-west-2"} or not re.fullmatch(r"[0-9]{12}", account):
        raise PublishError("Invalid QA account or unsupported laboratory region.")
    if not all(os.environ.get(name) for name in
               ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")):
        raise PublishError("All three temporary AWS credentials are required.")

    root_digest, manifest_digest = check_archive(archive, report)
    caller = json.loads(aws(region, "sts", "get-caller-identity", "--output", "json"))
    if caller.get("Account") != account:
        raise PublishError("The AWS identity does not belong to the configured QA account.")
    response = json.loads(aws(region, "ecr", "describe-repositories", "--repository-names",
                              service, "--output", "json"))
    repos = response.get("repositories", [])
    if len(repos) != 1 or repos[0].get("imageTagMutability") != "IMMUTABLE":
        raise PublishError("The destination repository must exist and use immutable tags.")

    tag = "sha-" + commit[:7]
    existing = aws(region, "ecr", "describe-images", "--repository-name", service,
                   "--image-ids", f"imageTag={tag}", "--output", "json",
                   allowed_error="ImageNotFoundException")
    if existing is not None:
        details = json.loads(existing).get("imageDetails", [])
        if len(details) != 1 or details[0].get("imageDigest") != root_digest:
            raise PublishError("The immutable SHA tag already points to another image.")
        print("The immutable tag already points to the same image; no push is needed.")
    else:
        registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
        password = aws(region, "ecr", "get-login-password")
        if not password:
            raise PublishError("ECR did not return an authentication token.")
        with tempfile.TemporaryDirectory() as temp:
            auth = str(Path(temp) / "auth.json")
            copied = Path(temp) / "digest.txt"
            execute(["skopeo", "login", "--authfile", auth, "--username", "AWS",
                     "--password-stdin", registry], stdin=password + "\n")
            execute(["skopeo", "copy", "--dest-authfile", auth, "--all", "--preserve-digests",
                     "--digestfile", str(copied), f"oci-archive:{archive}",
                     f"docker://{registry}/{service}:{tag}"])
            if digest(copied.read_text(encoding="utf-8").strip()) != root_digest:
                raise PublishError("The copied image digest differs from the scanned build.")
        published = json.loads(aws(region, "ecr", "describe-images", "--repository-name", service,
                                   "--image-ids", f"imageTag={tag}", "--output", "json"))
        details = published.get("imageDetails", [])
        if len(details) != 1 or details[0].get("imageDigest") != root_digest:
            raise PublishError("ECR did not return the expected image digest.")

    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({"service": service, "commit": commit, "tag": tag,
                                    "region": region, "digest": root_digest,
                                    "scanned_manifest_digest": manifest_digest}, indent=2) + "\n",
                        encoding="utf-8")
    print(f"Published {service}:{tag}; image digest {root_digest}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    try:
        publish(args.archive, args.identity, args.service, args.commit,
                args.region, args.account, args.evidence)
    except (PublishError, OSError, ValueError, KeyError, tarfile.TarError) as error:
        print(f"Image publication failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
