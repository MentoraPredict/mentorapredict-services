"""Select services with their own Dockerfile for an image build."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path.cwd()


def candidates() -> list[str]:
    services = sorted(
        item.name for item in (ROOT / "services").iterdir()
        if item.is_dir() and item.name != "sample-service"
        and (item / "build.gradle.kts").is_file()
    )
    invalid = [s for s in services if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", s)]
    if invalid:
        raise ValueError(f"Invalid service directories: {', '.join(invalid)}")
    return services


def select(paths: list[str]) -> list[str]:
    all_services = candidates()
    shared_prefixes = ("libs/", "build-logic/", "gradle/", "config/")
    shared_files = {
        "settings.gradle.kts", "build.gradle.kts", "gradle.properties", "gradlew",
        "gradlew.bat", ".dockerignore", ".github/workflows/build-images.yml",
        ".github/scripts/detect-image-services.py",
        ".github/scripts/tests/test_detect_image_services.py",
    }
    if any(path.startswith(shared_prefixes) or path in shared_files for path in paths):
        return all_services
    return sorted({
        parts[1] for path in paths if len(parts := path.split("/")) >= 3
        and parts[0] == "services" and parts[1] in all_services
    })


def main() -> None:
    requested = os.environ.get("SERVICIO", "")
    if requested:
        if requested == "ALL-SERVICES":
            selected = candidates()
        elif re.fullmatch(r"[a-z0-9][a-z0-9-]*", requested) and requested in candidates():
            selected = [requested]
        else:
            raise ValueError(f"Unknown or excluded service: {requested}")
    elif os.environ.get("EVENTO") == "push":
        base = os.environ["BASE"]
        if not base or set(base) == {"0"}:
            selected = candidates()
        else:
            paths = subprocess.check_output(
                ["git", "diff", "--name-only", "--no-renames", "-z", base, "HEAD"]
            ).decode("utf-8").split("\0")
            selected = select(paths)
    else:
        selected = candidates()
    buildable = [s for s in selected if (ROOT / "services" / s / "Dockerfile").is_file()]
    missing = [s for s in selected if s not in buildable]
    test_targets = ["ALL-SERVICES"] if selected and selected == candidates() else selected
    print("servicios=" + json.dumps(buildable, separators=(",", ":")))
    print("sin_dockerfile=" + json.dumps(missing, separators=(",", ":")))
    print("pruebas=" + json.dumps(test_targets, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        print(f"Cannot select image builds: {error}", file=sys.stderr)
        sys.exit(1)
