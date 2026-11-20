"""The lander decides alone what the unattended agent may land, on inputs the agent cannot write.

Each check gets an accepting and a refusing case. `main` is driven through `AgentTools.run`, so
the argv it would hand `gh` and `land.sh` is what these tests read.

Run: uv run pytest hosts/components/renovate-agent/tests/test_land_renovate_pr.py
"""

import json
import pathlib
import re

import agent_toolbox
import land_renovate_pr as lander
import pytest

ROLE = pathlib.Path(__file__).resolve().parents[1]
DENIED = frozenset({"authelia", "traefik"})
GOOD = {
    "number": 42,
    "author": {"login": "app/renovate", "is_bot": True},
    "isCrossRepository": False,
    "baseRefName": "master",
    "state": "OPEN",
    "headRefName": "renovate/k8s-image-sonarr",
    "headRefOid": "c" * 40,
    "files": [{"path": "k8s/roles/sonarr/defaults/main.yml"}],
}
HEAD = GOOD["headRefOid"]
REST_FILES = [{"status": "modified", "filename": "k8s/roles/sonarr/defaults/main.yml"}]
EVENTS = [
    {"activity_type": "push", "actor": {"login": "renovate[bot]"}, "after": HEAD},
    {"activity_type": "branch_creation", "actor": {"login": "renovate[bot]"}},
]
UNMERGED = (0, json.dumps({"merged": False}))
SOAKED = [
    {
        "context": "renovate/stability-days",
        "state": "success",
        "creator": {"login": "renovate[bot]"},
    }
]


@pytest.mark.parametrize("arg", ["3340", "1", "9999999"])
def test_a_plain_pr_number_is_accepted(arg):
    assert lander.pr_number(arg) == int(arg)


@pytest.mark.parametrize(
    "arg", ["0", "-1", "03340", "3340;id", "3340 ", "1e3", "", "12345678"]
)
def test_anything_but_a_plain_pr_number_is_refused(arg):
    assert lander.pr_number(arg) is None


def test_a_renovate_pr_touching_no_denied_role_passes_every_check():
    assert lander.refusals(GOOD, DENIED) == []


@pytest.mark.parametrize(
    "change, names",
    [
        ({"author": {"login": "DanielH2018"}}, "author"),
        ({"isCrossRepository": True}, "repository"),
        ({"baseRefName": "release"}, "base"),
        ({"state": "MERGED"}, "open"),
        ({"headRefName": "worktree-renovate-auto-3340"}, "renovate/"),
        ({"headRefName": None}, "does not parse"),
        ({"number": "42"}, "does not parse"),
        (
            {
                "headRefName": "renovate/k8s-image-navidrome-(manual-k8s_autodeploy-false)"
            },
            "k8s_autodeploy-false",
        ),
        ({"files": [{"path": "k8s/roles/authelia/defaults/main.yml"}]}, "authelia"),
        ({"files": []}, "cap"),
        ({"files": [{"path": "inventory/group_vars/all.yml"}]}, "inventory"),
        ({"headRefOid": "c" * 39}, "head SHA"),
        ({"files": [{"path": f"f{i}"} for i in range(lander.FILE_CAP)]}, "cap"),
    ],
)
def test_each_check_refuses_the_pr_that_breaks_it(change, names):
    # fact: .claude/skills/renovate-prs/SKILL.md#When to stop and say so
    reasons = lander.refusals({**GOOD, **change}, DENIED)
    assert len(reasons) == 1 and names in reasons[0], reasons


def test_the_denylist_is_the_first_line_of_the_output():
    assert lander.denied_roles("authelia,traefik\n") == DENIED
    assert lander.denied_roles("authelia,traefik\nuv noise, on stderr\n") == DENIED


@pytest.mark.parametrize(
    "output", ["", "\n", "Bytecode compiled 2189 files in 69ms\n", "authelia,\n"]
)
def test_output_holding_no_list_is_not_read_as_nothing_denied(output):
    assert lander.denied_roles(output) is None


