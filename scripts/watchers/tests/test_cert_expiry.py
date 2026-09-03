"""Tests for scripts/watchers/cert_expiry.py.

No test opens a real socket: `expiring_soon` and `check_expiring` are pure functions with
`now` and the fetched state injected, and `build_current` is exercised with a stub `fetch`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from watchers.cert_expiry import (
    KNOWN_PUBLIC_LABELS,
    build_current,
    check_expiring,
    expiring_soon,
    public_hostname_labels,
)

NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


# expiring_soon: the threshold pair.


def test_expiring_soon_is_flagged_inside_the_window() -> None:
    assert expiring_soon(NOW + timedelta(days=13), NOW, threshold_days=14) is True


def test_expiring_soon_is_clean_outside_the_window() -> None:
    assert expiring_soon(NOW + timedelta(days=15), NOW, threshold_days=14) is False


# check_expiring: the transition-rule pair.


def test_check_expiring_is_clean_when_state_is_unchanged() -> None:
    previous = {
        "svc.example.com": {"not_after": "2026-09-16T00:00:00+00:00", "expiring": True}
    }
    current = {
        "svc.example.com": {"not_after": "2026-09-16T00:00:00+00:00", "expiring": True}
    }
    assert check_expiring(previous, current) is None


def test_check_expiring_is_flagged_on_a_transition_into_the_window() -> None:
    previous = {
        "svc.example.com": {"not_after": "2026-10-01T00:00:00+00:00", "expiring": False}
    }
    current = {
        "svc.example.com": {"not_after": "2026-09-10T00:00:00+00:00", "expiring": True}
    }
    finding = check_expiring(previous, current)
    assert finding is not None
    assert "svc.example.com" in finding


def test_check_expiring_reports_a_renewal_too() -> None:
    previous = {
        "svc.example.com": {"not_after": "2026-09-10T00:00:00+00:00", "expiring": True}
    }
    current = {
        "svc.example.com": {"not_after": "2027-09-10T00:00:00+00:00", "expiring": False}
    }
    finding = check_expiring(previous, current)
    assert finding is not None
    assert "renewed" in finding.lower()


def test_check_expiring_is_clean_on_a_first_run_with_nothing_expiring() -> None:
    current = {
        "svc.example.com": {"not_after": "2027-09-10T00:00:00+00:00", "expiring": False}
    }
    assert check_expiring(None, current) is None


# build_current: folds a stub fetch into the shape check_expiring compares.


def test_build_current_flags_a_host_within_the_window() -> None:
    def fake_fetch(hostname: str, vip: str) -> datetime:
        return NOW + timedelta(days=5)

    current = build_current(["svc"], "example.com", "10.0.0.1", NOW, fetch=fake_fetch)
    assert current == {
        "svc.example.com": {
            "not_after": (NOW + timedelta(days=5)).isoformat(),
            "expiring": True,
        }
    }


def test_build_current_carries_forward_a_failing_hosts_previous_entry() -> None:
    # "b" fetches successfully so the run isn't a total failure; "a" fails and falls back
    # to its previous entry instead of being dropped.
    previous = {
        "a.example.com": {"not_after": "2026-09-10T00:00:00+00:00", "expiring": True}
    }

    def fetch(hostname: str, vip: str) -> datetime:
        if hostname == "a.example.com":
            raise OSError("connection refused")
        return NOW + timedelta(days=5)

    current = build_current(
        ["a", "b"], "example.com", "10.0.0.1", NOW, fetch=fetch, previous=previous
    )
    assert current["a.example.com"] == previous["a.example.com"]
    assert current["b.example.com"]["expiring"] is True


def test_build_current_raises_when_every_host_fails_with_no_history() -> None:
    # The red-proof half of the carry-forward behavior above: a total fetch failure must
    # not read as an empty, all-clear `current` -- check_expiring({}, {}) sees no
    # transition at all, which would silently wipe the accumulated state and never notify.
    def failing_fetch(hostname: str, vip: str) -> datetime:
        raise OSError("connection refused")

    with pytest.raises(RuntimeError):
        build_current(["a", "b"], "example.com", "10.0.0.1", NOW, fetch=failing_fetch)


def test_build_current_raises_when_every_host_fails_even_with_full_history() -> None:
    # The case a naive "raise only when current ends up empty" check would miss: every
    # fetch fails, but every host has a previous entry, so a carry-forward-only current
    # would be non-empty and look like a clean, uneventful run. A VIP move or a CA-store
    # change that breaks every fetch must still surface as a failure.
    previous = {
        "a.example.com": {"not_after": "2026-09-10T00:00:00+00:00", "expiring": False},
        "b.example.com": {"not_after": "2026-09-11T00:00:00+00:00", "expiring": False},
    }

    def failing_fetch(hostname: str, vip: str) -> datetime:
        raise OSError("connection refused")

    with pytest.raises(RuntimeError):
        build_current(
            ["a", "b"],
            "example.com",
            "10.0.0.1",
            NOW,
            fetch=failing_fetch,
            previous=previous,
        )


# public_hostname_labels: non-vacuity against a concrete floor, not a bare count -- a
# derivation that silently narrowed to zero services would still pass `>= 5`.


def test_public_hostname_labels_covers_the_known_floor() -> None:
    labels = set(public_hostname_labels())
    missing = KNOWN_PUBLIC_LABELS - labels
    assert not missing, f"public route derivation dropped: {missing}"
