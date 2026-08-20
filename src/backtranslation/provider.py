"""Secret-safe, one-request DeepSeek Chat Completions transport.

The credential is read from a validated local file only for the duration of a
request.  Its value is never accepted through argv or the environment and is
never included in returned events or exception messages.  Each invocation
performs exactly one HTTP request: scored experiment runs therefore cannot
silently turn transport/schema failures into extra model attempts.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


MAX_CREDENTIAL_BYTES = 4096
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
ALLOWED_HOST = "api.deepseek.com"
ALLOWED_ENDPOINT = "/chat/completions"
ALLOWED_MODEL = "deepseek-v4-pro"
ALLOWED_REASONING_EFFORT = "high"


class ProviderError(RuntimeError):
    """A provider failure carrying only a stable, secret-free code."""

    def __init__(self, code: str, *, http_status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class ProviderConfig:
    model: str = ALLOWED_MODEL
    reasoning_effort: str = ALLOWED_REASONING_EFFORT
    thinking: str = "enabled"
    max_tokens: int = 16_384
    timeout_seconds: int = 300
    response_format: str = "json_object"
    host: str = ALLOWED_HOST
    endpoint: str = ALLOWED_ENDPOINT

    def validate(self) -> None:
        if self.model != ALLOWED_MODEL:
            raise ProviderError("model_not_frozen")
        if self.reasoning_effort != ALLOWED_REASONING_EFFORT:
            raise ProviderError("reasoning_effort_not_frozen")
        if self.thinking != "enabled":
            raise ProviderError("thinking_mode_not_frozen")
        if not isinstance(self.max_tokens, int) or isinstance(self.max_tokens, bool):
            raise ProviderError("max_tokens_invalid")
        if self.max_tokens < 1 or self.max_tokens > 384_000:
            raise ProviderError("max_tokens_out_of_range")
        if self.timeout_seconds < 1 or self.timeout_seconds > 600:
            raise ProviderError("timeout_out_of_range")
        if self.response_format != "json_object":
            raise ProviderError("response_format_not_frozen")
        if self.host != ALLOWED_HOST or self.endpoint != ALLOWED_ENDPOINT:
            raise ProviderError("endpoint_not_frozen")


@dataclass(frozen=True)
class ProviderResult:
    content: str
    event: dict[str, Any]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProviderError("request_not_json_serializable") from exc


def credential_metadata(path: Path) -> dict[str, Any]:
    """Validate a credential path and return non-sensitive metadata."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProviderError("credential_stat_failed") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ProviderError("credential_not_regular_file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ProviderError("credential_mode_not_0600")
    if metadata.st_uid != os.getuid():
        raise ProviderError("credential_wrong_owner")
    if metadata.st_nlink != 1:
        raise ProviderError("credential_link_count_not_one")
    if metadata.st_size < 1 or metadata.st_size > MAX_CREDENTIAL_BYTES:
        raise ProviderError("credential_size_invalid")
    return {
        "mode": "0600",
        "owner_uid_matches_process": True,
        "regular_file": True,
        "symlink": False,
        "hard_link_count": 1,
        # Intentionally omit value, length, digest, prefix, and inode.
    }


def _read_credential(path: Path) -> bytearray:
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
        ):
            raise ProviderError("credential_changed_while_opening")
        secret = bytearray()
        while True:
            chunk = os.read(descriptor, 1024)
            if not chunk:
                break
            secret.extend(chunk)
            if len(secret) > MAX_CREDENTIAL_BYTES:
                raise ProviderError("credential_too_large")
    except OSError as exc:
        raise ProviderError("credential_open_or_read_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if secret.endswith(b"\r\n"):
        del secret[-2:]
    elif secret.endswith(b"\n"):
        del secret[-1:]
    if not secret:
        raise ProviderError("credential_empty")
    if any(value < 33 or value > 126 for value in secret):
        raise ProviderError("credential_contains_invalid_byte")
    return secret


def _usage_subset(value: Any) -> dict[str, int | None]:
    if not isinstance(value, Mapping):
        raise ProviderError("provider_usage_missing")
    output: dict[str, int | None] = {}
    for name in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    ):
        item = value.get(name)
        if item is None:
            output[name] = None
        elif isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            output[name] = item
        else:
            raise ProviderError(f"provider_usage_{name}_invalid")
    prompt = output["prompt_tokens"]
    completion = output["completion_tokens"]
    total = output["total_tokens"]
    if prompt is None or completion is None or total is None:
        raise ProviderError("provider_usage_core_fields_missing")
    if total != prompt + completion:
        raise ProviderError("provider_usage_total_mismatch")
    return output