class _Run:
    """Answers `gh pr view`, the denylist command and land.sh, recording every argv and cwd."""

    def __init__(
        self,
        pr,
        land_out="== 6/6\nVERDICT: settled (PR #3340)\n",
        land_rc=0,
        denylist=(0, "authelia,traefik\n"),
        files=REST_FILES,
        events=EVENTS,
        statuses=SOAKED,
        rest_pr=UNMERGED,
    ):
        self.pr, self.land_out, self.land_rc, self.calls = pr, land_out, land_rc, []
        self.denylist, self.cwds, self.rest_pr = denylist, [], rest_pr
        self.api = {"/files?": files, "/activity?": events, "/statuses?": statuses}

    def __call__(self, argv, cwd=None, timeout=120, stdin_text=None):
        self.calls.append(argv)
        self.cwds.append(cwd)
        if argv[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps(self.pr)
        if argv == list(lander.DENYLIST_ARGV):
            return self.denylist
        if argv == ["gh", "api", "repos/DanielH2018/server/pulls/3340"]:
            return self.rest_pr
        if argv[:2] == ["gh", "api"]:
            (answer,) = [v for k, v in self.api.items() if k in argv[-1]]
            return (0, json.dumps([answer])) if answer is not None else (1, "HTTP 502")
        return self.land_rc, self.land_out


FLOOR_FILE = ROLE / "files" / "t2-approval-floor"


def _main(tmp_path, run, floor_file=FLOOR_FILE):
    tools = agent_toolbox.AgentTools(run=run)
    rc = lander.main(
        ["land_renovate_pr.py", "3340"], tools, str(tmp_path / "out"), str(floor_file)
    )
    return rc, (tmp_path / "out" / "3340.verdict").read_text()


def test_a_missing_floor_file_refuses_before_land_sh(tmp_path):
    run = _Run(GOOD)
    rc, result = _main(tmp_path, run, floor_file=tmp_path / "absent")
    assert rc == lander.REFUSED_EXIT and "floor is unusable" in result
    assert not any("land.sh" in " ".join(argv) for argv in run.calls)


def test_the_denylist_comes_from_the_derivation_in_the_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("RENOVATE_AGENT_REPO_DIR", "/srv/checkout")
    run = _Run(GOOD)
    _main(tmp_path, run)
    assert run.calls[0] == [
        "uv",
        "run",
        "--frozen",
        "python",
        "scripts/dev/k8s_autodeploy_counts.py",
        "--denylist",
    ]
    assert run.cwds[0] == "/srv/checkout"


def test_a_denied_role_from_the_derivation_refuses_the_pr(tmp_path):
    files = [{"path": "k8s/roles/authelia/defaults/main.yml"}]
    run = _Run({**GOOD, "files": files})
    rc, result = _main(tmp_path, run)
    assert rc == lander.REFUSED_EXIT and "authelia" in result
    assert not any(argv[0].endswith("land.sh") for argv in run.calls)


def test_a_passing_pr_is_landed_and_only_the_verdict_line_is_recorded(tmp_path):
    run = _Run(GOOD)
    rc, result = _main(tmp_path, run)
    assert rc == 0 and result == "VERDICT: settled (PR #3340)\n"
    land = run.calls[-1]
    assert land[:2] == ["env", f"LAND_REQUIRE_HEAD={HEAD}"]
    assert land[2].endswith("scripts/deploy_tools/land.sh")
    assert land[3:] == ["--pr", "3340", "--arm-merge", "--await-merge"]


LAND_REFUSED = (
    "LAND-RESULT: verdict=refused cause=- exit=1 pr=3340 sha=- tags=0\n"
    "VERDICT: refused (PR #3340 — could not read PR #3340: GraphQL: API rate limit "
    "already exceeded)\n"
)


def test_a_land_sh_refusal_on_an_unmerged_pr_exits_as_a_refusal(tmp_path):
    rc, result = _main(tmp_path, _Run(GOOD, land_out=LAND_REFUSED, land_rc=1))
    assert rc == lander.REFUSED_EXIT
    assert result.startswith("VERDICT: refused (PR #3340")


@pytest.mark.parametrize(
    "rest_pr",
    [
        (0, json.dumps({"merged": True})),
        (1, "HTTP 403: API rate limit exceeded"),
        (0, "not json"),
        (0, json.dumps({})),
    ],
)
def test_a_land_sh_refusal_on_a_merged_or_unread_pr_keeps_its_exit(tmp_path, rest_pr):
    run = _Run(GOOD, land_out=LAND_REFUSED, land_rc=1, rest_pr=rest_pr)
    rc, _ = _main(tmp_path, run)
    assert rc == 1


def test_a_land_sh_crash_on_an_unmerged_pr_keeps_its_exit(tmp_path):
    out = (
        "LAND-RESULT: verdict=refused cause=- exit=1 pr=3340 sha=- tags=0\n"
        "VERDICT: refused (PR #3340 — the landing raised KeyError: 'files')\n"
        "Traceback (most recent call last):\n"
        '  File "land.py", line 332, in main\n'
        "KeyError: 'files'\n"
    )
    rc, _ = _main(tmp_path, _Run(GOOD, land_out=out, land_rc=1))
    assert rc == 1


def test_a_failure_after_the_merge_keeps_land_sh_s_exit(tmp_path):
    out = "VERDICT: ci-red (master CI failed on abcdef12)\n"
    rc, result = _main(tmp_path, _Run(GOOD, land_out=out, land_rc=1))
    assert rc == 1 and result == out


def test_a_floor_path_lands_only_with_the_operators_approval(tmp_path):
    files = [{"status": "modified", "filename": "uv.lock"}]
    run = _Run(GOOD, files=files)
    _main(tmp_path, run)
    assert run.calls[-1][:4] == [
        "env",
        f"LAND_REQUIRE_HEAD={HEAD}",
        "LAND_REQUIRE_APPROVAL=1",
        "LAND_APPROVER=DanielH2018",
    ]


def test_another_pusher_makes_the_landing_need_approval(tmp_path):
    events = [{**EVENTS[0], "actor": {"login": "DanielClaudeBot"}}, EVENTS[1]]
    run = _Run(GOOD, events=events)
    _main(tmp_path, run)
    assert "LAND_REQUIRE_APPROVAL=1" in run.calls[-1]


def test_a_soak_status_another_account_posted_is_refused(tmp_path):
    statuses = [{**SOAKED[0], "creator": {"login": "DanielClaudeBot"}}]
    rc, result = _main(tmp_path, _Run(GOOD, statuses=statuses))
    assert rc == lander.REFUSED_EXIT and "posted by DanielClaudeBot" in result


def test_an_unreadable_branch_activity_refuses(tmp_path):
    run = _Run(GOOD, events=None)
    rc, result = _main(tmp_path, run)
    assert rc == lander.REFUSED_EXIT and "branch activity" in result
    assert not any("land.sh" in " ".join(argv) for argv in run.calls)


def test_a_refused_pr_never_reaches_land_sh(tmp_path):
    run = _Run({**GOOD, "author": {"login": "DanielH2018"}})
    rc, result = _main(tmp_path, run)
    assert rc == lander.REFUSED_EXIT and result.startswith("REFUSED: its author")
    assert not any(argv[0].endswith("land.sh") for argv in run.calls)


@pytest.mark.parametrize(
    "denylist, names",
    [
        (
            (1, "k8s_autodeploy_counts.py: role 'x' has no defaults/main.yml\n"),
            "exited 1",
        ),
        ((124, "timed out after 120s\n"), "exited 124"),
        ((0, ""), "no list"),
    ],
)
def test_a_failed_denylist_refuses_before_asking_github(tmp_path, denylist, names):
    run = _Run(GOOD, denylist=denylist)
    rc, result = _main(tmp_path, run)
    assert rc == lander.REFUSED_EXIT and "--denylist" in result and names in result
    assert run.calls == [list(lander.DENYLIST_ARGV)]


def test_an_earlier_verdict_is_overwritten_before_the_checks_run(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "3340.verdict").write_text(
        "VERDICT: settled (an older landing)\n"
    )
    rc, result = _main(tmp_path, _Run("not a dict"))
    assert rc == lander.REFUSED_EXIT and result.startswith(
        "REFUSED: cannot read the PR"
    )


def test_the_polkit_rule_admits_exactly_the_unit_names_the_lander_accepts():
    rule = (ROLE / "templates" / "50-renovate-agent-land.rules.j2").read_text()
    found = re.search(r"if \(!/(.+?)/\.test\(action\.lookup\(\"unit\"\)\)\)", rule)
    assert found is not None, "the rule no longer tests the unit name against a regex"
    pattern = found.group(1)
    for arg in ["3340", "1", "9999999", "0", "-1", "03340", "3340;id", "12345678", "a"]:
        unit = f"renovate-agent-land@{arg}.service"
        assert bool(re.fullmatch(pattern.strip("^$"), unit)) == (
            lander.pr_number(arg) is not None
        ), unit
    assert re.search(r'action\.lookup\("verb"\) !== "start"', rule), (
        "only `start` may pass"
    )
