#!/usr/bin/env python3
"""What `land.sh` asks `land_tags` BEYOND the tag list, to reach a verdict.

Two questions, each behind a landing that read as success over unfinished work:

* `service_tags_at` -- which tags exist AT A REF (issue #1544). Asked of a checkout, a role the
  PR itself registers reads as UNREGISTERED, which is what a role nobody registered reads as
  too, so the landing prints the expensive remedy for work one `--tags` run does.
* `self_applied_command` -- what a hand runs when the tick CONVERGED without applying the PR
  (issue #1537).

Its own module rather than test_land_tags.py: that file pins the DERIVATION against a fixture
and patches `declared_tags` away, while these read real git and the real remediation text.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tags_verdict_inputs.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deploy_tags
import land_tags


# ── service_tags_at: which tags exist AT A REF, not in the working tree (issue #1544) ──


def _repo_git(repo, *args: str) -> str:
    """Run one git command in `repo` with every inherited GIT_* variable removed.

    Unscrubbed, GIT_DIR/GIT_INDEX_FILE from a prek hook redirect these writes at the real
    repository — the failure that is invisible from the run that causes it.
    """
    import os
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.invalid"
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def two_commit_repo(tmp_path):
    """A repo whose SECOND commit registers `pihole-exporter`; yields (repo, first, second).

    The shape of PR #1539: the role and its inventory entry arrive together, so the entry
    exists at the merge commit and in no checkout until the tick fast-forwards.
    """
    repo = tmp_path / "repo"
    inventory = repo / "ansible" / "inventory" / "host_vars"
    inventory.mkdir(parents=True)
    box = inventory / "daniel-box.yml"
    (inventory / "_example.yml").write_text("containers_list:\n  - name: not-a-host\n")
    _repo_git(repo, "init", "-q", "-b", "master")
    box.write_text("containers_list:\n  - name: sonarr\n")
    _repo_git(repo, "add", "-A")
    _repo_git(repo, "commit", "-q", "-m", "before", "--no-gpg-sign")
    first = _repo_git(repo, "rev-parse", "HEAD")
    box.write_text("containers_list:\n  - name: sonarr\n  - name: pihole-exporter\n")
    _repo_git(repo, "add", "-A")
    _repo_git(repo, "commit", "-q", "-m", "register it", "--no-gpg-sign")
    return repo, first, _repo_git(repo, "rev-parse", "HEAD")


def test_a_tag_registered_at_the_ref_is_declared_there(two_commit_repo):
    repo, _first, second = two_commit_repo
    assert land_tags.service_tags_at(second, repo) == {"sonarr", "pihole-exporter"}


def test_the_same_tag_is_not_declared_at_the_earlier_ref(two_commit_repo):
    """The must-not-fire half: read a commit before the entry and it is genuinely absent."""
    repo, first, _second = two_commit_repo
    assert land_tags.service_tags_at(first, repo) == {"sonarr"}


def test_the_example_host_is_not_a_source_of_tags_at_a_ref_either(two_commit_repo):
    """`host_files` excludes `_example.yml` on disk; the ref reader must exclude it too."""
    repo, _first, second = two_commit_repo
    assert "not-a-host" not in land_tags.service_tags_at(second, repo)


def test_this_repo_at_head_agrees_with_its_own_working_tree():
    """Non-vacuity. A reader that found no inventory file at all returns an empty set, which
    satisfies every assertion above about a tag being absent."""
    at_head = land_tags.service_tags_at("HEAD", deploy_tags.REPO)
    assert "traefik" in at_head
    assert at_head == deploy_tags.service_tags()


# ── self_applied_command: the hand-apply the tick would otherwise have done (issue #1537) ──


def test_the_command_names_the_role_the_tick_would_have_applied():
    files = ["ansible/roles/setup/renovate_agent/files/renovate_agent.py"]
    command = land_tags.self_applied_command(files)
    assert "ansible/initial_setup.yml --tags renovate_agent" in command
    assert "merge --ff-only" in command


@pytest.mark.parametrize(
    "files",
    [
        ["docs/gitops-pipeline.md"],
        ["ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"],
        ["ansible/bootstrap.yml"],
    ],
)
def test_the_command_is_empty_for_a_pr_the_tick_never_applies(files):
    """The must-not-fire half, over exactly the paths `self_applied` answers False for: a
    command here would send an operator at a plane apply their PR never needed."""
    assert land_tags.self_applied_command(files) == ""
    assert land_tags.self_applied(files) is False
