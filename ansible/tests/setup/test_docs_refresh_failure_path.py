"""Guards for docs-refresh.sh's commit-failure path (initial_setup role, tag `crons`).

One failed commit in this cron can park EVERY deploy on daniel-box. `git commit || git reset`
unstages and nothing more, so the regenerated pages stay MODIFIED in the primary checkout;
deploy_git.py reads any porcelain output as dirty and gitops_deploy.py takes its healthy-skip
path, still writing `last_run`. Only a human cleaning the tree ends it.

The functions below are executed, not pattern-matched. Each one is lifted out of the RENDERED
template by name and sourced into a scratch git repository, so what these tests exercise is the
production code rather than a copy of its logic. Six behaviours, each with the half that proves
the test can go red:

- **restore_generated_tree leaves the checkout clean**, for a modified page and for a newly
  generated one — an untracked leftover parks the deployer exactly as a modified one does. The
  rejecting half runs a bare `git reset` body against the same fixture and requires it dirty.
- **dirty_tree_status distinguishes this cron's own dirt from a human's**, via a stamp outside
  the repo. Path-based attribution cannot: a human running build_docs.py by hand dirties the
  same files.
- **say_failure names the failing hook**, and still says something when no hook token matches.
- **say_suite_failure names the failing node ids**, and falls back to the tail without them.
- **keep_failure_log keeps one log per failing run**, pruned to the newest few.
- **build_docs_failure tells a generator failure from a site-build failure**, which
  build_docs.py reports as exit 2 and exit 3.
"""

import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

from _shell_render import rendered_shell_text
from lib.proc_testing import run
from lib.repo_paths import REPO

SCRIPT = rendered_shell_text("components", "docs-refresh", "docs-refresh.sh.j2")

# The functions this module sources. Named rather than globbed: a rename would otherwise leave
# every test below sourcing an empty string and passing on nothing at all.
SOURCED = (
    "say_failure",
    "suite_summary",
    "say_suite_failure",
    "keep_failure_log",
    "keep_tree_evidence",
    "restore_generated_tree",
    "mark_commit_failed",
    "clear_commit_failed",
    "dirty_tree_status",
    "build_docs_failure",
)

STAMP_DIR = "/var/lib/homelab/docs-refresh.d"
FAILURE_LOG_NAME = "failure-20261012T061700Z.log"


def _function(name: str) -> str:
    """The body of one shell function, from `name() {` to the closing brace in column one.

    Reads the RENDER, not the .j2 source (#3178) — a value that moved into a role default
    would leave a source-text match looking for `{{ ... }}` matching nothing, and a Jinja
    expression inside the body would come through substituted rather than literal, so `bash`
    never sees it. The assertion below is a sanity check that the render is in fact complete.
    """
    # One-liner form first: a `{.*?^}` pattern tried first would run past a one-line function's
    # own closing brace and swallow the next multi-line function whole.
    match = re.search(rf"^{name}\(\) \{{[^\n]*\}}$", SCRIPT, re.M) or re.search(
        rf"^{name}\(\) \{{\n.*?^\}}$", SCRIPT, re.M | re.S
    )
    assert match, (
        f"{name}() is gone from docs-refresh.sh.j2 — this module sources it by name"
    )
    body = match.group(0)
    assert "{{" not in body, (
        f"{name}() gained a Jinja expression; it can no longer be sourced"
    )
    return body


PRELUDE = "\n".join(_function(name) for name in SOURCED)


def _bash(script: str, cwd: Path, stamp_dir: Path) -> subprocess.CompletedProcess:
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
        "COMMIT_FAILED_STAMP": str(stamp_dir / "commit-failed"),
        "FAILURE_LOG": str(stamp_dir / FAILURE_LOG_NAME),
        "FAILURE_LOGS_KEPT": "5",
    }
    return run(
        ["bash", "-uo", "pipefail", "-c", f"{PRELUDE}\n{script}"], cwd=cwd, env=env
    )


def _tree(tmp_path: Path) -> Path:
    """A scratch checkout holding one committed generated page and one hand-written one."""
    repo = tmp_path / "server"
    (repo / "docs/reference").mkdir(parents=True)
    (repo / "docs/assets/generated").mkdir(parents=True)
    (repo / "docs/reference/state.md").write_text("committed state\n")
    # git tracks no empty directory, and `git status --porcelain` collapses a wholly untracked
    # one to `?? docs/assets/` — which would hide the per-file leftover these tests assert on.
    (repo / "docs/assets/generated/topology.svg").write_text("<svg/>\n")
    (repo / "docs/reference/topology.md").write_text("hand-written prose\n")
    stamp = tmp_path / "stamp"
    _bash(
        "git init -q -b master . && git add -A && "
        "git -c commit.gpgsign=false commit -qm init --no-verify",
        repo,
        stamp,
    ).check_returncode()
    return repo


