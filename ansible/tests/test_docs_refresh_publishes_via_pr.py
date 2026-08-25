"""docs-refresh must publish through a PR, and must never discard the reason it failed.

Both properties were learned the hard way on 2026-08-25. The script wrote straight to
master, which a repository ruleset rejects outright ("Required status check ... is
expected"), so the cron had never once published. And both its `git commit` and `git push`
sent output to /dev/null, so the alert said the push failed and nothing said why -- the
cause stayed invisible until the rejected command was run by hand.

These are text assertions over the template, which is the weaker kind of guard: an
indirection through a variable or a helper would slip past. They are still worth having,
because the regression they cover is someone reinstating a direct push to master, and that
is a literal line of shell.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE = REPO / "ansible/roles/setup/initial_setup/templates/docs-refresh.sh.j2"


def template_text() -> str:
    text = TEMPLATE.read_text()
    assert len(text) > 2000, f"template is only {len(text)} bytes — has it moved?"
    return text


def _code_lines() -> list[str]:
    """Shell lines only. The header documents the old direct push at length, and a comment
    quoting `git push` must not be mistaken for one."""
    return [
        line
        for line in template_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_it_opens_a_pull_request():
    text = template_text()
    assert "gh pr create" in text, (
        "no PR is opened; a direct write to master is rejected"
    )
    assert "gh pr merge --auto" in text, (
        "the PR is opened but never lands — without --auto the cron needs a human, and the "
        "open-PR guard then skips every subsequent run"
    )


def test_nothing_is_pushed_to_master():
    """The bug. Every push must name the run's branch."""
    pushes = [line for line in _code_lines() if re.search(r"\bgit push\b", line)]
    assert pushes, "no git push at all — the branch never reaches the remote"
    for line in pushes:
        assert '"$BRANCH"' in line, (
            f"push does not target the run's branch, so it targets master, which a "
            f"repository ruleset rejects: {line.strip()!r}"
        )


def test_failure_paths_keep_their_error_text():
    """A failure whose reason goes to /dev/null cost a day of diagnosis."""
    for command in (
        "git commit -m",
        "git push -u",
        "gh pr create",
        "gh pr merge --auto",
    ):
        lines = [line for line in _code_lines() if command in line]
        assert lines, f"{command!r} not found in the template"
        for line in lines:
            assert "/dev/null" not in line, (
                f"{command!r} discards its error; route it to the log file instead so the "
                f"alert can say why: {line.strip()!r}"
            )


def test_master_is_not_left_ahead_of_origin():
    """Leaving the commit on master breaks gitops-deploy's --ff-only once the squash lands.

    That is what parked the deployer behind origin twice while this was being diagnosed.
    """
    text = template_text()
    assert "git reset --hard HEAD~1" in text, (
        "master keeps the commit after the branch is published, so the next fast-forward "
        "fails and the deployer parks"
    )
