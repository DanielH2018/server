"""Guards for docs-refresh.sh's own tree and its failure path.

The functions below are executed, not pattern-matched. Each one is lifted out of the RENDERED
template by name and sourced into a scratch git repository, so what these tests exercise is the
production code rather than a copy of its logic. Each behaviour has the half that proves the
test can go red:

- **prepare_tree leaves no ignored file behind**, so a role that master deleted but whose
  `__pycache__` survived is gone from the tree the generators and the suite read. The rejecting
  half runs the same body without `clean -x` and requires the leftover role directory.
- **stray_writes names a write outside the staged paths**, and is silent when every change is
  staged. The clean tree would otherwise drop that write with nothing said.
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

# Named, not globbed: a rename would otherwise leave every test sourcing an empty string and passing
# on nothing.
SOURCED = (
    "say_failure",
    "suite_summary",
    "say_suite_failure",
    "keep_failure_log",
    "keep_tree_evidence",
    "prepare_tree",
    "stray_writes",
    "build_docs_failure",
)

STAMP_DIR = "/var/lib/homelab/docs-refresh.d"
FAILURE_LOG_NAME = "failure-20261012T061700Z.log"


def _function(name: str) -> str:
    """The body of one shell function, from `name() {` to the closing brace in column one.

    Reads the RENDER, not the .j2 source — a value that moved into a role default
    would leave a source-text match looking for `{{ ... }}` matching nothing, and a Jinja
    expression inside the body would come through substituted rather than literal, so `bash`
    never sees it. The assertion below is a sanity check that the render is in fact complete.
    """
    # One-liner form first: a `{.*?^}` pattern tried first would swallow the next multi-line
    # function whole.
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
    # `git status --porcelain` collapses a wholly untracked directory to `?? docs/assets/`, hiding
    # the per-file leftover asserted on.
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


def test_stray_writes_with_every_change_staged_is_clean(tmp_path):
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    _stage_a_regeneration(repo, stamp)
    run = _bash("stray_writes", repo, stamp)
    assert run.stdout == "", run.stdout


def test_stray_writes_names_an_unstaged_and_an_untracked_write_is_flagged(tmp_path):
    repo, stamp = _tree(tmp_path), tmp_path / "stamp"
    _stage_a_regeneration(repo, stamp)
    (repo / "docs/reference/topology.md").write_text("a generator rewrote this\n")
    (repo / "k8s-glance.md").write_text("written outside the staged paths\n")
    run = _bash("stray_writes", repo, stamp)
    assert "docs/reference/topology.md" in run.stdout, run.stdout
    assert "k8s-glance.md" in run.stdout, run.stdout
    assert "state.md" not in run.stdout, run.stdout


def test_the_stray_check_degrades_the_run():
    """The production call site: the pair above proves a function nothing might call."""
    gate = SCRIPT[SCRIPT.index('STRAY="$(stray_writes)"') :]
    gate = gate[: gate.index("\nfi\n")]
    assert "GENERATORS_OK=0" in gate, gate


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


# Cron has no tty, so an id wider than 80 columns loses its ` - reason`.
SUITE_LOG = (
    "____________________________ test_bad ____________________________\n"
    "------------------------- Captured log call -------------------------\n"
    "ERROR    root:witness.py:12 loki answered 503\n"
    "E       AssertionError: boom\n"
    "=========================== short test summary info ============================\n"
    "FAILED hosts/tests/test_loki_route_witness.py::test_an_up_verdict_exits_zero_so_the_timer_rests\n"
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
        "FAILED hosts/tests/test_loki_route_witness.py::"
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

    Outside the tree, because the next run's prepare_tree cleans every untracked file in it.
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
    (stamp / "tree").mkdir()
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
    assert (stamp / "tree").is_dir()


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


def test_the_stamp_directory_is_created_and_writable_by_the_service_user():
    """The tree and the failure logs live here, and every failure path swallows a write error."""
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
        "leaves it no tree to work in and no failure log"
    )
    assert STAMP_DIR in SCRIPT, (
        "the script and the tmpfiles file no longer agree on the stamp path"
    )
    assert f'TREE="{STAMP_DIR}/tree"' in SCRIPT, (
        "the tree moved out of the stamp directory"
    )


def _primary_with_a_role_master_deletes(tmp_path: Path) -> tuple[Path, Path, str]:
    """A primary checkout whose master deletes role `retired`, and the cron's tree before that.

    Returns the primary, the tree prepared at the commit before the deletion, and the SHA after
    it. The tree holds what a run leaves in it: `retired`'s `__pycache__` and a `.venv`.
    """
    repo, stamp = tmp_path / "server", tmp_path / "stamp"
    for role in ("kept", "retired"):
        (repo / "k8s/roles" / role / "defaults").mkdir(parents=True)
        (repo / "k8s/roles" / role / "defaults/main.yml").write_text(f"{role}: 1\n")
    (repo / ".gitignore").write_text("__pycache__/\n.venv/\n")
    commit = "git -c commit.gpgsign=false commit -q --no-verify -m"
    before = _bash(
        f"git init -q -b master . && git add -A && {commit} init && git rev-parse HEAD",
        repo,
        stamp,
    )
    before.check_returncode()
    tree = stamp / "tree"
    _bash(
        f"REPO={repo}; TREE={tree}; prepare_tree {before.stdout.strip()}", repo, stamp
    ).check_returncode()
    (tree / "k8s/roles/retired/tests/__pycache__").mkdir(parents=True)
    (tree / "k8s/roles/retired/tests/__pycache__/test_retired.pyc").write_bytes(b"")
    (tree / ".venv").mkdir()
    (tree / ".venv/pyvenv.cfg").write_text("home = /usr/bin\n")
    after = _bash(
        f"git rm -rq k8s/roles/retired && {commit} drop && git rev-parse HEAD",
        repo,
        stamp,
    )
    after.check_returncode()
    return repo, tree, after.stdout.strip()


def _head(tree: Path, stamp: Path) -> str:
    return _bash(f"git -C {tree} rev-parse HEAD", tree, stamp).stdout.strip()


def test_prepare_tree_drops_a_deleted_roles_ignored_leftovers_is_clean(tmp_path):
    """ACCEPT: the role master deleted is gone from the tree, and the virtualenv stays."""
    repo, tree, after = _primary_with_a_role_master_deletes(tmp_path)
    stamp = tmp_path / "stamp"
    run = _bash(f"REPO={repo}; TREE={tree}; prepare_tree {after}", repo, stamp)
    assert run.returncode == 0, run.stderr
    assert not (tree / "k8s/roles/retired").exists()
    assert (tree / "k8s/roles/kept/defaults/main.yml").exists()
    assert (tree / ".venv/pyvenv.cfg").exists()
    assert _head(tree, stamp) == after


def test_prepare_tree_without_clean_x_keeps_the_deleted_role_is_flagged(tmp_path):
    """REJECT: without -x the leftover `__pycache__` keeps `retired` on disk, as on daniel-box."""
    repo, tree, after = _primary_with_a_role_master_deletes(tmp_path)
    no_x = _function("prepare_tree").replace("clean -ffdxq", "clean -ffdq")
    assert "clean -ffdq" in no_x, "prepare_tree no longer runs `clean -ffdxq`"
    run = _bash(
        f"{no_x}\nREPO={repo}; TREE={tree}; prepare_tree {after}",
        repo,
        tmp_path / "stamp",
    )
    assert run.returncode == 0, run.stderr
    assert (tree / "k8s/roles/retired").is_dir()


def test_prepare_tree_replaces_a_tree_git_no_longer_recognises(tmp_path):
    """A tree whose `.git` link is gone is added again, rather than failing every later run."""
    repo, tree, after = _primary_with_a_role_master_deletes(tmp_path)
    stamp = tmp_path / "stamp"
    (tree / ".git").unlink()
    run = _bash(f"REPO={repo}; TREE={tree}; prepare_tree {after}", repo, stamp)
    assert run.returncode == 0, run.stderr
    assert not (tree / "k8s/roles/retired").exists()
    assert _head(tree, stamp) == after


def test_build_docs_failure_names_which_half_failed(tmp_path):
    def message(rc: int) -> str:
        return _bash(f"build_docs_failure {rc}", tmp_path, tmp_path / "stamp").stdout

    generators, site, crash = message(2), message(3), message(1)
    assert "generators failed" in generators and "site build failed" not in generators
    assert "site build failed" in site and "generators failed" not in site
    assert "crashed (exit 1)" in crash
