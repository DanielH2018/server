#!/usr/bin/env python3
"""`scripts/deploy.sh --at <sha>` renders a snapshot of <sha> rather than of HEAD.

A landing used to wait up to 540s for the GitOps tick to fast-forward the primary checkout
onto its PR's merge commit, because deploy.sh rendered whatever tree it ran in. `--at` moves
the commit into the wrapper's own argument list: the snapshot is cut from <sha>, and the two
gates that would otherwise answer about the wrong commit -- the staleness check and the tag
validation -- are scoped to it as well.

Both halves everywhere, per CLAUDE.md. The default path must keep rendering HEAD (a test that
only asserts `--at` reached the right commit passes for an implementation that snapshots the
named SHA always), and each forwarded flag is asserted present under `--at` and ABSENT
without it, so a flag that stopped being forwarded cannot read as a pass.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_at_sha.py
"""

import subprocess
import time
from pathlib import Path

from _deploy_sh_fakes import (
    FLOCK_STUB,
    deploy_sh_env,
    git_free_env,
    make_snapshot_repo,
)

_REPO = Path(__file__).resolve().parents[3]
_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"

_BAD_FLAGS_EXIT = 64

# Records every helper call, and -- for the playbook run -- the commit the tree it was run
# from is at. That is the whole question `--at` answers, and it is readable only from inside
# the snapshot, which is deleted before the wrapper returns.
_UV_STUB = """#!/bin/bash
echo "$*" >> "$DEPLOY_SH_CALLS"
case "$*" in
  *ansible-playbook*) git rev-parse HEAD > "$DEPLOY_TEST_SHA_FILE"; exit 0 ;;
  *deploy_tags.py\\ list*) printf 'alpha\\nbeta\\n'; exit 0 ;;
  *deploy_detach_notify.py*)
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == "--cwd" ]]; then
        git -C "$2" rev-parse HEAD > "$DEPLOY_TEST_NOTIFY_FILE"
        break
      fi
      shift
    done
    exit 0 ;;
  *) exit 0 ;;
esac
"""

# `logger` is absent from most test environments, and deploy.sh swallows that with `|| true`,
# so the annotation is unobservable without a stub. Prefixed, because it shares the call log.
_LOGGER_STUB = """#!/bin/bash
echo "logger $*" >> "$DEPLOY_SH_CALLS"
"""


def _annotated_sha(calls: list[str]) -> str:
    """The `sha=` field of the deploy annotation, which is a SHORT sha of unfixed width."""
    line = next(c for c in calls if c.startswith("logger "))
    return next(f for f in line.split() if f.startswith("sha=")).removeprefix("sha=")


def _second_commit(repo: Path) -> tuple[str, str]:
    """Add one commit to `repo`; return (first sha, second sha), both full."""
    env = git_free_env()

    def sha() -> str:
        return subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    first = sha()
    (repo / "ansible" / "later.yml").write_text("---\n[]\n")
    for args in (
        ("git", "add", "-A"),
        ("git", "commit", "-q", "-m", "later", "--no-gpg-sign"),
    ):
        subprocess.run(args, cwd=repo, env=env, check=True, capture_output=True)
    return first, sha()


def _run(
    tmp_path: Path, repo: Path, *argv: str
) -> tuple[subprocess.CompletedProcess, list[str], str]:
    """Run the real deploy.sh in a throwaway repo; return (result, helper calls, deployed sha).

    Only `uv` and `flock` are stubbed: the argument parsing, the SHA resolution and the
    `git worktree add` that makes the snapshot are all the real script, run against a real
    two-commit repository.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "flock").write_text(FLOCK_STUB)
    (bin_dir / "uv").write_text(_UV_STUB)
    (bin_dir / "logger").write_text(_LOGGER_STUB)
    for stub in ("flock", "uv", "logger"):
        (bin_dir / stub).chmod(0o755)

    calls = tmp_path / "calls.log"
    calls.write_text("")
    deployed = tmp_path / "deployed-sha"
    env = deploy_sh_env(
        tmp_path,
        bin_dir,
        DEPLOY_SH_CALLS=str(calls),
        DEPLOY_TEST_SHA_FILE=str(deployed),
        DEPLOY_TEST_NOTIFY_FILE=str(tmp_path / "notify-sha"),
    )
    result = subprocess.run(
        [str(_DEPLOY_SH), *argv],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return (
        result,
        [ln for ln in calls.read_text().splitlines() if ln.strip()],
        deployed.read_text().strip() if deployed.exists() else "",
    )


def _prepared(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo with two commits, ready for `_run`; returns (repo, first, second)."""
    repo = make_snapshot_repo(tmp_path / "repo")
    first, second = _second_commit(repo)
    return repo, first, second


