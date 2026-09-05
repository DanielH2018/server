"""Guards for eval-run.sh's commit-failure path (initial_setup role, tag `crons`).

This cron carried the body that parked every deploy on daniel-box: `git commit || git reset`
unstages and nothing more, so evals/history.json stayed MODIFIED in the primary checkout,
deploy_git.py read the porcelain output as dirty, and gitops_deploy.py took its healthy-skip
path while still writing `last_run`. docs-refresh.sh had the identical body and did exactly
that on 2026-09-04 and again on 2026-09-05, 25 consecutive skipped ticks the second time
(#1155). This is the same fix one script over, with one difference the tests below pin: the
staged file here is a week of paid API calls, not generator output, so it is copied outside the
checkout before the tree is put back.

The functions are executed, not pattern-matched. Each is lifted out of the template by name and
sourced into a scratch git repository, so what runs is the production code rather than a copy of
its logic. Every behaviour carries the half that proves the test can go red.
"""

import re
import subprocess
from pathlib import Path

from _helpers import ANSIBLE

SCRIPT_PATH = ANSIBLE / "roles/setup/initial_setup/templates/eval-run.sh.j2"
SCRIPT = SCRIPT_PATH.read_text()
DOCS_REFRESH = (
    ANSIBLE / "roles/setup/initial_setup/templates/docs-refresh.sh.j2"
).read_text()
CRONS = (ANSIBLE / "roles/setup/initial_setup/tasks/crons.yml").read_text()

# The functions this module sources. Named rather than globbed: a rename would otherwise leave
# every test below sourcing an empty string and passing on nothing at all.
SOURCED = ("keep_failure_log", "restore_history")

# The pre-fix body, kept as a fixture so the tests that accept the real one can be shown to
# reject something. Without it every assertion below would pass on a `git reset` that never
# cleaned anything, which is the state this file exists to make impossible.
OLD_BODY = "restore_history() { git reset >/dev/null 2>&1; }"


def _function(name: str) -> str:
    """The body of one shell function, from `name() {` to the closing brace in column one."""
    # One-liner form first: a `{.*?^}` pattern tried first would run past a one-line function's
    # own closing brace and swallow the next multi-line function whole.
    match = re.search(rf"^{name}\(\) \{{[^\n]*\}}$", SCRIPT, re.M) or re.search(
        rf"^{name}\(\) \{{\n.*?^\}}$", SCRIPT, re.M | re.S
    )
    assert match, (
        f"{name}() is gone from eval-run.sh.j2 — this module sources it by name"
    )
    body = match.group(0)
    assert "{{" not in body, (
        f"{name}() gained a Jinja expression; it can no longer be sourced"
    )
    return body


PRELUDE = "\n".join(_function(name) for name in SOURCED)


