"""The Renovate floor: which changes need the operator's approval, and its three exceptions.

dotfiles#967 slice 1. Each rule gets a change it must pass and a change it must flag. The
coverage test holds every top-level entry of the tracked tree to a decision, so a new root
file cannot reach a session unclassified.

Run: uv run pytest hosts/components/renovate-agent/tests/test_renovate_floor.py
"""

import pytest
import renovate_floor as floor
from _helpers import REPO
from deploy_tools.land_lib import policy
from lib.proc_testing import run

FLOOR_FILE = (
    REPO / "hosts" / "components" / "renovate-agent" / "files" / "t2-approval-floor"
)
FLOOR = floor.read_floor(str(FLOOR_FILE))

OLD = "a" * 64
NEW = "b" * 64
HEAD = "c" * 40
DEFAULTS = "hosts/components/github-checks/defaults/main.yml"


def _bump(old=f"ghcr.io/x/y:1.2@sha256:{OLD}", new=f"ghcr.io/x/y:1.2@sha256:{NEW}"):
    return {
        "status": "modified",
        "filename": DEFAULTS,
        "patch": f"@@ -3,1 +3,1 @@\n-checks_image: {old}\n+checks_image: {new}",
    }


@pytest.mark.parametrize(
    "path",
    [
        "hosts/packages/homelab-tools/package.yml",
        "conftest.py",
        "k8s/roles/sonarr/tests/deep/conftest.py",
        "k8s/roles/monitor-bridge/files/registry.py",
        "docs/CLAUDE.md",
    ],
)
def test_a_path_on_the_floor_is_flagged(path):
    assert floor.floor_hits([{"status": "modified", "filename": path}], FLOOR) == [path]


@pytest.mark.parametrize(
    "path",
    [
        "k8s/roles/sonarr/defaults/main.yml",
        "k8s/roles/homelab-mcp-adjacent/defaults/main.yml",
    ],
)
def test_a_path_off_the_floor_is_clean(path):
    assert floor.floor_hits([{"status": "modified", "filename": path}], FLOOR) == []


def test_a_rename_out_of_the_floor_is_flagged():
    entry = {
        "status": "renamed",
        "filename": "docs/x.toml",
        "previous_filename": "prek.toml",
    }
    assert floor.floor_hits([entry], FLOOR) == ["prek.toml"]


def test_a_digest_only_bump_in_a_host_component_is_clean():
    assert floor.floor_hits([_bump()], FLOOR) == []


@pytest.mark.parametrize(
    "entry",
    [
        _bump(new=f"ghcr.io/x/y:1.3@sha256:{NEW}"),
        _bump(new=f"ghcr.io/x/y:1.2@sha256:{OLD}"),
        _bump(new=f"ghcr.io/x/y:1.2@sha256:{NEW}@sha256:{OLD}"),
        {**_bump(), "patch": None},
        {**_bump(), "status": "added"},
        {**_bump(), "filename": "hosts/packages/homelab-tools/package.yml"},
        {**_bump(), "patch": _bump()["patch"] + "\n+extra_flag: true"},
        {
            **_bump(),
            "patch": f"@@ -1 +1 @@\n-checks_url: x@sha256:{OLD}\n+checks_url: x@sha256:{NEW}",
        },
    ],
    ids=[
        "a tag change",
        "an unchanged line",
        "two digests",
        "no patch",
        "an added file",
        "a pinned binary",
        "an extra added line",
        "not an image line",
    ],
)
def test_a_change_that_is_not_digest_only_is_flagged(entry):
    assert floor.floor_hits([entry], FLOOR) != []


def test_a_branch_only_renovate_pushed_to_passes():
    events = [
        {
            "activity_type": "force_push",
            "actor": {"login": "renovate[bot]"},
            "after": HEAD,
        },
        {"activity_type": "branch_creation", "actor": {"login": "renovate[bot]"}},
        {"activity_type": "push", "actor": {"login": "DanielH2018"}},
    ]
    assert floor.pusher_problem(events, HEAD) == ""


