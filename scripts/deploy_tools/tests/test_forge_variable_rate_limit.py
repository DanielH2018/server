"""`GitHub.variable` waits out a rate-limit 403 instead of failing the DEPLOY_HOLD read (#7993).

`promote.py land-wait` reads DEPLOY_HOLD on every poll, and on 2026-11-11 the shared token's
exhausted quota failed that read during PR 7984's promotion wait.

Run: uv run pytest scripts/deploy_tools/tests/test_forge_variable_rate_limit.py
"""

import subprocess

import pytest

import forge

LIMITED = "gh: API rate limit exceeded for user ID 41801780. (HTTP 403)"


def _no_rest(*_args, **_kwargs):
    raise AssertionError("the variable read does not use REST")


def _github(answers: list[tuple[int, str, str]]):
    sleeps: list[float] = []
    argvs: list[list[str]] = []

    def run(argv, **_kwargs):
        argvs.append(argv)
        if argv[:3] == ["gh", "api", "rate_limit"]:
            return subprocess.CompletedProcess(argv, 0, "9999999999\n", "")
        code, out, err = answers.pop(0)
        return subprocess.CompletedProcess(argv, code, out, err)

    return forge.GitHub(_no_rest, None, run=run, sleep=sleeps.append), sleeps, argvs


def test_a_rate_limited_read_waits_and_reads_again():
    gh, sleeps, argvs = _github([(1, "", LIMITED), (0, "none\n", "")])
    assert gh.variable("DEPLOY_HOLD") == "none"
    assert len(sleeps) == 1 and sleeps[0] == 3600
    assert [a[:3] for a in argvs].count(["gh", "variable", "get"]) == 2


def test_a_read_rate_limited_past_its_waits_raises():
    gh, sleeps, _ = _github([(1, "", LIMITED)] * (forge.RATE_LIMIT_WAITS + 1))
    with pytest.raises(forge.ForgeError, match="rate limit exceeded"):
        gh.variable("DEPLOY_HOLD")
    assert len(sleeps) == forge.RATE_LIMIT_WAITS
