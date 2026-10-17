"""Behaviour of the poller that claude-triage-poll.timer runs every two minutes.

Each test drives `step` or `main` through its seams: no Prometheus, no systemctl.
"""

import fcntl
import json
from datetime import datetime, timezone

import claude_triage_poll as poll
import claude_triage_run as run
import pytest

T0 = datetime(2026, 10, 15, 18, 30, tzinfo=timezone.utc).timestamp()
MEMORY = {"kuma:Memory": {"source": "kuma", "monitor": "Memory"}}


def advance(
    state, down, hold=False, muted=False, unknown=frozenset(), kuma_up=frozenset()
):
    return poll.step(state, down, hold, muted, T0, 2, 3, unknown, kuma_up)


def test_the_first_poll_records_what_is_down_and_starts_nothing() -> None:
    state, episodes = advance({}, MEMORY)
    assert episodes == []
    # Still down on the next polls: it was down before the poller looked, so no episode.
    state, episodes = advance(state, MEMORY)
    state, episodes = advance(state, MEMORY)
    assert episodes == []


def test_a_fall_opens_one_episode_once_it_has_held_for_the_streak() -> None:
    state, _ = advance({}, {})
    state, episodes = advance(state, MEMORY)
    assert episodes == []  # one poll is a flap
    state, episodes = advance(state, MEMORY)
    assert [e["monitor"] for e in episodes] == ["Memory"]
    state, episodes = advance(state, MEMORY)
    assert episodes == []  # still the same episode


def test_a_flap_that_recovers_before_the_streak_opens_nothing() -> None:
    state, _ = advance({}, {})
    state, _ = advance(state, MEMORY)
    state, _ = advance(state, {}, kuma_up={"kuma:Memory"})
    state, episodes = advance(state, MEMORY)
    assert episodes == []


def test_a_recovery_closes_the_episode_so_the_next_fall_is_new() -> None:
    state, _ = advance({}, {})
    state, _ = advance(state, MEMORY)
    state, _ = advance(state, MEMORY)
    state, _ = advance(state, {}, kuma_up={"kuma:Memory"})
    assert state["keys"] == {}
    state, _ = advance(state, MEMORY)
    state, episodes = advance(state, MEMORY)
    assert len(episodes) == 1


def test_a_kuma_series_that_vanishes_is_not_a_recovery() -> None:
    """A long-interval monitor has no series until its first beat after a Kuma restart."""
    state, _ = advance({}, MEMORY)  # the baseline: already red
    state, _ = advance(state, {})
    assert "kuma:Memory" in state["keys"]
    _, episodes = advance(state, MEMORY)
    assert episodes == []


def test_only_a_monitor_reading_1_counts_as_up() -> None:
    kuma = [
        {"metric": {"monitor_name": "up"}, "value": [T0, "1"]},
        {"metric": {"monitor_name": "down"}, "value": [T0, "0"]},
        {"metric": {"monitor_name": "pending"}, "value": [T0, "2"]},
    ]
    assert poll.kuma_up_keys(kuma) == {"kuma:up"}
    assert set(poll.down_keys(kuma, [])) == {"kuma:down"}


def test_a_failed_read_keeps_the_state_rather_than_reading_as_recovered() -> None:
    state, _ = advance({}, {})
    state, _ = advance(state, MEMORY)
    state, _ = advance(state, MEMORY)
    held, episodes = advance(state, None)
    assert held == state
    assert episodes == []
    # The monitor never recovered, so the poll after the outage opens nothing.
    _, episodes = advance(held, MEMORY)
    assert episodes == []


def test_more_falls_than_the_storm_size_make_one_storm_episode() -> None:
    down = {f"kuma:m{i}": {"source": "kuma", "monitor": f"m{i}"} for i in range(4)}
    state, _ = advance({}, {})
    state, _ = advance(state, down)
    _, episodes = advance(state, down)
    (storm,) = episodes
    assert storm["storm"] is True
    assert [d["monitor"] for d in storm["down"]] == ["m0", "m1", "m2", "m3"]


def test_falls_up_to_the_storm_size_each_get_their_own_episode() -> None:
    down = {f"kuma:m{i}": {"source": "kuma", "monitor": f"m{i}"} for i in range(3)}
    state, _ = advance({}, {})
    state, _ = advance(state, down)
    _, episodes = advance(state, down)
    assert len(episodes) == 3