@pytest.mark.parametrize(
    "events, names",
    [
        (
            [
                {
                    "activity_type": "push",
                    "actor": {"login": "DanielClaudeBot"},
                    "after": HEAD,
                },
                {
                    "activity_type": "branch_creation",
                    "actor": {"login": "renovate[bot]"},
                },
            ],
            "DanielClaudeBot pushed",
        ),
        (
            [
                {
                    "activity_type": "push",
                    "actor": {"login": "renovate[bot]"},
                    "after": HEAD,
                }
            ],
            "no branch creation",
        ),
        (
            [
                {
                    "activity_type": "push",
                    "actor": {"login": "renovate[bot]"},
                    "after": "d" * 40,
                },
                {
                    "activity_type": "branch_creation",
                    "actor": {"login": "renovate[bot]"},
                },
            ],
            "not the head",
        ),
    ],
)
def test_a_branch_anyone_else_touched_is_flagged(events, names):
    assert names in floor.pusher_problem(events, HEAD)


def test_renovates_own_passed_soak_and_no_soak_both_pass():
    soak = {"context": floor.SOAK_CONTEXT, "creator": {"login": "renovate[bot]"}}
    assert floor.soak_problem([{**soak, "state": "success"}]) == ""
    assert floor.soak_problem([{"context": "ci/tested-tree", "state": "failure"}]) == ""


@pytest.mark.parametrize(
    "statuses, names",
    [
        (
            [
                {
                    "context": floor.SOAK_CONTEXT,
                    "state": "success",
                    "creator": {"login": "DanielClaudeBot"},
                },
                {
                    "context": floor.SOAK_CONTEXT,
                    "state": "pending",
                    "creator": {"login": "renovate[bot]"},
                },
            ],
            "posted by DanielClaudeBot",
        ),
        (
            [
                {
                    "context": floor.SOAK_CONTEXT,
                    "state": "pending",
                    "creator": {"login": "renovate[bot]"},
                }
            ],
            "has not passed",
        ),
    ],
)
def test_a_soak_status_that_is_not_renovates_success_is_flagged(statuses, names):
    assert names in floor.soak_problem(statuses)


# Each top-level entry of the tracked tree that is not under a floor entry, and why a Renovate
# PR may change it unapproved. A new root entry fails the test below until it is on the floor
# or listed here.
OFF_FLOOR = {
    "k8s": "its floor paths are listed one by one: templates, role tests and role code",
    "inventory": "host and group vars the renderer reads, with no code a session runs",
    "cluster": "services.yml is on the floor; the rest is k8s content Flux applies",
    "docs": "pages a session may read, with no code a session runs",
    "CLAUDE.md": "on the floor through **/CLAUDE.md",
    "README.md": "prose",
    "SECURITY.md": "prose",
    "LICENSE": "prose",
    ".gitignore": "decides only what git tracks",
    ".gitleaks.toml": "the secret scan's allowlist, which CI's own gitleaks run reads",
    ".shellcheckrc": "lint config",
    ".vale.ini": "prose lint config",
    "styles": "prose lint rules",
    ".vscode": "editor settings no session reads",
}


def unclassified(roots: set[str]) -> list[str]:
    """The top-level entries neither on the floor nor listed in `OFF_FLOOR`."""
    return sorted(
        root
        for root in roots
        if root not in OFF_FLOOR
        and not floor.on_floor(root, FLOOR)
        and not floor.on_floor(f"{root}/x", FLOOR)
    )


def test_every_top_level_entry_is_on_the_floor_or_classified():
    listed = run(["git", "ls-files", "-z"], cwd=REPO, check=True).stdout
    roots = {path.split("/", 1)[0] for path in listed.split("\0") if path}
    for member in ("hosts", "scripts", "uv.lock", "k8s", "docs"):
        assert member in roots, f"{member} is gone from the tracked tree"
    assert unclassified(roots) == []


def test_an_unclassified_root_entry_is_flagged():
    assert unclassified({"newtool.toml", "hosts", "docs"}) == ["newtool.toml"]


def _tracked() -> list[str]:
    listed = run(["git", "ls-files", "-z"], cwd=REPO, check=True).stdout
    return [path for path in listed.split("\0") if path]


def test_both_landers_read_the_floor_file_alike():
    """The host lander cannot import land_lib, so the two matchers must agree on every path."""
    entries = policy.read_approval_paths(str(FLOOR_FILE))
    paths = _tracked() + ["conftest.py", "a/b/c/d/conftest.py", "docs/x.md"]
    host = {p for p in paths if floor.on_floor(p, FLOOR)}
    land = {p for p in paths if policy.listed(p, entries)}
    assert host == land
    assert "hosts/components/renovate-agent/files/t2-approval-floor" in host


def test_an_empty_floor_file_is_refused(tmp_path):
    empty = tmp_path / "floor"
    empty.write_text("# only a comment\n\n")
    with pytest.raises(ValueError, match="names no path"):
        floor.read_floor(str(empty))
