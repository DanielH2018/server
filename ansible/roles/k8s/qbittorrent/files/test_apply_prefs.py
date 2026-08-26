"""Tests for the qBittorrent preference diff.

THE INJECTION POINT IS `diff_prefs`, DELIBERATELY. It takes (current, desired) and
returns the delta, so every case below exercises the decision the script actually makes
without a socket. Testing the HTTP transport instead would prove only that urllib works
and would leave the comparison — the part with the trap in it — unexercised.
"""

from __future__ import annotations

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
    assert apply_prefs.DESIRED["memory_working_set_limit"] <= 1536
