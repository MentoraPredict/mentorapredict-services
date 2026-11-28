"""Select Gradle services affected by a pull request or reusable workflow call."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys


def select(paths: list[str], root: Path) -> list[str]:
    shared = ("libs/", "build-logic/", "gradle/", "config/")
    shared_files = {
        "settings.gradle.kts", "build.gradle.kts", "gradle.properties",
        "gradlew", "gradlew.bat", ".github/workflows/ci.yml",
        ".github/scripts/detect-ci-changes.py",
    }
    selected: set[str] = set()
    for path in paths:
        if path.startswith(shared) or path in shared_files:
            return ["ALL-SERVICES"]
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "services":
            name = parts[1]
            if name == "sample-service":
                continue
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
                raise ValueError(f"Invalid service name: {name!r}")
            if not (root / "services" / name / "build.gradle.kts").is_file():
                return ["ALL-SERVICES"]
            selected.add(name)
    return sorted(selected)


def main() -> None:
    root = Path.cwd()
    service = os.environ.get("SERVICIO", "")
    if service == "ALL-SERVICES":
        projects = ["ALL-SERVICES"]
    elif service:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", service):
            raise ValueError("Invalid service name")
        if service == "sample-service":
            raise ValueError("sample-service is excluded from CI selection")
        if not (root / "services" / service / "build.gradle.kts").is_file():
            raise ValueError(f"Unknown service: {service}")
        projects = [service]
    elif os.environ.get("EVENTO") == "pull_request":
        base = os.environ["BASE"]
        paths = subprocess.check_output(
            ["git", "diff", "--name-only", "--no-renames", "-z", base, "HEAD"],
        ).decode("utf-8").split("\0")
        projects = select(paths, root)
    else:
        projects = ["ALL-SERVICES"]
    print("proyectos=" + json.dumps(projects, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        print(f"Cannot select CI projects: {error}", file=sys.stderr)
        sys.exit(1)
