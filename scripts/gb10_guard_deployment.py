#!/usr/bin/env python3
"""Read-only admission and bounded readiness for an authorized Guard cutover.

No lifecycle, executable publication, or database writes are performed here.
"""
import argparse
import hashlib
import json
import re
import stat
import time
from pathlib import Path

from gb10_bounded_process import command, remaining

UNIT = "llm-guard-proxy.service"
FIELDS = ("ActiveState", "SubState", "MainPID", "InvocationID",
          "ExecMainStartTimestampMonotonic")


class Hold(RuntimeError):
    """Unproved admission/readiness; never automatically restore bytes or data."""


def digest(path):
    if not stat.S_ISREG(Path(path).stat().st_mode):
        raise Hold("executable/evidence must be a regular file")
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def admit(evidence, old_sha256, candidate_sha256, current_schema):
    """Require explicit schema support for current AND post-start canonical data."""
    try:
        if type(current_schema) is not int or current_schema < 0:
            raise ValueError
        for role, expected in (("old", old_sha256), ("candidate", candidate_sha256)):
            row = evidence[role]
            if (not re.fullmatch(r"[0-9a-f]{64}", expected)
                    or row["sha256"] != expected
                    or not re.fullmatch(r"[0-9a-f]{40}", row["source_commit"])
                    or not re.fullmatch(r"[0-9a-f]{64}", row["evidence_sha256"])):
                raise ValueError
            schemas = row["supported_schemas"]
            if (type(schemas) is not list or not schemas
                    or any(type(value) is not int or value < 0 for value in schemas)
                    or len(set(schemas)) != len(schemas)
                    or current_schema not in schemas):
                raise ValueError
        target = evidence["candidate"]["migration_target"]
        if (type(target) is not int or target < current_schema
                or target not in evidence["candidate"]["supported_schemas"]
                or target not in evidence["old"]["supported_schemas"]):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise Hold("schema/digest/evidence binding unknown or binary rollback unsupported") from None


def query_generation(deadline):
    text = command(["/usr/bin/systemctl", "--user", "show", UNIT, "--no-pager",
                    *[f"--property={field}" for field in FIELDS]],
                   timeout=remaining(deadline, 5), deadline=deadline)
    rows = [line.split("=", 1) for line in text.splitlines()]
    if (any(len(row) != 2 for row in rows) or len(rows) != len(FIELDS)
            or {row[0] for row in rows} != set(FIELDS)):
        raise Hold("malformed Guard generation")
    return dict(rows)


def fresh(before, after):
    try:
        return (after["ActiveState"] == "active" and after["SubState"] == "running"
                and re.fullmatch(r"[0-9a-f]{32}", before["InvocationID"]) is not None
                and re.fullmatch(r"[0-9a-f]{32}", after["InvocationID"]) is not None
                and after["InvocationID"] != before["InvocationID"]
                and all(re.fullmatch(r"[0-9]+", row[field]) is not None
                        for row in (before, after)
                        for field in ("MainPID", "ExecMainStartTimestampMonotonic"))
                and int(before["MainPID"]) > 0 and int(after["MainPID"]) > 0
                and after["MainPID"] != before["MainPID"]
                and int(before["ExecMainStartTimestampMonotonic"]) > 0
                and int(after["ExecMainStartTimestampMonotonic"])
                > int(before["ExecMainStartTimestampMonotonic"]))
    except (KeyError, TypeError, ValueError):
        return False


def held_digest(generation):
    return digest(f'/proc/{generation["MainPID"]}/exe')


def health_status(deadline):
    cap = remaining(deadline, 5)
    return int(command(["/usr/bin/curl", "--silent", "--show-error", "--noproxy", "*",
                        "--max-time", str(cap), "--output", "/dev/null",
                        "--write-out", "%{http_code}", "http://100.105.4.92:18009/health"],
                       timeout=cap, deadline=deadline))


def wait_ready(before, expected_sha256):
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise Hold("unknown expected executable digest")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            current = query_generation(deadline)
            if current["ActiveState"] == "failed":
                raise Hold("Guard startup failed; preserve data and failure evidence")
            if fresh(before, current):
                if held_digest(current) != expected_sha256:
                    raise Hold("held Guard executable digest mismatch")
                if health_status(deadline) == 200:
                    after = query_generation(deadline)
                    if (after == current and held_digest(after) == expected_sha256
                            and time.monotonic() < deadline):
                        return after
        except Hold:
            raise
        except (OSError, RuntimeError, ValueError, KeyError):
            # Type=simple submission can precede bind(); refusal is not rollback.
            pass
        budget = deadline - time.monotonic()
        if budget > 0:
            time.sleep(min(0.25, budget))
    raise Hold("Guard readiness deadline expired; no automatic rollback")


def strict_object(pairs):
    if len({key for key, _ in pairs}) != len(pairs):
        raise Hold("duplicate evidence field")
    return dict(pairs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    admission = sub.add_parser("admit")
    admission.add_argument("--old-binary", type=Path, required=True)
    admission.add_argument("--candidate-binary", type=Path, required=True)
    admission.add_argument("--evidence", type=Path, required=True)
    admission.add_argument("--current-schema", type=int, required=True)
    ready = sub.add_parser("ready")
    ready.add_argument("--before", type=Path, required=True)
    ready.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.action == "admit":
            evidence = json.loads(args.evidence.read_text(), object_pairs_hook=strict_object)
            admit(evidence, digest(args.old_binary), digest(args.candidate_binary), args.current_schema)
            for role in ("old", "candidate"):
                row = evidence[role]
                if digest(args.evidence.parent / row["evidence_file"]) != row["evidence_sha256"]:
                    raise Hold("source/isolated evidence artifact digest mismatch")
            print(json.dumps({"status": "ADMIT", "current_schema": args.current_schema,
                              "evidence_sha256": digest(args.evidence),
                              "old_sha256": evidence["old"]["sha256"],
                              "candidate_sha256": evidence["candidate"]["sha256"],
                              "migration_target": evidence["candidate"]["migration_target"]}, sort_keys=True))
        else:
            before = json.loads(args.before.read_text(), object_pairs_hook=strict_object)
            generation = wait_ready(before, args.expected_sha256)
            print(json.dumps({"status": "READY", "generation": generation,
                              "held_sha256": args.expected_sha256}, sort_keys=True))
    except (Hold, OSError, RuntimeError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "HOLD", "reason": "unproved schema/evidence/readiness; preserve canonical data"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