def _bash(
    script: str, cwd: Path, stamp_dir: Path, prelude: str = PRELUDE
) -> subprocess.CompletedProcess:
    """Run `script` with the production functions in scope.

    GIT_* is stripped from the environment, not merely overridden: `git commit` exports GIT_DIR
    and GIT_INDEX_FILE to its hooks, so a pytest run under prek inherits them and every git
    command here would write the REAL repository whatever `cwd` says.
    """
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(cwd),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "STAMP_DIR": str(stamp_dir),
        "COMMIT_FAILED_LOG": str(stamp_dir / "last-commit-failure.log"),
        "UNPUBLISHED_HISTORY": str(stamp_dir / "unpublished-history.json"),
    }
    return subprocess.run(
        ["bash", "-uo", "pipefail", "-c", f"{prelude}\n{script}"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def _tree(tmp_path: Path) -> Path:
    """A scratch checkout holding a committed evals/history.json and one other tracked file."""
    repo = tmp_path / "server"
    (repo / "evals").mkdir(parents=True)
    (repo / "evals/history.json").write_text('{"runs": []}\n')
    (repo / "evals/README.md").write_text("prose\n")
    _bash(
        "git init -q -b master . && git add -A && "
        "git -c commit.gpgsign=false commit -qm init --no-verify",
        repo,
        tmp_path / "stamp",
    ).check_returncode()
    return repo


def _stage_a_sweep(repo: Path, stamp: Path) -> None:
    """Append this week's result and stage it, the way the cron does before committing."""
    (repo / "evals/history.json").write_text('{"runs": [{"score": 0.9}]}\n')
    _bash("git add evals/history.json", repo, stamp).check_returncode()


def _porcelain(repo: Path, stamp: Path) -> str:
    return _bash("git status --porcelain", repo, stamp).stdout


def test_the_restore_leaves_no_modified_history_behind(tmp_path):
    """ACCEPT: history.json goes back to its HEAD content, so the tree reads clean."""
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    _stage_a_sweep(repo, stamp)
    run = _bash("restore_history", repo, stamp)
    assert run.returncode == 0, run.stderr
    assert (repo / "evals/history.json").read_text() == '{"runs": []}\n'
    assert _porcelain(repo, stamp) == ""


def test_the_old_reset_only_body_leaves_the_tree_dirty(tmp_path):
    """REJECT: the pre-fix body against the same fixture, which is what parked the deployer.

    `git reset` unstages and nothing else, so the file stays modified and `git status
    --porcelain` still prints it — the one thing gitops_deploy.py reads to decide it must skip.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    _stage_a_sweep(repo, stamp)
    _bash("restore_history", repo, stamp, prelude=OLD_BODY)
    assert " M evals/history.json" in _porcelain(repo, stamp), (
        "the pre-fix body cleaned the tree, so these tests prove nothing"
    )


def test_the_restore_keeps_the_sweep_outside_the_checkout(tmp_path):
    """ACCEPT: the paid result survives the restore, at a path the alert names.

    This is where eval-run diverges from docs-refresh. There the staged files are generator
    output the next run reproduces; here a discarded history.json is a week of API spend that
    no rerun brings back.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    _stage_a_sweep(repo, stamp)
    run = _bash('restore_history; printf %s "$KEPT_NOTE"', repo, stamp)
    assert (
        stamp / "unpublished-history.json"
    ).read_text() == '{"runs": [{"score": 0.9}]}\n'
    assert run.stdout == f"sweep kept at {stamp / 'unpublished-history.json'}"


def test_a_copy_that_failed_is_reported_as_a_lost_sweep(tmp_path):
    """REJECT: the note must not claim a copy that never happened.

    `cp` is silenced, so a $STAMP_DIR that cannot be created — initial_setup not yet run since
    this landed, a full disk, wrong ownership — would otherwise leave the alert asserting
    `sweep kept at <path>` with nothing at that path. An alert reporting an outcome it did not
    confirm is the failure mode the rest of this script exists to keep out.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    _stage_a_sweep(repo, stamp)
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the stamp directory should be\n")
    run = _bash(
        f"STAMP_DIR={blocked}; UNPUBLISHED_HISTORY={blocked}/unpublished-history.json; "
        'restore_history; printf %s "$KEPT_NOTE"',
        repo,
        stamp,
    )
    assert run.stdout.startswith("SWEEP LOST"), run.stdout
    # Losing the copy must not also park the deployer: the two outcomes are independent.
    assert _porcelain(repo, stamp) == ""


def test_the_old_reset_only_body_kept_no_copy_of_the_sweep(tmp_path):
    """REJECT: nothing is written outside the checkout, so the assertion above can fail."""
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    _stage_a_sweep(repo, stamp)
    _bash("restore_history", repo, stamp, prelude=OLD_BODY)
    assert not (stamp / "unpublished-history.json").exists()


def test_the_restore_leaves_no_untracked_history_behind(tmp_path):
    """A history.json with no HEAD version needs `rm`, not `git checkout HEAD --`.

    The checkout fails on it and leaves the file UNTRACKED, and `git status --porcelain` counts
    untracked — so a checkout-only fix parks the deployer just as surely as the old body did.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    _bash(
        "git rm -q evals/history.json && "
        "git -c commit.gpgsign=false commit -qm drop --no-verify",
        repo,
        stamp,
    ).check_returncode()
    (repo / "evals/history.json").write_text('{"runs": [{"score": 0.9}]}\n')
    _bash("git add evals/history.json", repo, stamp).check_returncode()
    run = _bash("restore_history", repo, stamp)
    assert run.returncode == 0, run.stderr
    assert not (repo / "evals/history.json").exists()
    assert _porcelain(repo, stamp) == ""


def test_the_restore_reports_a_leftover_a_hook_wrote_after_git_add(tmp_path):
    """REJECT-side of the cleanliness check: a file this run never staged still parks deploys.

    prek's fixing hooks rewrite files after `git add` runs, and such a file is absent from the
    staged path by construction. A check scoped to evals/history.json would report `tree
    restored` over exactly the leftover that keeps the deployer skipping.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    _stage_a_sweep(repo, stamp)
    (repo / "evals/README.md").write_text("prose, rewritten by a hook\n")
    run = _bash("restore_history", repo, stamp)
    assert run.returncode != 0, (
        "a leftover outside evals/history.json read as a clean restore"
    )


def test_neither_cron_reads_the_exit_code_through_a_negation():
    """`if ! git commit` makes `$?` the status of the negation, which is always 0.

    Both scripts report git's exit code now, because "hook rejection?" was a guess: #1155 was
    filed with the cause undiagnosed precisely because the alert named none.
    """
    for name, text in (("eval-run", SCRIPT), ("docs-refresh", DOCS_REFRESH)):
        # Anchored to a statement, not a substring: both scripts name the rejected form in the
        # comment above the code that replaced it, and a bare `in` check would match that.
        assert not re.search(r"^\s*if ! git commit", text, re.M), (
            f"{name}.sh.j2 reads git commit's status through a negation; $? is 0 there"
        )
        assert "COMMIT_RC=$?" in text, (
            f"{name}.sh.j2 no longer captures git commit's exit code"
        )
        assert "(git exit ${COMMIT_RC})" in text, (
            f"{name}.sh.j2 captures the exit code but does not report it"
        )


def test_the_state_directory_is_created_outside_the_checkout():
    """The stamp directory is a cron-owned path under /var/lib/homelab, never inside $REPO.

    A file recording a failure that itself parks the deployer would be the bug wearing the fix's
    clothes.
    """
    assert "/var/lib/homelab/eval-run.d" in CRONS, (
        "crons.yml no longer creates eval-run's state directory; the cron runs without become "
        "and cannot create it itself"
    )
    assert 'STAMP_DIR="/var/lib/homelab/eval-run.d"' in SCRIPT
