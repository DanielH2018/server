"""Behaviour of the poller that claude-triage-poll.timer runs every two minutes.

Each test drives `step` or `main` through its seams: no Prometheus, no systemctl.
"""

import json
from datetime import datetime, timezone

import claude_triage_poll as poll
import claude_triage_run as run
import pytest

T0 = datetime(2026, 10, 15, 18, 30, tzinfo=timezone.utc).timestamp()
MEMORY = {"kuma:Memory": {"source": "kuma", "monitor": "Memory"}}


def advance(state, down, **kw):
    args = {
        "hold": False,
        "muted": False,
        "now": T0,
        "streak_needed": 2,
        "storm_size": 3,
    }
    args.update(kw)
    return poll.step(state, down, **args)


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
    state, _ = advance(state, {})
    state, episodes = advance(state, MEMORY)
    assert episodes == []


def test_a_recovery_closes_the_episode_so_the_next_fall_is_new() -> None:
    state, _ = advance({}, {})
    state, _ = advance(state, MEMORY)
    state, _ = advance(state, MEMORY)
    state, _ = advance(state, {})
    assert state["keys"] == {}
    state, _ = advance(state, MEMORY)
    state, episodes = advance(state, MEMORY)
    assert len(episodes) == 1


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
    kuma = [{"metric": {"monitor_name": "Memory"}}]
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
    path = tmp_path / "poll.env"
    path.write_text(
        f"STATE_DIR={state}\nPOLL_MODE={mode}\n"
        "PROM_URLS=http://loopback http://cluster-ip\nPOLL_STREAK=1\n"
    )
    return str(path)


def _reads(*kuma_by_poll):
    """A `read` seam answering each poll's Kuma query in turn, with no alerts firing."""
    polls = iter(kuma_by_poll)

    def read(base, query):
        return next(polls) if query == poll.KUMA_QUERY else []

    return read


def _run(config, read, started):
    return poll.main(
        ["claude_triage_poll.py"],
        config,
        read,
        lambda state_dir, episode: started.append(episode),
        lambda: T0,
    )


@pytest.mark.parametrize("mode", ["armed", "shadow"])
def test_armed_mode_starts_a_run_and_shadow_mode_only_logs_it(tmp_path, mode) -> None:
    config = _config(tmp_path, mode)
    started = []
    read = _reads([], [{"metric": {"monitor_name": "Memory"}}])
    assert _run(config, read, started) == 0  # the baseline
    assert _run(config, read, started) == 0
    shadow = tmp_path / "state" / "shadow.jsonl"
    if mode == "armed":
        assert [e["monitor"] for e in started] == ["Memory"]
        assert not shadow.exists()
    else:
        assert started == []
        (line,) = shadow.read_text().splitlines()
        assert json.loads(line)["monitor"] == "Memory"


def test_off_mode_reads_nothing(tmp_path) -> None:
    def read(base, query):
        raise AssertionError("off mode read Prometheus")

    assert _run(_config(tmp_path, "off"), read, []) == 0
    assert not (tmp_path / "state" / "poll-state.json").exists()


def test_the_poller_falls_back_to_the_cluster_ip_when_loopback_fails(tmp_path) -> None:
    asked = []

    def read(base, query):
        asked.append(base)
        return None if base == "http://loopback" else []

    assert _run(_config(tmp_path, "armed"), read, []) == 0
    assert asked == ["http://loopback", "http://cluster-ip", "http://cluster-ip"]
    state = json.loads((tmp_path / "state" / "poll-state.json").read_text())
    assert state["baseline"] is True


def test_the_poller_answers_help(capsys) -> None:
    with pytest.raises(SystemExit) as exit_:
        poll.main(["claude_triage_poll.py", "--help"], "/nonexistent")
    assert exit_.value.code == 0
    assert "Poll Kuma and Alertmanager" in capsys.readouterr().out
