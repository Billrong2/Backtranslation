"""Test-suite configuration for the public, source-free repository."""

from __future__ import annotations

from pathlib import Path

import pytest


PROJECT = Path(__file__).resolve().parents[1]
PRIVATE_INVENTORY = (
    PROJECT / "artifacts" / "provenance" / "legacy-attempt-inventory-v0.5.json"
)
PRIVATE_REPLAY_TEST = (
    "tests/test_complete_case_120.py::"
    "test_fixed_real_cohort_is_exactly_120_cells_and_49_methods"
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip only the byte-for-byte replay check when private inputs are absent."""

    if PRIVATE_INVENTORY.exists():
        return
    marker = pytest.mark.skip(
        reason="private generation provenance is intentionally not distributed"
    )
    for item in items:
        if item.nodeid == PRIVATE_REPLAY_TEST:
            item.add_marker(marker)
