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
ROTATION_AUDIT = TEMPLATES / "secret-rotation-audit.sh.j2"

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
    second time while the first is still unrecorded. It must refuse, not skip quietly.

    The gate is the REMOTE BRANCH, not an open PR. `gh pr create` runs after `git push`, so a
    create failure leaves the branch on origin with no PR at all — and the publish block's
    `git reset --hard HEAD~1` then erases every local trace. An open-PR check passes cleanly
    in exactly the state that most needs to refuse (2026-08-25 review H-1).
    """
    text = read(SECRET_ROTATE)
    assert "git ls-remote --heads origin" in text, (
        "no remote-branch check; a rotation whose `gh pr create` failed leaves no local "
        "evidence, so a local-only guard cannot see it and the next run stacks on it"
    )
    guard = text.split("git ls-remote --heads origin", 1)[1]
    assert "exit 1" in guard, (
        "the in-flight branch must exit non-zero — a silent skip hides that the live value "
        "and origin disagree"
    )


def test_secret_rotate_fails_closed_when_origin_is_unreachable():
    """`git ls-remote ... || true` would read an unreachable origin as "no stale branch" and
    rotate straight into the state the guard exists to refuse. The exit status must be tested.
    """
    text = read(SECRET_ROTATE)
    line = next(
        line for line in code_lines(SECRET_ROTATE) if "git ls-remote --heads" in line
    )
    assert "|| true" not in line, (
        f"the remote-branch check swallows its exit status, so an unreachable origin fails "
        f"OPEN rather than closed: {line!r}"
    )
    assert line.lstrip().startswith("if !"), (
        f"the remote-branch check must branch on its exit status: {line!r}"
    )
    assert "cannot reach origin" in text, (
        "no distinct alert for an unreachable origin; it would be indistinguishable from a "
        "clean run"
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


def test_the_audit_watches_for_an_unlanded_rotation_branch():
    """The weekly gate refuses to stack, but nothing would REPORT the stuck state between
    Sundays. The daily audit is the sticky signal, and its two existing arms read only local
    state -- a clean tree and an unrotated registry are exactly what the failure looks like.
    """
    text = ROTATION_AUDIT.read_text()
    assert "git ls-remote --heads origin" in text, (
        "the daily audit cannot see an unlanded rotation branch, so a failed `gh pr create` "
        "goes unreported until the next Sunday's gate happens to trip"
    )


def test_the_rotation_crons_push_through_the_shared_library():
    """crons.yml:15-19 says every health cron sources kuma-push-lib.sh "including
    secret-rotate.sh.j2 further down". That was false -- `grep -c kuma-push-lib` returned 0
    for it, and both rotation scripts passed the token-bearing URL to curl as an argv
    element, readable through /proc/<pid>/cmdline (2026-08-25 review M-9).

    The comment is documentation of an invariant, so the invariant gets a test rather than
    the comment getting a correction.
    """
    for path in (SECRET_ROTATE, ROTATION_AUDIT):
        text = path.read_text()
        assert "kuma-push-lib.sh" in text, (
            f"{path.name} does not source the shared push library, contradicting "
            f"crons.yml:15-19"
        )
        pushes = [
            line
            for line in text.splitlines()
            if "curl" in line
            and "api/push" in line
            and not line.strip().startswith("#")
        ]
        assert not pushes, (
            f"{path.name} still passes a token-bearing push URL to curl directly, which "
            f"exposes it in /proc/<pid>/cmdline: {pushes}"
        )


def test_the_audit_watches_the_gh_token_both_crons_depend_on():
    """Both publishing crons authenticate with one `gh` OAuth token that is not in
    secrets.yml and not in the rotation registry, so nothing watched it. Revoked, they both
    keep running, both fail at `gh pr create`, and both stop publishing silently
    (2026-08-25 review M-6).
    """
    text = ROTATION_AUDIT.read_text()
    assert "gh auth status" in text, (
        "nothing checks the credential secret-rotate and docs-refresh publish with"
    )
    line = next(
        line
        for line in text.splitlines()
        if "gh auth status" in line and not line.strip().startswith("#")
    )
    assert "if !" in line, (
        f"the gh check must branch on its exit status, not on its output: {line!r}"
    )


def test_the_audit_grace_is_shorter_than_the_gap_to_the_first_audit():
    """The grace period must not swallow the first audit after a failed publish.

    rotate is Sunday 09:00 and the audit is daily 08:00 (crons.yml), so that first audit is
    23h later. A 24h grace let it push UP -- green on the operator's Monday morning, with the
    sticky DOWN not appearing until Tuesday. That is M-1 narrowed to a day, not closed.
    """
    crons = (TEMPLATES.parent / "tasks/crons.yml").read_text()
    assert 'hour: "8"' in crons and 'hour: "9"' in crons, (
        "the cron schedule moved; re-derive the grace period against the new times"
    )
    text = ROTATION_AUDIT.read_text()
    seconds = [int(m) for m in re.findall(r"-gt (\d{4,})", text)]
    assert seconds, "no age threshold found in the stray-branch arm"
    assert max(seconds) < 23 * 3600, (
        "the grace period is at least as long as the 23h gap between a Sunday 09:00 rotate "
        "and the next 08:00 audit, so the first audit after a failed publish reports UP: %s"
        % seconds
    )


def test_the_audit_branch_arm_is_additive_not_a_short_circuit():
    """The two arms above it exit 0 before the auditor runs, which is right for a registry
    that cannot be trusted. A stray branch says nothing about the OTHER secrets, so
    short-circuiting there would silence every overdue secret until a human cleared it.
    """
    text = ROTATION_AUDIT.read_text()
    arm = text.split("git ls-remote --heads origin", 1)[1]
    # Up to the auditor invocation: the arm must reach it, not exit ahead of it.
    before_audit = arm.split("secret_rotation.py audit", 1)[0]
    assert "exit 0" not in before_audit, (
        "the stray-branch arm short-circuits past the auditor, suppressing overdue "
        "reporting for every other secret while the branch sits there"
    )
    assert "--extra-down" in arm, (
        "the arm does not force the monitor DOWN; the auditor would push UP and mask it"
    )


def test_the_two_audit_arms_accumulate_rather_than_suppress_each_other():
    """A revoked `gh` token is what makes `gh pr create` fail and strand the branch, so the
    two faults are causally linked and the compound case is the one H-1 exists for. Gating the
    branch arm on the token arm reports the cause and hides the consequence -- a live rotated
    credential, unpublished on origin.

    `git ls-remote` authenticates over git's own credential path, not through `gh`, so the
    branch arm still works with a dead token and has no reason to be skipped.
    """
    text = ROTATION_AUDIT.read_text()
    lines = [line for line in code_lines(ROTATION_AUDIT) if "EXTRA_DOWN" in line]

    # The branch arm's condition must not read EXTRA_DOWN -- that is the short-circuit.
    branch_arm = text.split("# Runs even when the arm above fired", 1)
    assert len(branch_arm) == 2, "the stray-branch arm lost its ordering comment"
    # "; then", not "then": the prose above the arm contains "au-then-ticates", and splitting
    # on the bare word truncated ahead of the condition -- the guard passed on the mutation it
    # exists to catch.
    condition = branch_arm[1].split("; then", 1)[0]
    assert "EXTRA_DOWN" not in condition, (
        "the stray-branch arm is gated on the gh-token arm, so a revoked token silences the "
        f"unpublished-credential report it causes: {condition!r}"
    )

    # Both arms must go through the accumulator, and the accumulator must concatenate.
    assert text.count("add_down ") >= 2, (
        "an arm assigns EXTRA_DOWN directly instead of accumulating, so a second fault "
        "overwrites the first"
    )
    concat = [line for line in lines if 'EXTRA_DOWN="$EXTRA_DOWN' in line]
    assert concat, (
        "add_down replaces rather than appends; the compound fault reports one reason: "
        f"{lines!r}"
    )
