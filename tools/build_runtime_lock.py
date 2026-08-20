#!/usr/bin/env python3
"""Build or verify the deterministic dependency/runtime hash inventory."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from backtranslation.artifacts import write_bytes_once  # noqa: E402
from backtranslation.freeze import (  # noqa: E402
    FreezeError,
    build_runtime_lock,
    canonical_json_bytes,
    verify_runtime_lock,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT / "config" / "runtime-lock.json",
        help="lock path (default: config/runtime-lock.json)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the current environment against an existing lock",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.check:
            value = json.loads(args.output.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise FreezeError("runtime_lock_not_object")
            findings = verify_runtime_lock(PROJECT, value)
            if findings:
                print(
                    json.dumps(
                        {"status": "failed", "findings": [item.as_dict() for item in findings]},
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                return 2
            print(json.dumps({"status": "passed", "inventory_sha256": value["inventory_sha256"]}, sort_keys=True))
            return 0
        lock = build_runtime_lock(PROJECT)
        write_bytes_once(args.output, canonical_json_bytes(lock) + b"\n")
        print(json.dumps({"status": "created", "path": os.fspath(args.output), "inventory_sha256": lock["inventory_sha256"]}, sort_keys=True))
        return 0
    except (FreezeError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        code = exc.code if isinstance(exc, FreezeError) else "runtime_lock_io_failed"
        print(json.dumps({"status": "failed", "code": code}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