def _parse_response(raw: bytes, expected_model: str) -> tuple[str, dict[str, Any]]:
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("provider_response_not_utf8_json") from exc
    if not isinstance(envelope, Mapping):
        raise ProviderError("provider_response_not_object")
    if envelope.get("object") != "chat.completion":
        raise ProviderError("provider_object_mismatch")
    if envelope.get("model") != expected_model:
        raise ProviderError("provider_model_mismatch")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProviderError("provider_choice_count_not_one")
    choice = choices[0]
    if not isinstance(choice, Mapping) or choice.get("index") != 0:
        raise ProviderError("provider_choice_invalid")
    if choice.get("finish_reason") != "stop":
        raise ProviderError("provider_finish_reason_not_stop")
    message = choice.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        raise ProviderError("provider_message_invalid")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("provider_content_empty")
    # Treat the assistant content as an opaque task payload at the transport
    # boundary.  Even in provider JSON mode, malformed content is a possible
    # experimental result and must reach the round-trip layer, which first
    # retains the exact bytes and only then classifies parse/schema failures.
    # Parsing here would discard precisely the invalid output needed for the
    # outcome-blind error audit.
    reasoning = message.get("reasoning_content")
    if reasoning is None:
        reasoning_bytes = 0
    elif isinstance(reasoning, str):
        reasoning_bytes = len(reasoning.encode("utf-8"))
    else:
        raise ProviderError("provider_reasoning_content_invalid")
    report = {
        "response_id": envelope.get("id") if isinstance(envelope.get("id"), str) else None,
        "returned_model": expected_model,
        "system_fingerprint": envelope.get("system_fingerprint")
        if isinstance(envelope.get("system_fingerprint"), str)
        else None,
        "finish_reason": "stop",
        "content_utf8_bytes": len(content.encode("utf-8")),
        "content_sha256": _sha256_bytes(content.encode("utf-8")),
        "reasoning_content_retained": False,
        "reasoning_content_utf8_bytes": reasoning_bytes,
        "usage": _usage_subset(envelope.get("usage")),
    }
    return content, report


def send_json_request(
    *,
    credential_path: Path,
    system_prompt: str,
    user_prompt: str,
    config: ProviderConfig | None = None,
    connection_factory: Callable[..., Any] = http.client.HTTPSConnection,
) -> ProviderResult:
    """Perform one non-streaming JSON-mode request and return sanitized data."""
    effective = config or ProviderConfig()
    effective.validate()
    credential_metadata(credential_path)
    if not system_prompt.strip() or "json" not in system_prompt.lower():
        raise ProviderError("system_prompt_must_request_json")
    if not user_prompt.strip():
        raise ProviderError("user_prompt_empty")
    request = {
        "model": effective.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": effective.response_format},
        "thinking": {"type": effective.thinking},
        "reasoning_effort": effective.reasoning_effort,
        "max_tokens": effective.max_tokens,
        "stream": False,
    }
    request_body = _canonical_json_bytes(request)
    secret: bytearray | None = None
    connection: Any = None
    headers: dict[str, str] = {}
    started = time.monotonic()
    try:
        secret = _read_credential(credential_path)
        try:
            authorization = "Bearer " + secret.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProviderError("credential_not_ascii") from exc
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        connection = connection_factory(effective.host, timeout=effective.timeout_seconds)
        connection.request("POST", effective.endpoint, body=request_body, headers=headers)
        response = connection.getresponse()
        status = int(response.status)
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("provider_response_too_large", http_status=status)
        if status != 200:
            raise ProviderError("provider_http_status_not_200", http_status=status)
        content, response_report = _parse_response(raw, effective.model)
    except ProviderError:
        raise
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise ProviderError("provider_transport_exception") from exc
    finally:
        if headers:
            headers["Authorization"] = ""
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if secret is not None:
            for index in range(len(secret)):
                secret[index] = 0
    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    event = {
        "schema_version": "backtranslation.provider_event.v1",
        "provider": "deepseek",
        "protocol": "openai_chat_completions",
        "request": {
            "host": effective.host,
            "endpoint": effective.endpoint,
            "model": effective.model,
            "thinking": effective.thinking,
            "reasoning_effort": effective.reasoning_effort,
            "max_tokens": effective.max_tokens,
            "stream": False,
            "response_format": effective.response_format,
            "system_prompt_utf8_bytes": len(system_prompt.encode("utf-8")),
            "system_prompt_sha256": _sha256_bytes(system_prompt.encode("utf-8")),
            "user_prompt_utf8_bytes": len(user_prompt.encode("utf-8")),
            "user_prompt_sha256": _sha256_bytes(user_prompt.encode("utf-8")),
            "request_body_utf8_bytes": len(request_body),
            "request_body_sha256": _sha256_bytes(request_body),
        },
        "response": response_report,
        "elapsed_milliseconds": elapsed_ms,
        "credential": credential_metadata(credential_path),
    }
    return ProviderResult(content=content, event=event)
