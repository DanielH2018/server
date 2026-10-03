"""The lander decides alone what the unattended agent may land, on inputs the agent cannot write.

Each check gets an accepting and a refusing case. `main` is driven through `AgentTools.run`, so
the argv it would hand `gh` and `land.sh` is what these tests read.

Run: uv run pytest ansible/roles/setup/renovate_agent/tests/test_land_renovate_pr.py
"""

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
import agent_toolbox
import land_renovate_pr as lander
import pytest

ROLE = pathlib.Path(__file__).resolve().parents[1]
DENIED = frozenset({"authelia", "traefik"})
GOOD = {
    "author": {"login": "app/renovate", "is_bot": True},
    "isCrossRepository": False,
    "baseRefName": "master",
    "state": "OPEN",
    "headRefName": "renovate/k8s-image-sonarr",
    "files": [{"path": "ansible/roles/k8s/sonarr/defaults/main.yml"}],
}


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
    passed = [{"context": "renovate/stability-days", "state": "SUCCESS"}]
    assert lander.refusals({**GOOD, "statusCheckRollup": passed}, DENIED) == []


@pytest.mark.parametrize(
    "change, names",
    [
        ({"author": {"login": "DanielH2018"}}, "author"),
        ({"isCrossRepository": True}, "repository"),
        ({"baseRefName": "release"}, "base"),
        ({"state": "MERGED"}, "open"),
        ({"headRefName": "worktree-renovate-auto-3340"}, "renovate/"),
        (
            {
                "headRefName": "renovate/k8s-image-navidrome-(manual-k8s_autodeploy-false)"
            },
            "k8s_autodeploy-false",
        ),
        (
            {"files": [{"path": "ansible/roles/k8s/authelia/defaults/main.yml"}]},
            "authelia",
        ),
        ({"files": []}, "cap"),
        ({"files": [{"path": "ansible/inventory/group_vars/all.yml"}]}, "inventory"),
        (
            {
                "statusCheckRollup": [
                    {"context": "renovate/stability-days", "state": "PENDING"}
                ]
            },
            "stability-days",
        ),
        ({"files": [{"path": f"f{i}"} for i in range(lander.FILE_CAP)]}, "cap"),
    ],
)
def test_each_check_refuses_the_pr_that_breaks_it(change, names):
    reasons = lander.refusals({**GOOD, **change}, DENIED)
    assert len(reasons) == 1 and names in reasons[0], reasons


def test_an_empty_denylist_is_not_read_as_nothing_denied():
    assert lander.denied_roles({"K8S_AUTODEPLOY_DENYLIST": ""}) is None
    assert (
        lander.denied_roles({"K8S_AUTODEPLOY_DENYLIST": "authelia,traefik"}) == DENIED
    )


class _Run:
    """Answers `gh pr view` with `pr` and land.sh with `land_out`, recording every argv."""

    def __init__(self, pr, land_out="== 6/6\nVERDICT: settled (PR #3340)\n", land_rc=0):
        self.pr, self.land_out, self.land_rc, self.calls = pr, land_out, land_rc, []

    def __call__(self, argv, cwd=None, timeout=120, stdin_text=None):
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps(self.pr)
        return self.land_rc, self.land_out


@pytest.fixture
def deployer_config(tmp_path):
    path = tmp_path / "config.env"
    path.write_text("K8S_AUTODEPLOY_DENYLIST=authelia,traefik\n")
    return str(path)


def _main(tmp_path, run, config):
    tools = agent_toolbox.AgentTools(run=run)
    rc = lander.main(
        ["land_renovate_pr.py", "3340"], tools, config, str(tmp_path / "out")
    )
    return rc, (tmp_path / "out" / "3340.verdict").read_text()


def test_a_passing_pr_is_landed_and_only_the_verdict_line_is_recorded(
    tmp_path, deployer_config
):
    run = _Run(GOOD)
    rc, result = _main(tmp_path, run, deployer_config)
    assert rc == 0 and result == "VERDICT: settled (PR #3340)\n"
    land = run.calls[-1]
    assert land[0].endswith("scripts/deploy_tools/land.sh")
    assert land[1:] == ["--pr", "3340", "--arm-merge", "--await-merge"]


def test_a_refused_pr_never_reaches_land_sh(tmp_path, deployer_config):
    run = _Run({**GOOD, "author": {"login": "DanielH2018"}})
    rc, result = _main(tmp_path, run, deployer_config)
    assert rc == 3 and result.startswith("REFUSED: its author")
    assert all(argv[:3] == ["gh", "pr", "view"] for argv in run.calls)


def test_an_unreadable_denylist_refuses_before_asking_github(tmp_path):
    run = _Run(GOOD)
    rc, result = _main(tmp_path, run, str(tmp_path / "missing.env"))
    assert rc == 3 and "denylist" in result and run.calls == []


def test_an_earlier_verdict_is_overwritten_before_the_checks_run(
    tmp_path, deployer_config
):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "3340.verdict").write_text(
        "VERDICT: settled (an older landing)\n"
    )
    rc, result = _main(tmp_path, _Run("not a dict"), deployer_config)
    assert rc == 3 and result.startswith("REFUSED: cannot read the PR")


def test_the_polkit_rule_admits_exactly_the_unit_names_the_lander_accepts():
    rule = (ROLE / "templates" / "50-renovate-agent-land.rules.j2").read_text()
    pattern = re.search(
        r"if \(!/(.+?)/\.test\(action\.lookup\(\"unit\"\)\)\)", rule
    ).group(1)
    for arg in ["3340", "1", "9999999", "0", "-1", "03340", "3340;id", "12345678", "a"]:
        unit = f"renovate-agent-land@{arg}.service"
        assert bool(re.fullmatch(pattern.strip("^$"), unit)) == (
            lander.pr_number(arg) is not None
        ), unit
    assert re.search(r'action\.lookup\("verb"\) !== "start"', rule), (
        "only `start` may pass"
    )