def _stage_a_regeneration(repo: Path, stamp: Path) -> str:
    """Regenerate one page and add one, stage both, and return the captured name-status."""
    (repo / "docs/reference/state.md").write_text("regenerated state\n")
    (repo / "docs/assets/generated/infra-map.svg").write_text("<svg/>\n")
    run = _bash(
        "git add docs/reference/state.md docs/assets/generated && "
        "git diff --cached --name-status --no-renames",
        repo,
        stamp,
    )
    run.check_returncode()
    return run.stdout


def _porcelain(repo: Path, stamp: Path) -> str:
    return _bash("git status --porcelain", repo, stamp).stdout


def test_the_restore_leaves_no_modified_page_behind(tmp_path):
    """ACCEPT: a regenerated page goes back to its HEAD content, so the tree reads clean."""
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    staged = _stage_a_regeneration(repo, stamp)
    run = _bash(f"restore_generated_tree {shlex.quote(staged)}", repo, stamp)
    assert run.returncode == 0, run.stderr
    assert (repo / "docs/reference/state.md").read_text() == "committed state\n"
    assert " M docs/reference/state.md" not in _porcelain(repo, stamp)


def test_the_restore_leaves_no_untracked_page_behind(tmp_path):
    """A brand-new generated page has no HEAD version, so `git checkout HEAD --` fails on it.

    Left there it is UNTRACKED, and `git status --porcelain` counts untracked — which parks the
    deployer just as surely as the modified page above. This is the half a checkout-only fix
    would miss.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    staged = _stage_a_regeneration(repo, stamp)
    assert "A\tdocs/assets/generated/infra-map.svg" in staged
    _bash(f"restore_generated_tree {shlex.quote(staged)}", repo, stamp)
    assert not (repo / "docs/assets/generated/infra-map.svg").exists()
    assert "infra-map.svg" not in _porcelain(repo, stamp)


def test_the_restore_does_not_touch_a_hand_edit_it_never_staged(tmp_path):
    """topology.md is the hand-written page in docs/reference/.

    The narrowing of the ACTION is the point: a blanket `git checkout -- docs/reference` would
    clean the tree and destroy an uncommitted human edit doing it.

    The restore still REPORTS this tree as dirty, and that is the deliberate asymmetry — see the
    hook-leftover test below for why the check cannot be scoped the same way. It costs a false
    "still dirty" only for an edit made inside the run window, because the top-of-script gate
    exits on a tree that was already dirty when the run began.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    staged = _stage_a_regeneration(repo, stamp)
    (repo / "docs/reference/topology.md").write_text("hand-written prose, mid-edit\n")
    _bash(f"restore_generated_tree {shlex.quote(staged)}", repo, stamp)
    assert (
        repo / "docs/reference/topology.md"
    ).read_text() == "hand-written prose, mid-edit\n"


def test_the_restore_reports_a_leftover_a_hook_wrote_after_git_add(tmp_path):
    """ACCEPT: the case that decides the check's scope.

    prek's gen-doc-fragments hook regenerates the WHOLE fragment set, so it rewrites fragments
    this run's generators never touched. That happens after `git add`, so the file is dirty, it
    parks the deployer, and it is absent from the captured name-status by construction. A check
    scoped to the staged paths would report `tree restored` over it.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    staged = _stage_a_regeneration(repo, stamp)
    # Committed, never staged by this run, rewritten by the hook mid-commit.
    (repo / "docs/assets/generated/topology.svg").write_text("<svg>hook</svg>\n")
    assert "topology.svg" not in staged
    run = _bash(f"restore_generated_tree {shlex.quote(staged)}", repo, stamp)
    assert run.returncode != 0, (
        "a hook's leftover under docs/assets/generated read as a clean restore"
    )
    assert " M docs/assets/generated/topology.svg" in _porcelain(repo, stamp)


def test_a_reset_only_failure_path_leaves_the_tree_dirty(tmp_path):
    """REJECT: a bare `git reset` body, minimised, against the same fixture.

    Without this half a restore that silently stopped restoring would be indistinguishable from
    one that works — the fixture would simply never have been dirty.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    _stage_a_regeneration(repo, stamp)
    _bash("git reset >/dev/null 2>&1", repo, stamp)
    porcelain = _porcelain(repo, stamp)
    assert " M docs/reference/state.md" in porcelain
    assert "?? docs/assets/generated/infra-map.svg" in porcelain


