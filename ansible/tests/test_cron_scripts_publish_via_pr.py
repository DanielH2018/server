"""The unattended crons that commit must publish through a PR, and must say why they failed.

Both properties were learned on 2026-08-25. docs-refresh.sh and secret-rotate.sh each ended
in a direct write to the default branch, which a repository ruleset rejects outright
("Required status check ... is expected"), so neither had ever published. And both sent the
failure to /dev/null, so the alert said it failed and nothing said why -- the cause stayed
invisible until the rejected command was run by hand.

secret-rotate is the more consequential of the two: it changes a live credential and
redeploys its consumer BEFORE publishing, so an unpublished rotation leaves the running value
and origin disagreeing, and the next deploy from origin re-applies the superseded one.

These are text assertions over the templates, which is the weaker kind of guard -- an
indirection through a variable or a helper would slip past. They are still worth having,
because the regression they cover is someone reinstating a direct write, and that is a
literal line of shell.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATES = REPO / "ansible/roles/setup/initial_setup/templates"

DOCS_REFRESH = TEMPLATES / "docs-refresh.sh.j2"
SECRET_ROTATE = TEMPLATES / "secret-rotate.sh.j2"

# (path, the commit invocation as it appears in that script). secret-rotate splits its commit
# across lines and passes --no-verify, so a single shared marker would silently match neither.
SCRIPTS = [
    pytest.param(DOCS_REFRESH, 'git commit -m "docs:', id="docs-refresh"),
    pytest.param(SECRET_ROTATE, "commit --no-verify -m", id="secret-rotate"),
]


def read(path: Path) -> str:
    text = path.read_text()
    assert len(text) > 2000, f"{path.name} is only {len(text)} bytes — has it moved?"
    return text


def code_lines(path: Path) -> list[str]:
    """Shell lines only. Both headers document the old direct write at length, and a comment
    quoting `git push` must not be mistaken for one."""
    return [
        line
        for line in read(path).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


@pytest.mark.parametrize(("path", "commit_marker"), SCRIPTS)
def test_it_opens_a_pull_request(path, commit_marker):
    text = read(path)
    assert "gh pr create" in text, (
        f"{path.name} opens no PR; a direct write is rejected"
    )
    assert "gh pr merge --auto" in text, (
        f"{path.name} opens a PR but never lands it — without --auto the cron needs a human, "
        f"and the in-flight guard then blocks every subsequent run"
    )


@pytest.mark.parametrize(("path", "commit_marker"), SCRIPTS)
def test_nothing_is_written_to_the_default_branch(path, commit_marker):
    """The bug. Every push must name the run's branch."""
    pushes = [line for line in code_lines(path) if re.search(r"\bgit push\b", line)]
    assert pushes, f"{path.name} has no git push — the branch never reaches the remote"
    for line in pushes:
        assert '"$BRANCH"' in line, (
            f"{path.name} pushes somewhere other than the run's branch, which the repository "
            f"ruleset rejects: {line.strip()!r}"
        )


@pytest.mark.parametrize(("path", "commit_marker"), SCRIPTS)
def test_failure_paths_keep_their_error_text(path, commit_marker):
    """A failure whose reason goes to /dev/null cost a day of diagnosis."""
    for command in (commit_marker, "git push -u", "gh pr create", "gh pr merge --auto"):
        lines = [line for line in code_lines(path) if command in line]
        assert lines, f"{command!r} not found in {path.name}"
        for line in lines:
            assert "/dev/null" not in line, (
                f"{path.name} discards the error from {command!r}; route it to the log file "
                f"so the alert can say why: {line.strip()!r}"
            )


@pytest.mark.parametrize(("path", "commit_marker"), SCRIPTS)
def test_the_checkout_is_not_left_ahead_of_origin(path, commit_marker):
    """Leaving the commit on the local branch breaks gitops-deploy's --ff-only once the squash
    lands under a new SHA. That parked the deployer behind origin twice during diagnosis."""
    assert "git reset --hard HEAD~1" in read(path), (
        f"{path.name} keeps the commit locally after publishing the branch, so the next "
        f"fast-forward fails and the deployer parks"
    )


def test_secret_rotate_refuses_to_stack_an_unlanded_rotation():
    """Rotating again while a previous rotation is unpublished moves the live credential a
    second time while the first is still unrecorded. It must refuse, not skip quietly."""
    text = read(SECRET_ROTATE)
    assert "OPEN_PR" in text, (
        "no in-flight check; a second rotation could stack on the first"
    )
    guard = text.split("OPEN_PR", 1)[1]
    assert "exit 1" in guard.split("fi", 1)[0], (
        "the in-flight branch must exit non-zero — a silent skip hides that the live value "
        "and origin disagree"
    )


def test_secret_rotate_never_reverts_a_live_rotation():
    """The credential is already live and its consumer redeployed by the time this publishes.

    Reverting there would discard the only record of the value that is actually running.
    """
    lines = code_lines(SECRET_ROTATE)
    starts = [i for i, line in enumerate(lines) if "commit --no-verify -m" in line]
    assert len(starts) == 1, f"expected one commit invocation, found {len(starts)}"
    # Comments only, deliberately excluded: the header and the publish block both discuss
    # reverting in order to rule it out, and matching prose would fail on the explanation.
    after = [
        line for line in lines[starts[0] :] if re.search(r"(^|\s|;)revert\b", line)
    ]
    assert not after, (
        f"the publish path calls revert after the rotation is live, which strands the "
        f"running value: {after!r}"
    )
