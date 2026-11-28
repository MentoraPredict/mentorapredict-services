"""Publish a scanned image using one of the configured laboratory accounts."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("publisher", Path(__file__).with_name("publish-ecr.py"))
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def publish_accounts(args):
    accounts = ("A", "B", "C") if args.account == "AUTO" else (args.account,)
    for label in accounts:
        print(f"Trying AWS account {label}.", flush=True)
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            os.environ[name] = os.environ.get(f"{label}_{name}", "")
        try:
            publisher.publish(args.archive, args.identity, args.service, args.commit,
                              os.environ.get(f"{label}_AWS_REGION", ""),
                              os.environ.get(f"{label}_AWS_ACCOUNT_ID", ""), args.evidence)
        except (publisher.PublishError, OSError, ValueError, KeyError) as error:
            # Publisher errors are sanitized and never contain the credentials.
            print(f"Account {label}: {error}", file=sys.stderr, flush=True)
            continue
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        evidence["account_label"] = label
        args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
                summary.write(f"- {args.service}: published to account {label}.\n")
        print(f"Publication succeeded using account {label}.", flush=True)
        return label
    raise publisher.PublishError("Publication failed for all selected accounts.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--account", choices=("AUTO", "A", "B", "C"), required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    try:
        publish_accounts(parser.parse_args())
    except publisher.PublishError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
