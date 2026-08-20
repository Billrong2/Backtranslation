#!/usr/bin/env python3
"""Local Responses-to-DeepSeek adapter for CODE-UP Stage 1.

Codex 0.148 only supports the Responses wire protocol for custom providers,
while DeepSeek exposes an OpenAI-compatible Chat Completions endpoint.  This
loopback-only service performs that narrow translation.  It deliberately does
not forward Codex tools: Stage 1 asks the model for one JSON document per turn.

The DeepSeek credential is read from a mode-0600 regular file for each request
and is never written to the audit log or returned to the caller.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from aiohttp import ClientSession, ClientTimeout, web


MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_CREDENTIAL_BYTES = 4096


class ProxyError(RuntimeError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_credential(path: Path) -> bytearray:
    before = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or before.st_size < 1
        or before.st_size > MAX_CREDENTIAL_BYTES
    ):
        raise ProxyError("credential_metadata_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
        ):
            raise ProxyError("credential_changed_while_opening")
        secret = bytearray()
        while True:
            chunk = os.read(descriptor, 1024)
            if not chunk:
                break
            secret.extend(chunk)
            if len(secret) > MAX_CREDENTIAL_BYTES:
                raise ProxyError("credential_too_large")
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise ProxyError("credential_changed_while_reading")
    finally:
        os.close(descriptor)
    if secret.endswith(b"\r\n"):
        del secret[-2:]
    elif secret.endswith(b"\n"):
        del secret[-1:]
    if not secret or any(byte < 33 or byte > 126 for byte in secret):
        raise ProxyError("credential_content_invalid")
    return secret


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") in {"input_text", "output_text", "text"}:
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def responses_to_messages(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    items = payload.get("input")
    if isinstance(items, str):
        messages.append({"role": "user", "content": items})
    elif isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") not in {None, "message"}:
                continue
            role = item.get("role")
            if role == "developer":
                role = "system"
            if role not in {"system", "user", "assistant"}:
                continue
            text = content_text(item.get("content"))
            if text:
                messages.append({"role": str(role), "content": text})
    if not messages or not any(item["role"] == "user" for item in messages):
        raise ProxyError("responses_input_has_no_user_message")
    return messages


def response_object(
    *,
    request_id: str,
    model: str,
    text: str,
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    if not isinstance(input_tokens, int) or input_tokens < 0:
        input_tokens = 0
    if not isinstance(output_tokens, int) or output_tokens < 0:
        output_tokens = 0
    item_id = f"msg_{uuid.uuid4().hex}"
    return {
        "id": request_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "background": False,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "max_tool_calls": None,
        "model": model,
        "output": [
            {
                "id": item_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
            }
        ],
        "parallel_tool_calls": False,
        "previous_response_id": None,
        "prompt_cache_key": None,
        "reasoning": {"effort": None, "summary": None},
        "safety_identifier": None,
        "service_tier": "default",
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}, "verbosity": "medium"},
        "tool_choice": "none",
        "tools": [],
        "top_logprobs": 0,
        "top_p": None,
        "truncation": "disabled",
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": input_tokens + output_tokens,
        },
        "user": None,
        "metadata": {},
    }


async def append_audit(path: Path, value: Mapping[str, Any], lock: asyncio.Lock) -> None:
    encoded = canonical_json_bytes(value) + b"\n"
    async with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


async def deepseek_completion(
    session: ClientSession,
    *,
    endpoint: str,
    credential_path: Path,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> tuple[str, dict[str, Any], str, int, str | None]:
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    body_bytes = canonical_json_bytes(body)
    last_code = "deepseek_request_failed"
    for retry in range(4):
        secret = read_credential(credential_path)
        try:
            authorization = "Bearer " + secret.decode("ascii")
            async with session.post(
                endpoint,
                data=body_bytes,
                headers={
                    "Authorization": authorization,
                    "Content-Type": "application/json",
                },
            ) as response:
                raw = await response.read()
                status = response.status
        finally:
            for index in range(len(secret)):
                secret[index] = 0
        if status == 200:
            try:
                envelope = json.loads(raw.decode("utf-8"))
                choices = envelope["choices"]
                choice = choices[0]
                content = choice["message"]["content"]
                returned_model = envelope["model"]
                usage = envelope.get("usage", {})
                finish_reason = choice.get("finish_reason")
            except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProxyError("deepseek_response_invalid") from exc
            if not isinstance(content, str) or not content.strip():
                raise ProxyError("deepseek_response_content_empty")
            if not isinstance(returned_model, str) or not returned_model:
                raise ProxyError("deepseek_response_model_invalid")
            if not isinstance(usage, Mapping):
                usage = {}
            return content, dict(usage), returned_model, retry, finish_reason
        last_code = f"deepseek_http_{status}"
        if status not in {408, 409, 429, 500, 502, 503, 504} or retry == 3:
            raise ProxyError(last_code)
        await asyncio.sleep(min(8.0, 0.5 * (2**retry)))
    raise ProxyError(last_code)


def sse_event(event_type: str, payload: Mapping[str, Any]) -> bytes:
    return (
        f"event: {event_type}\n"
        f"data: {canonical_json_bytes(payload).decode('utf-8')}\n\n"
    ).encode("utf-8")


async def handle_responses(request: web.Request) -> web.StreamResponse:
    state = request.app
    started = time.monotonic()
    request_id = f"resp_{uuid.uuid4().hex}"
    try:
        raw = await request.read()
        if len(raw) > MAX_BODY_BYTES:
            raise ProxyError("responses_request_too_large")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ProxyError("responses_request_not_object")
        requested_model = payload.get("model")
        if requested_model != state["model"]:
            raise ProxyError("responses_model_mismatch")
        messages = responses_to_messages(payload)
        prompt_bytes = canonical_json_bytes(messages)
        content, usage, returned_model, retries, finish_reason = await deepseek_completion(
            state["session"],
            endpoint=state["endpoint"],
            credential_path=state["credential_path"],
            model=state["model"],
            messages=messages,
            max_tokens=state["max_tokens"],
        )
        response = response_object(
            request_id=request_id,
            model=state["model"],
            text=content,
            usage=usage,
        )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        await append_audit(
            state["audit_path"],
            {
                "schema_version": "codeup.deepseek-codex-adapter-event.v1",
                "request_id": request_id,
                "model_requested": state["model"],
                "model_returned": returned_model,
                "thinking": "disabled",
                "prompt_sha256": sha256_bytes(prompt_bytes),
                "prompt_bytes": len(prompt_bytes),
                "response_sha256": sha256_bytes(content.encode("utf-8")),
                "response_bytes": len(content.encode("utf-8")),
                "finish_reason": finish_reason,
                "usage": usage,
                "adapter_retries": retries,
                "elapsed_milliseconds": elapsed_ms,
            },
            state["audit_lock"],
        )
    except (ProxyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        code = str(exc) if isinstance(exc, ProxyError) else "responses_request_invalid"
        return web.json_response(
            {"error": {"message": code, "type": "adapter_error", "code": code}},
            status=502,
        )

    if not payload.get("stream", False):
        return web.json_response(response)

    stream = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await stream.prepare(request)
    item = response["output"][0]
    part = item["content"][0]
    created_response = dict(response)
    created_response["status"] = "in_progress"
    created_response["output"] = []
    sequence = 0

    async def emit(kind: str, value: dict[str, Any]) -> None:
        nonlocal sequence
        value = {"type": kind, "sequence_number": sequence, **value}
        sequence += 1
        await stream.write(sse_event(kind, value))

    await emit("response.created", {"response": created_response})
    await emit("response.in_progress", {"response": created_response})
    await emit("response.output_item.added", {"output_index": 0, "item": {**item, "status": "in_progress", "content": []}})
    await emit("response.content_part.added", {"item_id": item["id"], "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": [], "logprobs": []}})
    await emit("response.output_text.delta", {"item_id": item["id"], "output_index": 0, "content_index": 0, "delta": content, "logprobs": []})
    await emit("response.output_text.done", {"item_id": item["id"], "output_index": 0, "content_index": 0, "text": content, "logprobs": []})
    await emit("response.content_part.done", {"item_id": item["id"], "output_index": 0, "content_index": 0, "part": part})
    await emit("response.output_item.done", {"output_index": 0, "item": item})
    await emit("response.completed", {"response": response})
    await stream.write(b"data: [DONE]\n\n")
    await stream.write_eof()
    return stream


async def create_app(args: argparse.Namespace) -> web.Application:
    timeout = ClientTimeout(total=args.timeout_seconds)
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app["credential_path"] = args.credential_path
    app["endpoint"] = args.deepseek_endpoint
    app["model"] = args.model
    app["max_tokens"] = args.max_tokens
    app["audit_path"] = args.audit_path
    app["audit_lock"] = asyncio.Lock()
    app["session"] = ClientSession(timeout=timeout)
    app.router.add_post("/v1/responses", handle_responses)
    app.router.add_post("/responses", handle_responses)

    async def close_session(application: web.Application) -> None:
        await application["session"].close()

    app.on_cleanup.append(close_session)
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-path", type=Path, required=True)
    parser.add_argument("--audit-path", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument(
        "--deepseek-endpoint",
        default="https://api.deepseek.com/chat/completions",
    )
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("adapter_must_bind_loopback")
    if args.model not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
        raise SystemExit("model_must_be_an_approved_deepseek_v4_variant")
    if args.max_tokens < 1 or args.max_tokens > 32_768:
        raise SystemExit("max_tokens_out_of_range")
    canary_secret = read_credential(args.credential_path)
    for index in range(len(canary_secret)):
        canary_secret[index] = 0
    web.run_app(create_app(args), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
