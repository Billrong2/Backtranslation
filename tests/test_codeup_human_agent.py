from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backtranslation.codeup_human_agent import (
    _code_output,
    _directions_output,
    _call_retained,
    agent_revision_prompt,
    diff_new_side,
    ProviderUnavailableError,
)
from backtranslation.codeup_stage1 import Stage1Error


def test_diff_new_side_keeps_context_and_additions() -> None:
    assert diff_new_side(
        "@@ -1,3 +1,3 @@\n old();\n-removed();\n+added();\n kept();"
    ) == "old();\nadded();\nkept();"


def test_agent_prompt_does_not_receive_human_revision() -> None:
    prompt = agent_revision_prompt(
        {
            "review_request": "use a safer call",
            "path": "A.java",
            "pre_review_diff_hunk": "@@ x @@\n+unsafe();",
            "pre_review_code": "unsafe();",
            "human_revision_code": "SECRET_HUMAN_REVISION",
        }
    )
    assert "SECRET_HUMAN_REVISION" not in prompt
    assert "use a safer call" in prompt


def test_generation_output_contracts() -> None:
    assert _code_output(b'{"code":"return value;"}') == "return value;"
    assert _directions_output(b'{"directions":["return the value"]}') == [
        "return the value"
    ]
    with pytest.raises(Stage1Error):
        _code_output(b'{"code":""}')
    with pytest.raises(Stage1Error):
        _directions_output(json.dumps({"directions": []}).encode())
    assert _code_output(b'{"code":"return value;"}"}') == "return value;"


def test_provider_account_failure_aborts_without_format_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_call(*args: object, **kwargs: object) -> tuple[bytes, bytes, int, int]:
        return b"", b"deepseek_http_402", 1, 5

    monkeypatch.setattr("backtranslation.codeup_human_agent.codex_call", failed_call)
    abort = asyncio.Event()
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(
            _call_retained(
                "stage",
                "prompt",
                attempt_root=tmp_path / "attempt",
                project_root=tmp_path,
                codex_home=tmp_path / "codex-home",
                semaphore=asyncio.Semaphore(1),
                timeout_seconds=5,
                abort=abort,
            )
        )
    assert abort.is_set()
    assert (tmp_path / "attempt" / "stage.stderr").read_bytes() == b"deepseek_http_402"
