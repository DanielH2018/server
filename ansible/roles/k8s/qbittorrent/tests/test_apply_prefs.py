"""Tests for the qBittorrent preference diff.

THE INJECTION POINT IS `diff_prefs`, DELIBERATELY. It takes (current, desired) and
returns the delta, so every case below exercises the decision the script actually makes
without a socket. Testing the HTTP transport instead would prove only that urllib works
and would leave the comparison — the part with the trap in it — unexercised.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files")
)

import apply_prefs


def test_no_changes_when_everything_matches() -> None:
    current = dict(apply_prefs.DESIRED)
    assert apply_prefs.diff_prefs(current, apply_prefs.DESIRED) == {}


def test_reports_current_and_desired_for_a_changed_key() -> None:
    current = dict(apply_prefs.DESIRED) | {"connection_speed": 30}
    assert apply_prefs.diff_prefs(current, apply_prefs.DESIRED) == {
        "connection_speed": (30, 100)
    }


def test_absent_key_counts_as_a_change() -> None:
    current = dict(apply_prefs.DESIRED)
    del current["hashing_threads"]
    changes = apply_prefs.diff_prefs(current, apply_prefs.DESIRED)
    assert changes == {"hashing_threads": ("<absent>", 2)}


def test_extra_keys_in_current_are_ignored() -> None:
    # The live API returns ~200 preferences; this script owns eight of them and must not
    # report the rest as drift.
    current = dict(apply_prefs.DESIRED) | {"web_ui_port": 8080, "dht": True}
    assert apply_prefs.diff_prefs(current, apply_prefs.DESIRED) == {}


def test_every_desired_key_is_reported_when_current_is_empty() -> None:
    changes = apply_prefs.diff_prefs({}, apply_prefs.DESIRED)
    assert set(changes) == set(apply_prefs.DESIRED)


def test_desired_values_are_scalars_the_api_accepts() -> None:
    # setPreferences serialises to JSON, and qBittorrent silently ignores a key whose type
    # it does not expect — which the read-back in main() would then report as NOT APPLIED.
    # Catch a bad literal here instead.
    for key, value in apply_prefs.DESIRED.items():
        assert isinstance(value, bool | int), f"{key} is {type(value).__name__}"


def test_hashing_threads_stays_within_the_container_cpu_limit() -> None:
    # qbittorrent_k8s_cpu_limit is "2" in the role defaults. More hashing threads than
    # that buys nothing and invites throttling, since the CPU REQUEST is only 100m.
    assert apply_prefs.DESIRED["hashing_threads"] <= 2


def test_memory_working_set_fits_the_container_memory_limit() -> None:
    # qbittorrent_k8s_mem_limit is 2048Mi. libtorrent 2.0's mmap I/O holds dirty pages up
    # to this bound, so it has to leave headroom for the rest of the process.
    #
    # NB the bound is the only thing this asserts, and that is now the only thing worth
    # asserting: the setting is inert on Linux (see the DESIRED comment), so it cannot
    # actually exceed anything. Kept so a future edit can't raise it past the container
    # limit on the assumption that it works.
    assert apply_prefs.DESIRED["memory_working_set_limit"] <= 1536


def test_checking_memory_use_leaves_room_under_the_container_memory_limit() -> None:
    # In MiB, and additive to the resident session rather than carved out of it: the recheck
    # buffer is held on top of whatever the session already holds. Against a 2048Mi container
    # limit — with mmap page cache already growing to fill it, since memory_working_set_limit
    # bounds nothing here — a large value turns a routine post-restore recheck into the one
    # operation that can push the cgroup over. 256 keeps it to an eighth of the limit.
    assert apply_prefs.DESIRED["checking_memory_use"] <= 256


class _StubClient:
    """Stands in for QbtClient so main()'s --dry-run exit code is tested without a socket."""

    def __init__(self, preferences: dict[str, object]) -> None:
        self._preferences = preferences
        self.login_calls: list[tuple[str, str]] = []

    def login(self, username: str, password: str) -> None:
        self.login_calls.append((username, password))

    def preferences(self) -> dict[str, object]:
        return self._preferences


def _run_dry_run(monkeypatch, current_preferences: dict[str, object]) -> int:
    monkeypatch.setenv("QBT_USERNAME", "admin")
    monkeypatch.setenv("QBT_PASSWORD", "hunter2")
    monkeypatch.setattr(
        apply_prefs, "QbtClient", lambda base_url: _StubClient(current_preferences)
    )
    return apply_prefs.main(["--dry-run", "--url", "http://qbittorrent.test:8080"])


# THE MUTATION: apply_prefs.py:190-191 used to `return 0` unconditionally on --dry-run, so a
# cron branching on exit code could never tell "found drift" from "nothing to do". These two
# tests are the pair that catches a regression back to that shape — flip EXIT_DRIFT back to
# EXIT_OK in main() and test_dry_run_exits_drift_when_a_preference_has_changed fails while its
# sibling keeps passing, which is exactly the signature of the bug this closes.
def test_dry_run_exits_ok_when_nothing_has_changed(monkeypatch) -> None:
    exit_code = _run_dry_run(monkeypatch, dict(apply_prefs.DESIRED))
    assert exit_code == apply_prefs.EXIT_OK


def test_dry_run_exits_drift_when_a_preference_has_changed(monkeypatch) -> None:
    # This also covers "--dry-run never writes": set_preferences is not on _StubClient at all,
    # so main() raises AttributeError — not EXIT_DRIFT — if the early return stops gating the
    # write. A separate test asserting the same exit code on the same input added no branch.
    current = dict(apply_prefs.DESIRED) | {"connection_speed": 30}
    exit_code = _run_dry_run(monkeypatch, current)
    assert exit_code == apply_prefs.EXIT_DRIFT


def test_missing_credentials_exits_bad_args(monkeypatch) -> None:
    monkeypatch.delenv("QBT_USERNAME", raising=False)
    monkeypatch.delenv("QBT_PASSWORD", raising=False)
    assert apply_prefs.main(["--dry-run"]) == apply_prefs.EXIT_BAD_ARGS