def test_the_restore_reports_failure_when_the_tree_stays_dirty(tmp_path):
    """A restore that cannot finish must say so, or the run reports the wrong reason.

    Simulated by making the page read-only through its directory, which is how a
    root-owned leftover would present.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    staged = _stage_a_regeneration(repo, stamp)
    run = _bash(
        f"chmod 500 docs/assets/generated; restore_generated_tree {shlex.quote(staged)}; rc=$?; "
        "chmod 700 docs/assets/generated; exit $rc",
        repo,
        stamp,
    )
    assert run.returncode != 0


def test_the_dirty_branch_reports_down_on_this_cron_s_own_leftover_dirt(tmp_path):
    """ACCEPT: run N stamped a commit failure, so run N+1's dirt is the alarm, not a human."""
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    _bash("mark_commit_failed 'commit failed at T; tree restored'", repo, stamp)
    assert (stamp / "commit-failed").exists()
    assert _bash("dirty_tree_status", repo, stamp).stdout == "down"


def test_the_dirty_branch_still_reports_up_on_foreign_dirt(tmp_path):
    """REJECT: with no stamp the dirt is somebody else's mid-edit and the run is legitimately up.

    Without this half a `dirty_tree_status` wired to `down` unconditionally would pass the test
    above and turn every human edit on the box into a false DOWN twice a day.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    assert _bash("dirty_tree_status", repo, stamp).stdout == "up"


def test_the_stamp_is_cleared_once_a_run_publishes(tmp_path):
    """The reverse state. A stamp nothing clears makes every later dirty tree read down."""
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    _bash("mark_commit_failed 'commit failed at T'; clear_commit_failed", repo, stamp)
    assert not (stamp / "commit-failed").exists()
    assert _bash("dirty_tree_status", repo, stamp).stdout == "up"


def test_both_paths_that_end_a_healthy_run_clear_the_stamp():
    """`clear_commit_failed` on the publish path only would strand the `no change` runs.

    Most runs have nothing to publish, so a stamp cleared solely by a successful PR would keep
    reporting down long after the failure was fixed by hand.
    """
    assert SCRIPT.count("\nclear_commit_failed\n") >= 1, (
        "the publish path no longer clears it"
    )
    no_change = SCRIPT[SCRIPT.index('PUSH_MSG="no change"') :]
    assert "clear_commit_failed" in no_change[: no_change.index("exit 0")], (
        "the `no change` exit does not clear the stamp — a fixed-by-hand tree stays down"
    )


def _alert(log_text: str, tmp_path: Path, say: str = "say_failure") -> str:
    log = tmp_path / "commit.log"
    log.write_text(log_text)
    run = _bash(
        f"alert() {{ printf '%s' \"$1\"; }}; {say} 'failed' {shlex.quote(str(log))}",
        tmp_path,
        tmp_path / "stamp",
    )
    return run.stdout


def test_the_alert_names_the_failing_hook(tmp_path):
    """ACCEPT: prek keeps going past a failure and mkdocs-strict is the last hook.

    The 400-byte tail of the whole run therefore always ends `...Passed`. This log has the shape
    of the real failure: the failure is early and buried under far more than 400 bytes of later
    output.
    """
    log = (
        "Check YAML.........Passed\n"
        "gen-doc-fragments..Failed\n"
        "- hook id: gen-doc-fragments\n"
        "- files were modified by this hook\n"
    ) + "Build the docs site (mkdocs --strict)...............Passed\n" * 20
    alert = _alert(log, tmp_path)
    assert "gen-doc-fragments" in alert, alert
    assert len(alert) <= 500


def test_the_alert_falls_back_to_the_plain_tail_when_no_hook_failed(tmp_path):
    """REJECT: a filter with no fallback returns EMPTY here, which is worse than uninformative.

    `git commit` also fails for reasons carrying none of the hook tokens, and the script runs
    without `set -e`, so nothing would notice the empty message.
    """
    alert = _alert(
        "error: gpg failed to sign the data\nfatal: failed to write commit\n", tmp_path
    )
    assert "gpg failed to sign the data" in alert, alert


# Cron has no tty, so an id wider than 80 columns loses its ` - reason`. A captured-log `ERROR`
# line precedes the block; the 2026-10-12 18:20 shutdown traceback, which `Failed` matches, follows.
SUITE_LOG = (
    "____________________________ test_bad ____________________________\n"
    "------------------------- Captured log call -------------------------\n"
    "ERROR    root:witness.py:12 loki answered 503\n"
    "E       AssertionError: boom\n"
    "=========================== short test summary info ============================\n"
    "FAILED ansible/tests/setup/test_loki_route_witness.py::test_an_up_verdict_exits_zero_so_the_timer_rests\n"
    "FAILED scripts/tests/test_sample.py::test_param[a b] - AssertionError: assert 'a b' == 'c'\n"
    "ERROR scripts/tests/test_broken.py - ImportError while importing test module '/tmp/x...\n"
    "========= 2 failed, 9061 passed, 1 error in 312.45s (0:05:12) =========\n"
    "Exception ignored in: <function _remove at 0x7f>\n"
    "Traceback (most recent call last):\n"
    '    hook_error = RuntimeError("Failed to process unraisable exception")\n'
)


def test_the_suite_alert_names_the_failing_node_ids(tmp_path):
    """ACCEPT: the count line first, then each FAILED/ERROR id, and nothing outside the block."""
    alert = _alert(SUITE_LOG, tmp_path, say="say_suite_failure")
    count = "failed: 2 failed, 9061 passed, 1 error in 312.45s (0:05:12); "
    assert alert.startswith(count), alert
    assert (
        "FAILED ansible/tests/setup/test_loki_route_witness.py::"
        "test_an_up_verdict_exits_zero_so_the_timer_rests" in alert
    ), alert
    assert "FAILED scripts/tests/test_sample.py::test_param[a b];" in alert, alert
    assert alert.endswith("ERROR scripts/tests/test_broken.py"), alert
    assert "unraisable" not in alert and "loki answered 503" not in alert, alert


def test_the_suite_alert_falls_back_to_the_tail_without_a_summary(tmp_path):
    """REJECT: a `timeout` kill prints no summary; without the fallback the alert is empty."""
    log = "collected 9063 items\n........F.....\nTerminated\n"
    assert "Terminated" in _alert(log, tmp_path, say="say_suite_failure")


def test_the_suite_failure_branch_pushes_the_summary():
    """The production call site: the tests above prove a function nothing might call."""
    branch = SCRIPT[SCRIPT.index('if [ "$SUITE_RC" -ne 0 ]; then') :]
    branch = branch[: branch.index("\nfi\n")]
    assert 'say_suite_failure "docs-refresh: the test suite failed' in branch, branch
    assert "say_failure " not in branch, branch


def test_the_failure_log_is_kept_outside_the_repo(tmp_path):
    """on_exit deletes the temp log and the suite output never reaches the cron's logger, so this copy is the record.

    Outside the checkout for the same reason as the stamp: a file written inside it is untracked
    dirt, and a diagnostic that parks the deployer is not a diagnostic.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    log = tmp_path / "commit.log"
    log.write_text("- hook id: gen-doc-fragments\n")
    run = _bash(f"keep_failure_log {shlex.quote(str(log))}", repo, stamp)
    assert run.returncode == 0, run.stderr
    assert "gen-doc-fragments" in (stamp / FAILURE_LOG_NAME).read_text()
    assignment = SCRIPT[SCRIPT.index("STAMP_DIR=") :].split("\n")[0]
    assert "$REPO" not in assignment and STAMP_DIR in assignment, assignment


