from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backtranslation.provider import (
    ProviderConfig,
    ProviderError,
    credential_metadata,
    send_json_request,
)


class FakeResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, amount: int) -> bytes:
        assert amount > len(self.body)
        return self.body


class FakeConnection:
    instances: list["FakeConnection"] = []

    def __init__(self, host: str, timeout: int) -> None:
        self.host = host
        self.timeout = timeout
        self.request_record = None
        self.closed = False
        self.instances.append(self)

    def request(
        self,
        method: str,
        endpoint: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.request_record = (method, endpoint, body, dict(headers or {}))

    def getresponse(self) -> FakeResponse:
        content = json.dumps({"schema_version": "example.v1", "value": 7})
        envelope = {
            "id": "response-test",
            "object": "chat.completion",
            "model": "deepseek-v4-pro",
            "system_fingerprint": "fp_test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "private reasoning is not retained",
                        "content": content,
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 20,
            },
        }
        return FakeResponse(json.dumps(envelope).encode())

    def close(self) -> None:
        self.closed = True


class MalformedTaskConnection(FakeConnection):
    """Return a valid provider envelope containing invalid task JSON."""

    def getresponse(self) -> FakeResponse:
        envelope = {
            "id": "response-malformed-task",
            "object": "chat.completion",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "reasoning_content": None,
                        "content": "{not valid task json",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 20,
            },
        }
        return FakeResponse(json.dumps(envelope).encode())


@pytest.fixture()
def credential(tmp_path: Path) -> tuple[Path, bytes]:
    secret = b"fixture-provider-credential-do-not-log"
    path = tmp_path / "credential"
    path.write_bytes(secret)
    path.chmod(0o600)
    return path, secret


def test_credential_requires_private_mode(credential: tuple[Path, bytes]) -> None:
    path, _ = credential
    path.chmod(0o644)
    with pytest.raises(ProviderError, match="credential_mode_not_0600"):
        credential_metadata(path)


def test_single_request_is_sanitized(credential: tuple[Path, bytes]) -> None:
    path, secret = credential
    FakeConnection.instances.clear()
    result = send_json_request(
        credential_path=path,
        system_prompt="Return one JSON object matching the documented example.",
        user_prompt="Input for the unit test.",
        config=ProviderConfig(max_tokens=128),
        connection_factory=FakeConnection,
    )
    assert json.loads(result.content)["value"] == 7
    assert len(FakeConnection.instances) == 1
    connection = FakeConnection.instances[0]
    assert connection.closed
    method, endpoint, body, headers = connection.request_record
    assert method == "POST"
    assert endpoint == "/chat/completions"
    assert json.loads(body)["model"] == "deepseek-v4-pro"
    assert headers["Authorization"].startswith("Bearer ")
    serialized_event = json.dumps(result.event, sort_keys=True).encode()
    assert secret not in serialized_event
    assert secret not in result.content.encode()
    assert result.event["credential"] == {
        "mode": "0600",
        "owner_uid_matches_process": True,
        "regular_file": True,
        "symlink": False,
        "hard_link_count": 1,
    }
    assert result.event["response"]["reasoning_content_retained"] is False
    assert "private reasoning" not in serialized_event.decode()


def test_model_and_endpoint_are_frozen() -> None:
    with pytest.raises(ProviderError, match="model_not_frozen"):
        ProviderConfig(model="different-model").validate()
    with pytest.raises(ProviderError, match="endpoint_not_frozen"):
        ProviderConfig(endpoint="/different").validate()


def test_transport_preserves_malformed_task_content_for_caller(
    credential: tuple[Path, bytes],
) -> None:
    path, _ = credential
    result = send_json_request(
        credential_path=path,
        system_prompt="Return one JSON object for the test.",
        user_prompt="Input for the malformed task fixture.",
        config=ProviderConfig(max_tokens=128),
        connection_factory=MalformedTaskConnection,
    )
    assert result.content == "{not valid task json"
    response = result.event["response"]
    assert response["content_utf8_bytes"] == len(result.content.encode("utf-8"))
    assert response["content_sha256"]
