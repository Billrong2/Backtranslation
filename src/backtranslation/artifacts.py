"""Canonical, write-once artifact helpers for experiment records."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any


class ArtifactError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ArtifactError("artifact_not_json_serializable") from exc


def write_bytes_once(path: Path, content: bytes, *, mode: int = 0o644) -> None:
    """Atomically publish bytes without ever replacing an existing artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            mode,
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ArtifactError("artifact_short_write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ArtifactError("artifact_already_exists") from exc
        directory_descriptor = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except ArtifactError:
        raise
    except OSError as exc:
        raise ArtifactError("artifact_write_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_json_once(path: Path, value: Any, *, mode: int = 0o644) -> None:
    write_bytes_once(path, canonical_json_bytes(value), mode=mode)


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError("artifact_json_read_failed") from exc
    if not isinstance(value, dict):
        raise ArtifactError("artifact_json_not_object")
    return value
