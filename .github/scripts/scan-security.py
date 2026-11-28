"""Runs the Trivy security gates on the repository and on a built OCI image (DOC-A-INF-07).

Usage:
  scan-security.py repo                      secrets in the files tracked by Git
  scan-security.py image <oci.tar> [...]     vulnerabilities and secrets in a built image
  scan-security.py selftest                  proves that both gates block known bad input

Exit code: 0 passed, 1 blocked by findings, 2 the scan could not be completed.
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import string
import subprocess
import sys
import tarfile
import tempfile


# Pinned by digest: version tags were moved in the March 2026 compromise (GHSA-69fq-xp46-6x23).
TRIVY_IMAGE = ("aquasec/trivy:0.76.2"
               "@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb")
CACHE_VOLUME = "mentora-trivy-cache"
CONFIG = Path(__file__).resolve().parents[1] / "trivy"

# Blocks high and critical vulnerabilities that already have a fix.
VULNERABILITY_ARGS = ["--scanners", "vuln", "--severity", "HIGH,CRITICAL", "--ignore-unfixed"]
# Blocks any secret, whatever its severity.
SECRET_ARGS = ["--scanners", "secret", "--secret-config", "/config/secret.yaml"]

EXCEPTIONS_FILE = CONFIG / "exceptions.json"
MAX_EXCEPTION_DAYS = 90
SECTIONS = ("vulnerabilities", "secrets")
REQUIRED_FIELDS = ("id", "statement", "expired_at")
OPTIONAL_FIELDS = ("paths",)

INDEX_TYPES = {"application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json"}


class ScanError(Exception):
    """The scan could not be completed, so nothing can be considered safe."""


def unique_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ScanError(f"Duplicated key in the exceptions file: {keys}")
    return dict(pairs)


def printable(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.isprintable()


def load_exceptions(path: Path, today: datetime.date) -> list[dict]:
    """Validates the exceptions file. Any entry that breaks the rules stops the scan."""
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    if not isinstance(data, dict) or set(data) != set(SECTIONS):
        raise ScanError(f"The exceptions file must contain exactly: {', '.join(SECTIONS)}")
    exceptions = []
    for section in SECTIONS:
        if not isinstance(data[section], list):
            raise ScanError(f"'{section}' must be a list.")
        for number, entry in enumerate(data[section], start=1):
            where = f"Exception {number} of '{section}'"
            if not isinstance(entry, dict):
                raise ScanError(f"{where} must be an object.")
            missing = [field for field in REQUIRED_FIELDS if field not in entry]
            if missing:
                raise ScanError(f"{where} is missing: {', '.join(missing)}")
            unknown = sorted(set(entry) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS))
            if unknown:
                raise ScanError(f"{where} has unknown fields: {', '.join(unknown)}")
            if not (isinstance(entry["id"], str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", entry["id"])):
                raise ScanError(f"{where} has an invalid id.")
            if not printable(entry["statement"]):
                raise ScanError(f"{where} needs a reason written on one line.")
            paths = entry.get("paths")
            if "paths" in entry and not (isinstance(paths, list) and paths
                                         and all(printable(item) for item in paths)):
                raise ScanError(f"{where}: 'paths' must be a non-empty list of texts.")
            text = entry["expired_at"]
            if not (isinstance(text, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text)):
                raise ScanError(f"{where}: 'expired_at' must look like 2027-01-31.")
            expires = datetime.date.fromisoformat(text)
            if (expires - today).days > MAX_EXCEPTION_DAYS:
                raise ScanError(f"{where} lasts more than {MAX_EXCEPTION_DAYS} days.")
            # Trivy stops applying an entry when its expiry date starts.
            exceptions.append({**entry, "section": section, "active": expires > today})
    return exceptions


def show_exceptions(exceptions: list[dict], reports: Path | None) -> None:
    """Makes every exception visible in the log, the job summary and the reports."""
    lines = [f"{'ACTIVE' if e['active'] else 'EXPIRED (not applied)'} {e['section']} {e['id']} "
             f"until {e['expired_at']}: {e['statement']}"
             f" [paths: {', '.join(e.get('paths', ['all']))}]" for e in exceptions]
    title = f"Security scan exceptions: {len(lines) or 'none'}"
    print(title)
    for line in lines:
        print(f"  {line}")
    active = sum(e["active"] for e in exceptions)
    if active and os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::The security scan is applying {active} exception(s).")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        listed = "\n".join(lines or ["none"])
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"## {title}\n\n~~~text\n{listed}\n~~~\n")
    if reports:
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "exceptions.txt").write_text("\n".join([title, *lines]) + "\n",
                                                encoding="utf-8")


def ignore_file(exceptions: list[dict]) -> str:
    """Builds the Trivy ignore file from the validated, active exceptions only."""
    lines = []
    for section in SECTIONS:
        entries = [e for e in exceptions if e["section"] == section and e["active"]]
        lines.append(f"{section}:" if entries else f"{section}: []")
        for entry in entries:
            # A JSON string or list is also valid YAML. The date stays unquoted, as a YAML date.
            lines.append(f"  - id: {json.dumps(entry['id'])}")
            lines.append(f"    statement: {json.dumps(entry['statement'])}")
            lines.append(f"    expired_at: {entry['expired_at']}")
            if "paths" in entry:
                lines.append(f"    paths: {json.dumps(entry['paths'])}")
    return "\n".join(lines) + "\n"


def read_blob(layout: Path, digest: str) -> Path:
    """Returns the file of a blob after checking that its content matches its digest."""
    algorithm, _, value = digest.partition(":")
    if algorithm != "sha256" or len(value) != 64 or set(value) - set("0123456789abcdef"):
        raise ScanError(f"Unsupported digest: {digest!r}")
    blob = layout / "blobs" / algorithm / value
    with blob.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != value:
        raise ScanError(f"Blob content does not match its digest: {digest}")
    return blob


def select_image(layout: Path, expected: str) -> str:
    """Verifies the OCI layout, leaves only the linux/amd64 image in it and returns its digest."""
    index_file = layout / "index.json"
    roots = json.loads(index_file.read_text(encoding="utf-8"))["manifests"]
    if len(roots) != 1:
        raise ScanError("The OCI archive must contain exactly one build result.")
    if expected and roots[0]["digest"] != expected:
        raise ScanError(f"The archive is not the image that was built: {roots[0]['digest']}")

    pending, images = list(roots), []
    while pending:
        descriptor = pending.pop()
        # BuildKit stores provenance as extra manifests; they are not runnable images.
        if descriptor.get("annotations", {}).get("vnd.docker.reference.type"):
            continue
        content = json.loads(read_blob(layout, descriptor["digest"]).read_text(encoding="utf-8"))
        if descriptor["mediaType"] in INDEX_TYPES:
            pending.extend(content["manifests"])
        else:
            images.append((descriptor, content))
    if len(images) != 1:
        raise ScanError(f"Expected exactly one image, found {len(images)}.")

    descriptor, manifest = images[0]
    config = json.loads(read_blob(layout, manifest["config"]["digest"]).read_text(encoding="utf-8"))
    platform = f"{config.get('os')}/{config.get('architecture')}"
    if platform != "linux/amd64":
        raise ScanError(f"The image must be linux/amd64, not {platform}.")
    for layer in manifest["layers"]:
        read_blob(layout, layer["digest"])

    # Trivy reads this copy of the layout. The manifest and its digest do not change.
    index_file.write_text(json.dumps({"schemaVersion": 2, "manifests": [descriptor]}),
                          encoding="utf-8")
    return descriptor["digest"]


def findings(report: dict) -> list[str]:
    """Describes each finding without including the value of any secret."""
    lines = []
    for result in report.get("Results") or []:
        target = result.get("Target", "?")
        for item in result.get("Vulnerabilities") or []:
            lines.append(f"{item.get('Severity')} {item.get('VulnerabilityID')} "
                         f"{item.get('PkgName')} {item.get('InstalledVersion')} -> "
                         f"{item.get('FixedVersion')} ({target})")
        for item in result.get("Secrets") or []:
            lines.append(f"SECRET {item.get('RuleID')} {target}:{item.get('StartLine')}")
    return sorted(set(lines))


def trivy(work: Path, command: str, args: list[str], target: str,
          exceptions: list[dict] | None = None) -> dict:
    """Runs Trivy on a path inside the work directory and returns its JSON report."""
    output = work / "out"
    output.mkdir(exist_ok=True)
    report = output / "report.json"
    report.unlink(missing_ok=True)
    (work / "ignore.yaml").write_text(ignore_file(exceptions or []), encoding="utf-8")
    ignore = ["--ignorefile", "/work/ignore.yaml"]
    source = ["--input", target] if command == "image" else [target]
    docker = ["docker", "run", "--rm",
              "-v", f"{CACHE_VOLUME}:/root/.cache/trivy",
              "-v", f"{CONFIG}:/config:ro",
              "-v", f"{work}:/work",
              TRIVY_IMAGE, command, *args, *ignore,
              "--format", "json", "--output", "/work/out/report.json", "--no-progress", *source]
    if subprocess.run(docker, check=False).returncode != 0 or not report.is_file():
        raise ScanError("Trivy did not finish the scan.")
    return json.loads(report.read_text(encoding="utf-8"))


def gate(name: str, report: dict, reports: Path | None) -> bool:
    if reports:
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"{name}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    found = findings(report)
    print(f"[{name}] {'BLOCKED: ' + str(len(found)) + ' finding(s)' if found else 'passed'}")
    for line in found:
        print(f"  {line}")
    return not found


def scan_repository(root: Path, work: Path, reports: Path | None,
                    exceptions: list[dict]) -> bool:
    """Scans the tracked files only, so local ignored files give the same result as in CI."""
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode("utf-8")
    copy = work / "repository"
    for name in filter(None, tracked.split("\0")):
        source = root / name
        if source.is_file() and not source.is_symlink():
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, copy / name)
    report = trivy(work, "fs", SECRET_ARGS, "/work/repository", exceptions)
    return gate("repository-secrets", report, reports)


def scan_image(archive: Path, expected: str, work: Path, reports: Path | None,
               exceptions: list[dict]) -> bool:
    layout = work / "layout"
    with tarfile.open(archive) as tar:
        tar.extractall(layout, filter="data")
    digest = select_image(layout, expected)
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    identity = f"archive sha256:{checksum}\nbuild digest {expected or 'not provided'}\n" \
               f"scanned linux/amd64 manifest {digest}\n"
    print(identity, end="")
    if reports:
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "image-identity.txt").write_text(identity, encoding="utf-8")
    # Both gates always run, so one execution reports every problem.
    vulnerabilities = gate("image-vulnerabilities", trivy(
        work, "image", VULNERABILITY_ARGS, "/work/layout", exceptions), reports)
    image_secrets = gate("image-secrets", trivy(
        work, "image", [*SECRET_ARGS, "--image-config-scanners", "secret"],
        "/work/layout", exceptions), reports)
    return vulnerabilities and image_secrets


def self_test(work: Path) -> bool:
    """Gives each gate input that it must block. Nothing here is stored in the repository."""
    bad = work / "selftest"
    bad.mkdir()
    # Random value with the shape of a GitHub token. It is not a real credential.
    alphabet = string.ascii_letters + string.digits
    token = "ghp_" + "".join(secrets.choice(alphabet) for _ in range(36))
    (bad / "settings.env").write_text(f"GITHUB_TOKEN={token}\n", encoding="utf-8")
    # A very old release with published high and critical vulnerabilities.
    (bad / "requirements.txt").write_text("Django==2.2.0\n", encoding="utf-8")

    secrets_blocked = not gate("selftest-secrets", trivy(
        work, "fs", SECRET_ARGS, "/work/selftest"), None)
    vulnerabilities_blocked = not gate("selftest-vulnerabilities", trivy(
        work, "fs", VULNERABILITY_ARGS, "/work/selftest"), None)
    ok = secrets_blocked and vulnerabilities_blocked
    print("Self-test passed: both gates block." if ok
          else "Self-test FAILED: a gate accepted input that it must block.")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("repo").add_argument("--reports", type=Path)
    image = commands.add_parser("image")
    image.add_argument("archive", type=Path)
    image.add_argument("--expected-digest", default="")
    image.add_argument("--reports", type=Path)
    commands.add_parser("selftest")
    args = parser.parse_args()
    try:
        # The self-test never uses exceptions: they must not change what it proves.
        exceptions = []
        if args.command != "selftest":
            today = datetime.datetime.now(datetime.timezone.utc).date()
            exceptions = load_exceptions(EXCEPTIONS_FILE, today)
            show_exceptions(exceptions, args.reports)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            work = Path(temp).resolve()
            if args.command == "repo":
                passed = scan_repository(Path.cwd(), work, args.reports, exceptions)
            elif args.command == "image":
                passed = scan_image(args.archive, args.expected_digest, work, args.reports, exceptions)
            else:
                passed = self_test(work)
    except (ScanError, OSError, ValueError, KeyError, tarfile.TarError,
            subprocess.CalledProcessError) as error:
        print(f"Security scan not completed: {error}", file=sys.stderr)
        return 2
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
