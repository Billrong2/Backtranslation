"""Isolated, write-once Code 1 -> directions -> Code 2 execution."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import ArtifactError, write_bytes_once, write_json_once
from .cases import (
    StudyCase,
    extraction_input,
    regeneration_input,
    render_prompt,
    sha256_bytes,
)
from .directions import (
    SchemaValidationError,
    complexity_features,
    validate_directions_document,
    validate_regenerated_code,
)
from .provider import ProviderConfig, ProviderError, ProviderResult, send_json_request
from .java_validation import analyze_java_method


class RoundTripError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


ProviderSender = Callable[..., ProviderResult]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def run_directory(
    artifact_root: Path, protocol_hash: str, run_index: int, method_id: str
) -> Path:
    """Return the single canonical directory for a method/run claim."""

    return artifact_root / protocol_hash / f"run-{run_index}" / method_id


def claim_case_run(
    *,
    case: StudyCase,
    run_index: int,
    protocol_hash: str,
    artifact_root: Path,
    schedule_ordinal: int | None = None,
) -> Path:
    """Claim one unit exactly once, optionally binding its frozen ordinal."""

    if run_index not in (0, 1, 2):
        raise RoundTripError("run_index_not_frozen")
    if len(protocol_hash) != 64 or any(
        char not in "0123456789abcdef" for char in protocol_hash
    ):
        raise RoundTripError("protocol_hash_invalid")
    if schedule_ordinal is not None and (
        not isinstance(schedule_ordinal, int)
        or isinstance(schedule_ordinal, bool)
        or schedule_ordinal < 1
        or schedule_ordinal > 150
    ):
        raise RoundTripError("schedule_ordinal_invalid")
    directory = run_directory(
        artifact_root, protocol_hash, run_index, case.method_id
    )
    if (directory / "status.json").exists() or (directory / "run.claim.json").exists():
        raise RoundTripError("run_already_claimed")
    claim: dict[str, Any] = {
        "schema_version": "backtranslation.run_claim.v1",
        "method_id": case.method_id,
        "run_index": run_index,
        "protocol_hash": protocol_hash,
        "claimed_at_utc": _utc_now(),
        "code_1_sha256": case.code_1_sha256,
        "type_context_sha256": case.type_context_sha256,
    }
    if schedule_ordinal is not None:
        claim["schedule_ordinal"] = schedule_ordinal
    try:
        write_json_once(directory / "run.claim.json", claim)
    except ArtifactError as exc:
        if exc.code == "artifact_already_exists":
            raise RoundTripError("run_already_claimed") from exc
        raise RoundTripError(exc.code) from exc
    return directory


def _verify_preclaim(
    *,
    run_directory_path: Path,
    case: StudyCase,
    run_index: int,
    protocol_hash: str,
    schedule_ordinal: int,
) -> None:
    try:
        claim = json.loads(
            (run_directory_path / "run.claim.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RoundTripError("preclaim_read_failed") from exc
    expected = {
        "schema_version": "backtranslation.run_claim.v1",
        "method_id": case.method_id,
        "run_index": run_index,
        "protocol_hash": protocol_hash,
        "code_1_sha256": case.code_1_sha256,
        "type_context_sha256": case.type_context_sha256,
        "schedule_ordinal": schedule_ordinal,
    }
    if (
        not isinstance(claim, Mapping)
        or not isinstance(claim.get("claimed_at_utc"), str)
        or {key: claim.get(key) for key in expected} != expected
        or set(claim) != set(expected) | {"claimed_at_utc"}
        or (run_directory_path / "status.json").exists()
    ):
        raise RoundTripError("preclaim_mismatch")


def _load_prompt(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RoundTripError("prompt_read_failed") from exc
    if not value.strip():
        raise RoundTripError("prompt_empty")
    return value


def _write_failure(
    run_directory: Path,
    *,
    stage: str,
    failure_class: str,
    failure_code: str,
    started: float,
    method_id: str | None = None,
    run_index: int | None = None,
    http_status: int | None = None,
) -> dict[str, Any]:
    value = {
        "schema_version": "backtranslation.run_status.v1",
        "status": "failed",
        "stage": stage,
        "failure_class": failure_class,
        "failure_code": failure_code,
        "finished_at_utc": _utc_now(),
        "elapsed_milliseconds": max(0, int((time.monotonic() - started) * 1000)),
    }
    if method_id is not None:
        value["method_id"] = method_id
    if run_index is not None:
        value["run_index"] = run_index
    if http_status is not None:
        value["http_status"] = http_status
    write_json_once(run_directory / "status.json", value)
    return value


def _stage_claim(
    run_directory: Path,
    *,
    stage: str,
    system_prompt: str,
    user_prompt: str,
    output_schema_path: Path,
    provider_config: ProviderConfig,
) -> None:
    try:
        schema_bytes = output_schema_path.read_bytes()
    except OSError as exc:
        raise RoundTripError("output_schema_read_failed") from exc
    write_json_once(
        run_directory / f"{stage}.claim.json",
        {
            "schema_version": "backtranslation.stage_claim.v1",
            "stage": stage,
            "claimed_at_utc": _utc_now(),
            "system_prompt_sha256": _sha256_text(system_prompt),
            "user_prompt_sha256": _sha256_text(user_prompt),
            "output_schema_path": output_schema_path.name,
            "output_schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
            "requested_settings": {
                "model": provider_config.model,
                "thinking": provider_config.thinking,
                "reasoning_effort": provider_config.reasoning_effort,
                "max_tokens": provider_config.max_tokens,
                "response_format": provider_config.response_format,
                "stream": False,
                "temperature": "omitted",
                "top_p": "omitted",
                "seed": "omitted",
            },
        },
    )


def _call_stage(
    *,
    stage: str,
    run_directory: Path,
    credential_path: Path,
    system_prompt: str,
    user_prompt: str,
    sender: ProviderSender,
    provider_config: ProviderConfig,
    output_schema_path: Path,
) -> ProviderResult:
    _stage_claim(
        run_directory,
        stage=stage,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        output_schema_path=output_schema_path,
        provider_config=provider_config,
    )
    result = sender(
        credential_path=credential_path,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        config=provider_config,
    )
    write_json_once(
        run_directory / f"{stage}.provider.json",
        {
            "schema_version": "backtranslation.stage_provider_response.v1",
            "stage": stage,
            "provider_event": result.event,
        },
    )
    # Retain the assistant's exact task output before task-specific parsing or
    # schema validation.  This makes malformed/schema-invalid generations
    # auditable without retaining hidden reasoning or request headers.  A text
    # envelope is intentional: test senders may violate the production
    # transport's syntactic-JSON guarantee, and a bad payload must not make the
    # artifact tree masquerade as valid JSON.
    write_bytes_once(
        run_directory / f"{stage}.output.txt",
        result.content.encode("utf-8", errors="strict"),
    )
    return result


def execute_case_run(
    *,
    case: StudyCase,
    run_index: int,
    protocol_hash: str,
    project_directory: Path,
    artifact_root: Path,
    credential_path: Path,
    sender: ProviderSender = send_json_request,
    provider_config: ProviderConfig | None = None,
    preclaimed_schedule_ordinal: int | None = None,
) -> dict[str, Any]:
    """Execute one paired run exactly once and return its terminal status."""
    if run_index not in (0, 1, 2):
        raise RoundTripError("run_index_not_frozen")
    if len(protocol_hash) != 64 or any(char not in "0123456789abcdef" for char in protocol_hash):
        raise RoundTripError("protocol_hash_invalid")
    run_directory_path = run_directory(
        artifact_root, protocol_hash, run_index, case.method_id
    )
    run_started = time.monotonic()
    if preclaimed_schedule_ordinal is None:
        claim_case_run(
            case=case,
            run_index=run_index,
            protocol_hash=protocol_hash,
            artifact_root=artifact_root,
        )
    else:
        _verify_preclaim(
            run_directory_path=run_directory_path,
            case=case,
            run_index=run_index,
            protocol_hash=protocol_hash,
            schedule_ordinal=preclaimed_schedule_ordinal,
        )
    effective_config = provider_config or ProviderConfig()
    extraction_payload = extraction_input(case)
    extraction_system = _load_prompt(project_directory / "prompts" / "extract.system.txt")
    extraction_template = _load_prompt(project_directory / "prompts" / "extract.user.txt")
    extraction_user = render_prompt(
        extraction_template, "EXTRACTION_INPUT_JSON", extraction_payload
    )
    try:
        extraction_result = _call_stage(
            stage="extraction",
            run_directory=run_directory_path,
            credential_path=credential_path,
            system_prompt=extraction_system,
            user_prompt=extraction_user,
            sender=sender,
            provider_config=effective_config,
            output_schema_path=project_directory / "schemas" / "directions-v1.schema.json",
        )
    except ProviderError as exc:
        return _write_failure(
            run_directory_path,
            stage="extraction_api",
            failure_class="provider",
            failure_code=exc.code,
            started=run_started,
            method_id=case.method_id,
            run_index=run_index,
            http_status=exc.http_status,
        )
    try:
        raw_directions = json.loads(extraction_result.content)
    except json.JSONDecodeError:
        return _write_failure(
            run_directory_path,
            stage="extraction_parse",
            failure_class="parse",
            failure_code="extraction_not_json",
            started=run_started,
            method_id=case.method_id,
            run_index=run_index,
        )
    try:
        document = validate_directions_document(raw_directions)
    except SchemaValidationError as exc:
        return _write_failure(
            run_directory_path,
            stage="extraction_schema",
            failure_class="schema",
            failure_code=exc.code,
            started=run_started,
            method_id=case.method_id,
            run_index=run_index,
        )
    features = complexity_features(document)
    write_json_once(
        run_directory_path / "extraction.result.json",
        {
            "schema_version": "backtranslation.extraction_result.v1",
            "method_id": case.method_id,
            "run_index": run_index,
            "directions": raw_directions,
            "complexity_features": features,
            "provider_event": extraction_result.event,
        },
    )

    regeneration_payload = regeneration_input(case, raw_directions)
    regeneration_system = _load_prompt(project_directory / "prompts" / "regenerate.system.txt")
    regeneration_template = _load_prompt(project_directory / "prompts" / "regenerate.user.txt")
    regeneration_user = render_prompt(
        regeneration_template, "REGENERATION_INPUT_JSON", regeneration_payload
    )
    try:
        regeneration_result = _call_stage(
            stage="regeneration",
            run_directory=run_directory_path,
            credential_path=credential_path,
            system_prompt=regeneration_system,
            user_prompt=regeneration_user,
            sender=sender,
            provider_config=effective_config,
            output_schema_path=project_directory / "schemas" / "regeneration-v1.schema.json",
        )
    except ProviderError as exc:
        return _write_failure(
            run_directory_path,
            stage="regeneration_api",
            failure_class="provider",
            failure_code=exc.code,
            started=run_started,
            method_id=case.method_id,
            run_index=run_index,
            http_status=exc.http_status,
        )
    try:
        raw_code = json.loads(regeneration_result.content)
    except json.JSONDecodeError:
        return _write_failure(
            run_directory_path,
            stage="regeneration_parse",
            failure_class="parse",
            failure_code="regeneration_not_json",
            started=run_started,
            method_id=case.method_id,
            run_index=run_index,
        )
    try:
        regenerated = validate_regenerated_code(raw_code)
    except SchemaValidationError as exc:
        return _write_failure(
            run_directory_path,
            stage="regeneration_schema",
            failure_class="schema",
            failure_code=exc.code,
            started=run_started,
            method_id=case.method_id,
            run_index=run_index,
        )
    java_analysis = analyze_java_method(regenerated.code, case.target_declaration)
    write_json_once(
        run_directory_path / "regeneration.result.json",
        {
            "schema_version": "backtranslation.regeneration_result.v1",
            "method_id": case.method_id,
            "run_index": run_index,
            "output": raw_code,
            "code_2_sha256": sha256_bytes(regenerated.code.encode("utf-8")),
            "java_validation": java_analysis.as_metadata(),
            "provider_event": regeneration_result.event,
        },
    )
    status = {
        "schema_version": "backtranslation.run_status.v1",
        "status": "generated",
        "stage": "generation_complete",
        "method_id": case.method_id,
        "run_index": run_index,
        "protocol_hash": protocol_hash,
        "finished_at_utc": _utc_now(),
        "elapsed_milliseconds": max(0, int((time.monotonic() - run_started) * 1000)),
    }
    write_json_once(run_directory_path / "status.json", status)
    return status