@pytest.mark.parametrize("gate", ["hold", "muted"])
def test_a_hold_or_the_reboot_mute_defers_the_episode_until_it_lifts(gate) -> None:
    state, _ = advance({}, {})
    state, _ = advance(state, MEMORY, **{gate: True})
    state, episodes = advance(state, MEMORY, **{gate: True})
    assert episodes == []
    _, episodes = advance(state, MEMORY)
    assert len(episodes) == 1


@pytest.mark.parametrize(
    "key",
    [
        "kuma:rkhunter Scan (daniel-box)",
        "kuma:Memory",
        "alert:KubePodCrashLooping:",
        "alert:" + "VeryLongAlertName" * 6 + ":sonarr",
        "storm",
    ],
)
def test_every_episode_id_is_one_the_runner_and_its_rule_accept(key) -> None:
    episode_id = poll.episode_id(key, T0)
    assert run.EPISODE_ID.match(episode_id), episode_id
    assert episode_id.startswith("20261015t1830-")


def test_down_keys_skip_the_deploy_hold_and_key_alerts_by_service() -> None:
    kuma = [{"metric": {"monitor_name": "Memory"}, "value": [T0, "0"]}]
    alerts = [
        {"metric": {"alertname": "DeployHoldActive"}},
        {"metric": {"alertname": "TargetDown", "service": "sonarr"}},
    ]
    assert set(poll.down_keys(kuma, alerts)) == {
        "kuma:Memory",
        "alert:TargetDown:sonarr",
    }


REBOOT = {
    "REBOOT_WEEKDAY": "0",
    "REBOOT_HOUR": "7",
    "REBOOT_MINUTE": "30",
    "REBOOT_BEFORE_MIN": "5",
    "REBOOT_GRACE_MIN": "45",
}


@pytest.mark.parametrize(
    ("when", "muted"),
    [
        (
            datetime(2026, 10, 18, 7, 24, tzinfo=timezone.utc),
            False,
        ),  # Sunday, before the lead
        (datetime(2026, 10, 18, 7, 25, tzinfo=timezone.utc), True),
        (datetime(2026, 10, 18, 8, 15, tzinfo=timezone.utc), True),
        (datetime(2026, 10, 18, 8, 16, tzinfo=timezone.utc), False),
        (datetime(2026, 10, 17, 7, 30, tzinfo=timezone.utc), False),  # Saturday
    ],
)
def test_the_reboot_window_spans_the_lead_and_the_grace(when, muted) -> None:
    now = when.timestamp()
    assert poll.in_reboot_window(REBOOT, now) is muted


def _config(tmp_path, mode: str) -> str:
    state = tmp_path / "state"
    (state / "inbox").mkdir(parents=True)
    path = tmp_path / "poll.cfg"
    path.write_text(
        f"STATE_DIR={state}\nPOLL_MODE={mode}\n"
        "PROM_URLS=http://loopback http://cluster-ip\nPOLL_STREAK=1\n"
    )
    return str(path)


HEALTHY = {
    poll.KUMA_UP_QUERY: [{"value": [T0, "1"]}],
    poll.PROM_AGE_QUERY: [{"value": [T0, "86400"]}],
}


def _reads(*kuma_by_poll, **answers):
    """A `read` seam answering each poll's Kuma query in turn, with healthy sources."""
    polls = iter(kuma_by_poll)
    fixed = {**HEALTHY, poll.ALERTS_QUERY: [], **answers}

    def read(base, query):
        return next(polls) if query == poll.KUMA_QUERY else fixed[query]

    return read


def _run(config, read, started):
    return poll.main(
        ["claude_triage_poll.py"], config, read, started.append, lambda: T0
    )


def _fall(*names):
    return [{"metric": {"monitor_name": n}, "value": [T0, "0"]} for n in names]


@pytest.mark.parametrize("mode", ["armed", "shadow"])
def test_armed_mode_starts_a_run_and_shadow_mode_only_logs_it(tmp_path, mode) -> None:
    config = _config(tmp_path, mode)
    started = []
    read = _reads([], _fall("Memory"))
    assert _run(config, read, started) == 0  # the baseline
    assert _run(config, read, started) == 0
    shadow = tmp_path / "state" / "shadow.jsonl"
    inbox = tmp_path / "state" / "inbox"
    if mode == "armed":
        assert started == ["20261015t1830-kuma-memory"]
        queued = json.loads((inbox / f"{started[0]}.json").read_text())
        assert queued["monitor"] == "Memory"
        assert not shadow.exists()
    else:
        assert started == []
        assert list(inbox.iterdir()) == []
        (line,) = shadow.read_text().splitlines()
        assert json.loads(line)["monitor"] == "Memory"