def test_at_snapshots_the_named_commit_and_not_head(tmp_path):
    """RED half: the playbook must run from a tree at <sha>, which is NOT this checkout's HEAD."""
    repo, first, second = _prepared(tmp_path)
    result, _calls, deployed = _run(
        tmp_path, repo, "--skip-tag-check", "--skip-staleness-check", "--at", first
    )
    assert result.returncode == 0, result.stderr
    assert deployed == first, result.stderr
    assert deployed != second


def test_without_at_the_snapshot_is_still_head(tmp_path):
    """CLEAN half: `--at` must not become the only thing the snapshot ever reads."""
    repo, _first, second = _prepared(tmp_path)
    result, _calls, deployed = _run(
        tmp_path, repo, "--skip-tag-check", "--skip-staleness-check"
    )
    assert result.returncode == 0, result.stderr
    assert deployed == second


def test_a_short_sha_and_a_ref_both_resolve(tmp_path):
    """Any committish the object store resolves: a landing passes a full SHA, a hand a ref."""
    repo, first, _second = _prepared(tmp_path)
    _result, _calls, deployed = _run(
        tmp_path, repo, "--skip-tag-check", "--skip-staleness-check", "--at", first[:8]
    )
    assert deployed == first


def test_an_unresolvable_at_refuses_before_anything_runs(tmp_path):
    """64, the code --detach with --check already uses: a bad argument, not a tag miss (2)."""
    repo, _first, _second = _prepared(tmp_path)
    result, calls, deployed = _run(
        tmp_path, repo, "--tags", "x", "--at", "deadbeefdeadbeef"
    )
    assert result.returncode == _BAD_FLAGS_EXIT, result.stdout + result.stderr
    assert "deadbeefdeadbeef" in result.stderr
    assert calls == [] and deployed == ""


def test_at_with_changed_is_refused(tmp_path):
    """--changed derives from the working tree's diff; --at deploys another commit entirely."""
    repo, first, _second = _prepared(tmp_path)
    result, calls, deployed = _run(tmp_path, repo, "--changed", "--at", first)
    assert result.returncode == _BAD_FLAGS_EXIT, result.stdout + result.stderr
    assert calls == [] and deployed == ""


def test_the_staleness_gate_is_asked_about_the_named_commit(tmp_path):
    """The gate reads HEAD unless told otherwise, and under --at HEAD is the wrong commit."""
    repo, first, _second = _prepared(tmp_path)
    _result, calls, _deployed = _run(tmp_path, repo, "--tags", "sonarr", "--at", first)
    gate = next(c for c in calls if "deploy_staleness.py" in c)
    assert f"--sha {first}" in gate, calls


def test_without_at_the_staleness_gate_is_asked_about_head(tmp_path):
    """CLEAN half: no --sha on the ordinary path, where HEAD is what renders."""
    repo, _first, _second = _prepared(tmp_path)
    _result, calls, _deployed = _run(tmp_path, repo, "--tags", "sonarr")
    assert "--sha" not in next(c for c in calls if "deploy_staleness.py" in c)


def test_tag_validation_reads_containers_list_at_the_named_commit(tmp_path):
    """A PR that adds a role and its containers_list entry declares its tag in no working tree."""
    repo, first, _second = _prepared(tmp_path)
    _result, calls, _deployed = _run(tmp_path, repo, "--tags", "sonarr", "--at", first)
    validate = next(c for c in calls if "deploy_tags.py validate" in c)
    assert f"--at {first}" in validate, calls


