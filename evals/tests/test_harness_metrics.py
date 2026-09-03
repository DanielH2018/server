"""Red-proof pair for evals/harness_metrics.py's precision/recall maths, plus a
non-vacuity check on the shared classifier corpus import.

Run: uv run pytest evals/tests/test_harness_metrics.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness_metrics import DEFAULT_FIXTURE, load_vectors, precision_recall

_VECTORS = [
    {"command": "a", "readonly": True},
    {"command": "b", "readonly": False},
]


def test_precision_recall_is_perfect_when_labels_and_verdicts_agree():
    def classify(cmd):
        return "ok" if cmd == "a" else None

    result = precision_recall(_VECTORS, classify)
    assert result["tp"] == 1
    assert result["fp"] == 0
    assert result["fn"] == 0
    assert result["precision"] == 1.0
    assert result["recall"] == 1.0


def test_precision_drops_when_a_dangerous_command_is_approved():
    def classify_always_approve(_cmd):
        return "read-only: stub"

    result = precision_recall(_VECTORS, classify_always_approve)
    assert result["tp"] == 1
    assert result["fp"] == 1
    assert result["precision"] == 0.5
    assert result["recall"] == 1.0


def test_recall_drops_when_a_readonly_command_is_refused():
    def classify_never_approve(_cmd):
        return None

    result = precision_recall(_VECTORS, classify_never_approve)
    assert result["tp"] == 0
    assert result["fn"] == 1
    assert result["recall"] == 0.0
    assert result["precision"] is None


def test_corpus_import_is_non_vacuous():
    if not DEFAULT_FIXTURE.is_file():
        pytest.skip(f"shared corpus not present at {DEFAULT_FIXTURE}")
    vectors = load_vectors()
    # Measured 2026-09-03: the shared chezmoi corpus carries 15 vectors. The floor sits
    # below that so a vector accidentally dropped from the fixture still fails this test
    # well before the count reaches zero, without pinning the test to the exact count.
    assert len(vectors) >= 10, (
        f"corpus at {DEFAULT_FIXTURE} yielded too few vectors to be a real corpus"
    )