NAMING = next(line for line in SCRIPT.splitlines() if line.startswith("FAILURE_LOG="))


def _keep_runs(starts: list[str], tmp_path: Path) -> tuple[Path, list[str]]:
    """Run the script's FAILURE_LOG= line and keep_failure_log per start, `date` pinned to it."""
    stamp = tmp_path / "stamp"
    stamp.mkdir(exist_ok=True)
    (stamp / "commit-failed").write_text("suite failed at T\n")
    log = shlex.quote(str(tmp_path / "suite.log"))
    script = ""
    for start in starts:
        epoch = int(datetime.fromisoformat(f"{start}+00:00").timestamp())
        script += (
            f'date() {{ command date -d @{epoch} "$@"; }}; {NAMING}; printf %s {start} >{log}; '
            f'keep_failure_log {log}; echo "${{FAILURE_LOG##*/}}"\n'
        )
    return stamp, _bash(script, tmp_path, stamp).stdout.split()


def test_two_failing_runs_leave_two_logs(tmp_path):
    """ACCEPT: the 18:17 failure no longer overwrites the 06:17 one's traceback."""
    starts = ["2026-10-12T06:17:00", "2026-10-12T18:17:00"]
    stamp, names = _keep_runs(starts, tmp_path)
    assert [(stamp / name).read_text() for name in names] == starts, names