def test_queued_episodes_start_one_at_a_time_as_the_lock_frees(tmp_path) -> None:
    """Three falls in one poll: a busy lock must leave them queued, not exit them 75."""
    config = _config(tmp_path, "armed")
    falls = _fall("a", "b", "c")
    started = []
    read = _reads([], falls, falls, falls, falls, falls)
    _run(config, read, started)  # the baseline
    with open(tmp_path / "state" / "run.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        _run(config, read, started)
        assert started == []  # a run holds the lock: all three wait in the inbox
    inbox = tmp_path / "state" / "inbox"
    for _ in range(3):
        _run(config, read, started)
        (inbox / f"{started[-1]}.json").unlink()  # the runner finished it
    assert [s.rsplit("-", 1)[1] for s in started] == ["a", "b", "c"]
    _run(config, read, started)
    assert len(started) == 3


def test_a_queued_episode_whose_run_crashed_is_not_started_again(tmp_path) -> None:
    config = _config(tmp_path, "armed")
    started = []
    memory = _fall("Memory")
    read = _reads([], memory, memory, memory)
    _run(config, read, started)
    _run(config, read, started)
    # The run crashed: the lock is free and its episode is still queued.
    _run(config, read, started)
    _run(config, read, started)
    assert len(started) == 1


def test_a_kuma_restart_does_not_read_as_every_tile_recovering(tmp_path) -> None:
    """Kuma's series vanish while it restarts; its standing reds are not new falls after."""
    config = _config(tmp_path, "armed")
    started = []
    restarting = _reads([], [], **{poll.KUMA_UP_QUERY: [{"value": [T0, "0"]}]})
    _run(config, _reads(_fall("rkhunter")), started)  # the baseline: already red
    _run(config, restarting, started)
    _run(config, restarting, started)
    back = _reads(_fall("rkhunter"), _fall("rkhunter"))
    _run(config, back, started)
    _run(config, back, started)
    assert started == []


def test_alerts_after_a_prometheus_restart_are_not_new_falls() -> None:
    alert = {
        "alert:TargetDown:sonarr": {"source": "alertmanager", "monitor": "TargetDown"}
    }
    state, _ = advance({}, alert)
    state, _ = advance(state, {}, unknown={"alert"})  # Prometheus just restarted
    assert "alert:TargetDown:sonarr" in state["keys"]
    _, episodes = advance(state, alert)
    assert episodes == []


def test_the_baseline_waits_for_a_poll_that_can_judge_every_source() -> None:
    state, _ = advance({}, {}, unknown={"kuma"})
    assert state == {}
    state, episodes = advance(state, MEMORY)
    assert episodes == []  # this poll is the baseline


@pytest.mark.parametrize(
    ("kuma_up", "age", "unknown"),
    [
        ("1", "86400", set()),
        ("0", "86400", {"kuma"}),
        (None, "86400", {"kuma"}),
        ("1", "300", {"alert"}),
        ("1", None, {"alert"}),
    ],
)
def test_a_source_is_unknown_while_its_exporter_is_down_or_just_restarted(
    kuma_up, age, unknown
) -> None:
    found = {
        "kuma_up": [] if kuma_up is None else [{"value": [T0, kuma_up]}],
        "prom_age": [] if age is None else [{"value": [T0, age]}],
    }
    assert poll.unknown_sources(found) == unknown


def test_off_mode_reads_nothing(tmp_path) -> None:
    def read(base, query):
        raise AssertionError("off mode read Prometheus")

    assert _run(_config(tmp_path, "off"), read, []) == 0
    assert not (tmp_path / "state" / "poll-state.json").exists()


def test_the_poller_falls_back_to_the_cluster_ip_when_loopback_fails(tmp_path) -> None:
    asked = []
    healthy = _reads([])

    def read(base, query):
        asked.append(base)
        return None if base == "http://loopback" else healthy(base, query)

    assert _run(_config(tmp_path, "armed"), read, []) == 0
    assert asked[0] == "http://loopback"
    assert set(asked[1:]) == {"http://cluster-ip"}
    state = json.loads((tmp_path / "state" / "poll-state.json").read_text())
    assert state["baseline"] is True


def test_the_poller_answers_help(capsys) -> None:
    with pytest.raises(SystemExit) as exit_:
        poll.main(["claude_triage_poll.py", "--help"], "/nonexistent")
    assert exit_.value.code == 0
    assert "Poll Kuma and Alertmanager" in capsys.readouterr().out
