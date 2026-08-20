#!/usr/bin/env python3
"""Preflight, create, verify, and approve a protocol freeze manifest."""

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
    append_freeze_record,
    audit_artifact_tree,
    canonical_json_bytes,
    create_freeze_manifest,
    manifest_sha256,
    preflight_freeze,
    verify_manifest,
)


def _load_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FreezeError("json_object_required")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "create"):
        child = subparsers.add_parser(command)
        child.add_argument("--spec", type=Path, default=PROJECT / "config" / "freeze-spec.json")
        if command == "create":
            child.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--expected-sha256")
    approve = subparsers.add_parser("approve")
    approve.add_argument("--manifest", type=Path, required=True)
    approve.add_argument("--spec", type=Path, default=PROJECT / "config" / "freeze-spec.json")
    approve.add_argument("--expected-sha256", required=True)
    approve.add_argument("--record", type=Path, required=True)
    approve.add_argument("--reviewer", required=True)
    return parser


def _scan_declared_artifacts(spec: dict[str, object]) -> None:
    roots = spec.get("artifact_scan_roots", [])
    if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
        raise FreezeError("artifact_scan_roots_invalid")
    findings = []
    for relative in roots:
        path = PROJECT / relative
        if path.exists():
            findings.extend(audit_artifact_tree(path))
    if findings:
        raise FreezeError("artifact_safety_scan_failed", findings)


def _failure(exc: Exception) -> int:
    if isinstance(exc, FreezeError):
        payload: dict[str, object] = {"status": "blocked", "code": exc.code}
        if exc.findings:
            payload["findings"] = [item.as_dict() for item in exc.findings]
    else:
        payload = {"status": "blocked", "code": "freeze_io_failed"}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in {"preflight", "create"}:
            spec = _load_object(args.spec)
            if args.command == "preflight":
                paths = preflight_freeze(PROJECT, spec)
                _scan_declared_artifacts(spec)
                print(json.dumps({"status": "passed", "candidate_files": len(paths)}, sort_keys=True))
                return 0
            manifest = create_freeze_manifest(PROJECT, spec)
            _scan_declared_artifacts(spec)
            digest = manifest_sha256(manifest)
            write_bytes_once(args.output, canonical_json_bytes(manifest) + b"\n")
            print(json.dumps({"status": "candidate_created", "manifest_sha256": digest, "path": os.fspath(args.output)}, sort_keys=True))
            return 0
        manifest_path = args.manifest if args.manifest.is_absolute() else PROJECT / args.manifest
        manifest = _load_object(manifest_path)
        digest = verify_manifest(PROJECT, manifest, expected_sha256=args.expected_sha256)
        if args.command == "verify":
            print(json.dumps({"status": "passed", "manifest_sha256": digest}, sort_keys=True))
            return 0
        if digest != args.expected_sha256:
            raise FreezeError("approval_hash_mismatch")
        spec_path = args.spec if args.spec.is_absolute() else PROJECT / args.spec
        spec = _load_object(spec_path)
        candidate_paths = preflight_freeze(PROJECT, spec)
        _scan_declared_artifacts(spec)
        manifested_paths = tuple(str(item.get("path")) for item in manifest.get("files", []))
        if manifested_paths != candidate_paths:
            raise FreezeError("approval_manifest_scope_mismatch")
        relative_manifest = manifest_path.resolve(strict=True).relative_to(
            PROJECT.resolve(strict=True)
        )
        record = append_freeze_record(
            args.record,
            manifest_path=relative_manifest.as_posix(),
            manifest_hash=digest,
            reviewer=args.reviewer,
        )
        print(json.dumps({"status": "frozen", **record}, sort_keys=True))
        return 0
    except (FreezeError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return _failure(exc)


if __name__ == "__main__":
    raise SystemExit(main())
