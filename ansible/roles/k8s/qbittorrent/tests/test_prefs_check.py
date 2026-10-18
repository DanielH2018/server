"""Tests for the qbittorrent-prefs-check CronJob's entry point.

`run` takes apply_prefs.main as `check`, so each case drives the verdict mapping with a
stand-in that prints what the real check prints and returns its exit code.
"""

import apply_prefs
import prefs_check

_CREDS = {"QBT_CREDENTIALS": "admin\nhunter2"}


def _check_returning(status: int, output: str = ""):
    calls: list[list[str]] = []

    def check(argv: list[str]) -> int:
        calls.append(argv)
        if output:
            print(output)
        return status

    return check, calls


def _run(env: dict[str, str], check) -> tuple[int, list[str]]:
    lines: list[str] = []
    return prefs_check.run(env, check=check, emit=lines.append), lines


def test_a_match_exits_zero_with_the_ok_line() -> None:
    check, calls = _check_returning(apply_prefs.EXIT_OK, "8 preferences already match")
    code, lines = _run(_CREDS, check)
    assert code == 0
    assert lines == [
        "qbittorrent-prefs-check: ok: preferences match: 8 preferences already match"
    ]
    assert calls == [["--dry-run", "--connect-wait", "60"]]


def test_drift_is_a_result_not_a_failure() -> None:
    check, _ = _check_returning(
        apply_prefs.EXIT_DRIFT,
        "would set connection_speed: 30 -> 100\nwould set max_connec: 500 -> 1000",
    )
    code, lines = _run(_CREDS, check)
    assert code == 0
    assert lines == [
        "qbittorrent-prefs-check: DRIFT: would set connection_speed: 30 -> 100 "
        "would set max_connec: 500 -> 1000"
    ]


def test_an_unreachable_webui_fails_the_job() -> None:
    check, _ = _check_returning(apply_prefs.EXIT_UNREACHABLE, "no response within 60s")
    code, lines = _run(_CREDS, check)
    assert code == 1
    assert lines[0].startswith("qbittorrent-prefs-check: cannot reach qbittorrent: ")


def test_an_unknown_exit_is_reported_as_unexpected() -> None:
    check, _ = _check_returning(7)
    code, lines = _run(_CREDS, check)
    assert code == 1
    assert lines == ["qbittorrent-prefs-check: unexpected exit 7"]


def test_the_two_line_secret_becomes_apply_prefs_credentials() -> None:
    assert prefs_check.split_credentials("admin\nhunter2:x") == ("admin", "hunter2:x")


def test_a_malformed_secret_is_refused_without_calling_the_check() -> None:
    for value in ("", "admin", "admin\n", "admin\npw\nextra"):
        check, calls = _check_returning(apply_prefs.EXIT_OK)
        code, lines = _run({"QBT_CREDENTIALS": value}, check)
        assert (code, calls) == (2, [])
        assert lines == [
            "qbittorrent-prefs-check: credentials missing from QBT_CREDENTIALS"
        ]