def test_without_at_tag_validation_reads_the_working_tree(tmp_path):
    """CLEAN half: the flag is forwarded only when it was given."""
    repo, _first, _second = _prepared(tmp_path)
    _result, calls, _deployed = _run(tmp_path, repo, "--tags", "sonarr")
    assert "--at" not in next(c for c in calls if "deploy_tags.py validate" in c)


def test_an_at_with_no_value_is_refused(tmp_path):
    """FLAGGED half: reading past the end of argv used to leave `--at` empty, silently.

    An empty `at_ref` passes every check below it and `make_snapshot` falls through to
    `${at_sha:-HEAD}`, so `--at "$sha"` with an unset variable deployed this checkout's tip and
    said nothing about it. Measured before the fix: rc=0, deployed == HEAD, empty stderr.
    """
    repo, _first, _second = _prepared(tmp_path)
    cases = (("--tags", "x", "--at"), ("--tags", "x", "--at", ""), ("--at=",))
    for n, argv in enumerate(cases):
        case = tmp_path / f"case{n}"
        case.mkdir()
        result, calls, deployed = _run(case, repo, *argv)
        assert result.returncode == _BAD_FLAGS_EXIT, (
            argv,
            result.stdout,
            result.stderr,
        )
        assert "--at needs a commit" in result.stderr
        assert calls == [] and deployed == ""


def test_an_at_does_not_swallow_the_flag_after_it(tmp_path):
    """`--at --tags sonarr` names no commit, so it is the refusal above, not a tag named `--tags`."""
    repo, _first, _second = _prepared(tmp_path)
    result, _calls, _deployed = _run(tmp_path, repo, "--at", "--tags", "alpha")
    assert result.returncode == _BAD_FLAGS_EXIT, result.stderr
    assert "--at needs a commit" in result.stderr


def test_the_detach_notifier_gates_the_snapshot_that_was_deployed(tmp_path):
    """The second entry point to the same health gate, and it had no `cwd`.

    `probe.py health <tag>` enumerates the workloads to check from its own working directory,
    so the notifier must be pointed at the snapshot -- which therefore has to outlive the
    playbook. Gating this checkout instead answers about the WORKING TREE: under `--at` a
    different commit entirely, and a role the deployed commit adds enumerates nothing there and
    posts `skipped` to Discord.
    """
    repo, first, second = _prepared(tmp_path)
    notified = tmp_path / "notify-sha"
    result, _calls, _deployed = _run(
        tmp_path,
        repo,
        "--detach",
        "--tags",
        "alpha",
        "--skip-tag-check",
        "--skip-staleness-check",
        "--at",
        first,
    )
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 30
    while not notified.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert notified.exists(), "the backgrounded notifier never ran"
    assert notified.read_text().strip() == first
    assert notified.read_text().strip() != second


def test_the_annotation_names_the_commit_that_was_deployed(tmp_path):
    """The annotation outlives the snapshot, and Grafana renders it as the record of the run.

    It reads `snapshot_sha`, captured inside the worktree while it existed. Re-reading
    `git rev-parse HEAD` at emit time -- the fallback that arm still carries -- would name this
    checkout's tip under `--at`, a commit the run never rendered.
    """
    repo, first, second = _prepared(tmp_path)
    _result, calls, _deployed = _run(
        tmp_path, repo, "--skip-tag-check", "--skip-staleness-check", "--at", first
    )
    sha = _annotated_sha(calls)
    assert first.startswith(sha), (sha, first)
    assert not second.startswith(sha)


def test_without_at_the_annotation_names_head(tmp_path):
    """CLEAN half: the ordinary path annotates the commit it did snapshot, which is HEAD."""
    repo, first, second = _prepared(tmp_path)
    _result, calls, _deployed = _run(
        tmp_path, repo, "--skip-tag-check", "--skip-staleness-check"
    )
    sha = _annotated_sha(calls)
    assert second.startswith(sha), (sha, second)
    assert not first.startswith(sha)
