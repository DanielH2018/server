"""Behaviour of the boot-time resume that claude-bg-resume.service runs.

`main` is driven through its `Tools` seam, so no test starts a Claude supervisor.
"""

import json
from dataclasses import replace

import bg_session_resume as resume
import pytest

NOW_MS = 1_792_931_969_762
FRESH = NOW_MS - 3600 * 1000


def roster(*shorts: str, updated_at: int = FRESH) -> dict:
    return {"updatedAt": updated_at, "workers": {s: {"pid": 1} for s in shorts}}


def bg(short: str, pid: int | None = None, state: str = "working") -> dict:
    session = {"id": short, "kind": "background", "state": state}
    if pid is not None:
        session["pid"] = pid
    return session


def test_a_roster_worker_with_no_live_process_is_a_candidate() -> None:
    sessions = [bg("aaaa1111", state="working"), bg("bbbb2222", state="blocked")]
    assert resume.candidates(roster("aaaa1111", "bbbb2222"), sessions, NOW_MS) == [
        "aaaa1111",
        "bbbb2222",
    ]


def test_a_session_still_running_is_left_alone() -> None:
    sessions = [bg("aaaa1111", pid=4242)]
    assert resume.candidates(roster("aaaa1111"), sessions, NOW_MS) == []


def test_a_session_the_roster_does_not_list_is_left_alone() -> None:
    # The supervisor had already stopped it for idling, so it had no Remote Control link to lose.
    sessions = [bg("aaaa1111", state="blocked")]
    assert resume.candidates(roster(), sessions, NOW_MS) == []


def test_a_roster_worker_that_was_deleted_is_left_alone() -> None:
    assert resume.candidates(roster("aaaa1111"), [], NOW_MS) == []


def test_an_interactive_session_is_left_alone() -> None:
    sessions = [{"id": "aaaa1111", "kind": "interactive"}]
    assert resume.candidates(roster("aaaa1111"), sessions, NOW_MS) == []


def test_a_roster_older_than_48_hours_resumes_nothing() -> None:
    stale = roster("aaaa1111", updated_at=NOW_MS - 49 * 3600 * 1000)
    assert resume.candidates(stale, [bg("aaaa1111")], NOW_MS) == []


@pytest.fixture
def box(tmp_path):
    """A roster on disk, the respawns made, and `Tools` fakes over both.

    The fake respawn rewrites the roster with only the session it started, which is what a
    freshly started supervisor does to the workers it did not find alive.
    """
    path = tmp_path / "roster.json"
    path.write_text(json.dumps(roster("aaaa1111", "bbbb2222")))
    calls = []

    def respawn(claude, short):
        calls.append(short)
        path.write_text(json.dumps(roster(short)))
        return True

    tools = resume.Tools(
        list_sessions=lambda claude: [bg("aaaa1111"), bg("bbbb2222")],
        respawn=respawn,
        wait_for_dns=lambda host, budget: True,
        now_ms=lambda: NOW_MS,
    )
    return ["--claude", "claude", "--roster", str(path)], tools, calls


def test_every_candidate_is_read_before_the_first_respawn(box) -> None:
    argv, tools, calls = box
    assert resume.main(argv, tools) == 0
    assert calls == ["aaaa1111", "bbbb2222"]


def test_dry_run_respawns_nothing(box, capsys) -> None:
    argv, tools, calls = box
    assert resume.main([*argv, "--dry-run"], tools) == 0
    assert calls == []
    assert "2 session(s) to respawn: aaaa1111 bbbb2222" in capsys.readouterr().out


def test_unresolved_dns_respawns_nothing_and_fails(box) -> None:
    argv, tools, calls = box
    no_dns = replace(tools, wait_for_dns=lambda host, budget: False)
    assert resume.main(argv, no_dns) == 1
    assert calls == []


def test_a_failed_respawn_fails_the_run_after_trying_the_rest(box) -> None:
    argv, tools, calls = box
    first_fails = replace(
        tools, respawn=lambda claude, short: calls.append(short) or short != "aaaa1111"
    )
    assert resume.main(argv, first_fails) == 1
    assert calls == ["aaaa1111", "bbbb2222"]


def test_no_roster_is_nothing_to_do(tmp_path) -> None:
    tools = resume.Tools(list_sessions=lambda claude: pytest.fail("listed sessions"))
    argv = ["--claude", "claude", "--roster", str(tmp_path / "roster.json")]
    assert resume.main(argv, tools) == 0


def test_dns_wait_retries_until_the_name_resolves() -> None:
    answers = iter([OSError("ENOTFOUND"), OSError("ENOTFOUND"), None])

    def resolve(host):
        answer = next(answers)
        if answer:
            raise answer

    clock = iter(range(100))
    assert resume.wait_for_dns(
        "api.anthropic.com",
        60,
        resolve=resolve,
        sleep=lambda s: None,
        clock=lambda: next(clock),
    )


def test_dns_wait_gives_up_at_its_budget() -> None:
    def resolve(host):
        raise OSError("ENOTFOUND")

    clock = iter(range(0, 1000, 10))
    assert not resume.wait_for_dns(
        "api.anthropic.com",
        30,
        resolve=resolve,
        sleep=lambda s: None,
        clock=lambda: next(clock),
    )