def test_the_prune_keeps_only_the_newest_logs(tmp_path):
    """REJECT a stamp that is not fixed-width (`%s`, `%-m`), which the prune's sort relies on."""
    starts = ["2001-09-09T01:46:39", "2001-09-09T01:46:40", "2026-09-30T23:59:59"]
    starts += [f"2026-10-0{day}T06:17:00" for day in range(1, 5)]
    stamp, names = _keep_runs(starts, tmp_path)
    assert sorted(names) == names, names
    assert sorted(p.name for p in stamp.glob("failure-*.log")) == names[2:]
    assert (stamp / "commit-failed").exists()


def _hook_rewrites_a_staged_page(repo: Path) -> None:
    """What prek's `files were modified by this hook` means on disk.

    The page is already staged, so a hook that rewrites it leaves the worktree ahead of the
    index — which is the difference `git diff --stat` reports and the hook's own stdout does not.
    """
    (repo / "docs/reference/state.md").write_text("rewritten by a hook\n")


def test_the_kept_log_names_the_file_a_hook_rewrote(tmp_path):
    """ACCEPT: the evidence capture names the modified path.

    The log line `files were modified by this hook` names no file, so a recurrence cannot be
    attributed without this capture.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    _stage_a_regeneration(repo, stamp)
    _hook_rewrites_a_staged_page(repo)
    run = _bash("keep_tree_evidence", repo, stamp)
    assert run.returncode == 0, run.stderr
    log = (stamp / FAILURE_LOG_NAME).read_text()
    assert "docs/reference/state.md" in log, log
    assert "git diff --stat" in log, log


def test_the_evidence_is_gone_once_the_restore_has_run(tmp_path):
    """REJECT: capturing after the restore names nothing, which is why the order is pinned.

    restore_generated_tree puts every staged path back and clears the index. Run it first and the
    same capture reports a clean tree — a log that proves only that the failure was cleaned up.
    """
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    staged = _stage_a_regeneration(repo, stamp)
    _hook_rewrites_a_staged_page(repo)
    run = _bash(
        f"restore_generated_tree {shlex.quote(staged)}; keep_tree_evidence", repo, stamp
    )
    log = (stamp / FAILURE_LOG_NAME).read_text()
    assert "docs/reference/state.md" not in log, log
    assert run.returncode == 0, run.stderr


def test_the_capture_runs_before_the_restore_on_the_commit_failure_path():
    """The production call site, not a copy of it: the two tests above only bracket the ordering."""
    branch = SCRIPT[SCRIPT.index('if [ "$COMMIT_RC" -ne 0 ]; then') :]
    branch = branch[: branch.index("\nfi\n")]
    assert branch.index("keep_tree_evidence") < branch.index(
        "restore_generated_tree"
    ), branch


def test_the_stamp_directory_is_created_and_writable_by_the_service_user():
    """The fix is inert if the stamp cannot be written, and every failure path swallows that."""
    tmpfiles = (
        REPO / "hosts/components/docs-refresh/tmpfiles.d/homelab-docs-site.conf.j2"
    ).read_text()
    line = next(
        (ln for ln in tmpfiles.splitlines() if STAMP_DIR in ln and ln.startswith("d ")),
        None,
    )
    assert line, f"no tmpfiles d line creates {STAMP_DIR}"
    assert re.search(r"0755 \{\{ sys_user \}\} \{\{ sys_user \}\} -$", line), (
        "the docs-refresh service runs as sys_user — a root-owned stamp directory "
        "makes every mark_commit_failed a silent no-op"
    )
    assert STAMP_DIR in SCRIPT, (
        "the script and the tmpfiles file no longer agree on the stamp path"
    )


def test_build_docs_failure_names_which_half_failed(tmp_path):
    def message(rc: int) -> str:
        return _bash(f"build_docs_failure {rc}", tmp_path, tmp_path / "stamp").stdout

    generators, site, crash = message(2), message(3), message(1)
    assert "generators failed" in generators and "site build failed" not in generators
    assert "site build failed" in site and "generators failed" not in site
    assert "crashed (exit 1)" in crash
