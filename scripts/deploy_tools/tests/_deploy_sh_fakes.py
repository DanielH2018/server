"""The throwaway repo and environment every `deploy.sh` test runs the wrapper against.

`deploy.sh` copies HEAD into a detached worktree before it runs a playbook (ADR-0017), and
takes one lock per service under /var/lock. Run against this checkout with the real
environment, every test here would register a worktree in the real `.git` and flock files a
live deploy uses. These three helpers are what keep a run inside its own tmp_path.

A module rather than conftest fixtures: `from conftest import x` resolves to whichever
conftest.py sys.path reached first once the whole repo suite runs, and this repo has three.
`_land_fakes.py` is here for the same reason.

Typical usage example:

    bin_dir = ...  # stub uv, flock, ...
    repo = make_snapshot_repo(tmp_path / "repo")
    env = deploy_sh_env(tmp_path, bin_dir)
    subprocess.run([DEPLOY_SH, "--tags", "x"], cwd=repo, env=env)
"""

import os
import re
import shutil
import sys
from pathlib import Path, PurePath

from lib.proc_testing import fake_bin, path_with
from lib.git_testing import git, init_repo, scrub_process_git_env

from deploy_locks import TREE_LOCK
from lib.repo_paths import REPO as _REPO

# The module `deploy.sh` reads its service locks from, at the path the wrapper runs it by.
# `make_snapshot_repo` copies it into every throwaway repo, so the wrapper's `uv run python
# ansible/.../deploy_locks.py plan` resolves there the way it does in a real checkout.
DEPLOY_LOCKS_REL = Path("ansible/roles/setup/gitops_deploy/files/deploy_locks.py")

# The shared `flock` stub: drop flock's own options and its lock-file argument, then run
# whatever is left. No real lock is taken, so no test can interleave with a live deploy.
# `-s` and `-x` are in the single-shift arm because a scoped deploy takes
# `server-deploy-all.lock` shared and the snapshot owner lock is taken `-n -x`; without them
# the stub tries to exec `-w`.
FLOCK_STUB = """#!/bin/bash
while [[ $# -gt 0 ]]; do
  case "$1" in
    -w|-E) shift 2 ;;
    -n|-u|-s|-x) shift ;;
    *) break ;;
  esac
done
shift
exec "$@"
"""

# The `case` arms every `uv` stub carries for the wrapper's own two `uv run` calls, both run
# for REAL under the interpreter running the tests (`deploy_sh_env` sets DEPLOY_TEST_PYTHON):
#
# - `deploy.sh` is a shim that execs `uv run --project <dir> python <deploy_run.py> ...`, so
#   without this arm a stub's `*) exit 0` would swallow the whole run and report success.
#   `shift 4` drops `run --project <dir> python`, the shim's exact prefix.
# - `deploy_locks.py plan` is not scripted either: a stub that printed its own lock order
#   would make the concurrency tests flock whatever the stub said, and prove nothing about
#   the deployer's names. `shift 2` drops `run python`.
#
# A test that wants a BROKEN plan writes its own `deploy_locks.py` arm and carries
# UV_DEPLOY_RUN_ARM alone.
UV_DEPLOY_RUN_ARM = '  *deploy_run.py*) shift 4; exec "$DEPLOY_TEST_PYTHON" "$@" ;;'
UV_WRAPPER_ARMS = (
    UV_DEPLOY_RUN_ARM
    + '\n  *deploy_locks.py*) shift 2; exec "$DEPLOY_TEST_PYTHON" "$@" ;;'
)

# The tags the throwaway repo's `containers_list` declares. `deploy_run.py` validates
# `--tags` IN PROCESS against the caller's own host_vars, so a stubbed `uv` no longer answers
# for it: a tag a test passes must be declared here, or the run refuses with exit 2.
TEST_SERVICE_TAGS = (
    "alpha",
    "authelia",
    "beta",
    "jellyfin",
    "n8n",
    "pi-peer-backup",
    "pihole",
    "radarr",
    "sonarr",
    "traefik",
    "uptime-kuma",
    "x",
)

# What a stubbed `ansible-playbook` must print for `deploy.sh` to count the run as a deploy.
# The wrapper reads the PLAY RECAP and refuses (exit 78) when it names no host -- ansible
# exits 0 for that -- so a stub that exits 0 in silence is a no-host run, not a success. One
# host line, in ansible's own layout, is the whole requirement. `test_deploy_exit_codes.py`
# holds the red half, where a stub prints the banner alone.
FAKE_RECAP = (
    'echo "PLAY RECAP *********"; '
    'echo "daniel-box                 : ok=3    changed=1    unreachable=0    failed=0"'
)


def git_free_env(**overrides: str) -> dict[str, str]:
    """The environment a `deploy.sh` child runs under: no `GIT_*`, plus `overrides`.

    Under a prek hook the environment carries `GIT_DIR` and `GIT_INDEX_FILE` pointing at the
    REAL repository, and those beat the child's working directory — so a run that looks scoped
    to a tmp_path would snapshot, and create worktrees in, this checkout.

    This is `lib.git_testing.scrubbed_env` without the scratch commit identity: the subject
    here is a shell script's whole environment rather than one git call, and `deploy.sh` runs
    read-only git verbs that an author name would only add noise to.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(overrides)
    return env


def deploy_sh_env(tmp_path: Path, bin_dir: Path, **overrides: str) -> dict[str, str]:
    """The environment a `deploy.sh` run needs to stay inside `tmp_path`.

    The TREE lock is redirected too. `deploy.sh` takes it for real, for the snapshot, and the
    production path is the one every holder in docs/deploying.md's list takes — so a
    test taking it would queue behind real work and hold real work up behind itself. So is
    the deployer's state directory, which a successful run's `k8s_unapplied` discharge
    rewrites, and the release records that discharge reads.

    Args:
        tmp_path: the test's own directory; the snapshot root and lock directory go under it.
        bin_dir: the stub directory to put first on PATH.
        overrides: extra variables, applied last.
    """
    locks = tmp_path / "locks"
    locks.mkdir(exist_ok=True)
    return git_free_env(
        PATH=path_with(bin_dir, detach_stub_bin(tmp_path)),
        HOMELAB_DEPLOY_SNAPSHOT_ROOT=str(tmp_path / "snapshots"),
        HOMELAB_DEPLOY_LOCK_DIR=str(locks),
        HOMELAB_DEPLOY_TREE_LOCK=str(locks / PurePath(TREE_LOCK).name),
        # A successful run rewrites the deployer's owed ledger; never the host's.
        HOMELAB_DEPLOY_GITOPS_STATE_DIR=str(tmp_path / "gitops-state"),
        HOMELAB_DEPLOY_RELEASE_DIR=str(tmp_path / "releases"),
        DEPLOY_TEST_PYTHON=sys.executable,
        **overrides,
    )


def make_snapshot_repo(path: Path) -> Path:
    """A one-commit git repo `deploy.sh` can snapshot HEAD from; returns `path`.

    Carries `ansible/deploy.yml` because that is the argument the wrapper hands
    ansible-playbook. The content never matters: every test that uses this stubs `uv`. Carries
    the real `deploy_locks.py` too, because the wrapper reads its lock list from that module
    at a checkout-relative path and the stubbed `uv` (UV_WRAPPER_ARMS) runs it for real.

    A host_vars declaring TEST_SERVICE_TAGS exists for the tag validation, which
    `deploy_run.py` calls in process against this checkout.
    """
    # `lib.git_testing.init_repo` carries the identity and the signing-off config in the
    # environment, so none of it has to be written into the scratch repository's own config.
    init_repo(path, branch="main")
    (path / "ansible").mkdir(exist_ok=True)
    (path / "ansible" / "deploy.yml").write_text("---\n[]\n")
    host_vars = path / "ansible" / "inventory" / "host_vars"
    host_vars.mkdir(parents=True, exist_ok=True)
    (host_vars / "daniel-box.yml").write_text(
        "containers_list:\n"
        + "".join(f"  - {{ name: {tag} }}\n" for tag in TEST_SERVICE_TAGS)
    )
    (path / DEPLOY_LOCKS_REL).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_REPO / DEPLOY_LOCKS_REL, path / DEPLOY_LOCKS_REL)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "seed", "--no-gpg-sign")
    return path


# What a `--detach` child runs to leave a systemd user unit's cgroup (`lib/detach_fork.py`).
# Refused, so a run under a fan-out unit or claude-rc.service stays where it is instead of
# creating a real scope on the host; the child logs the refusal and carries on.
BUSCTL_REFUSED = (
    "#!/bin/sh\necho 'busctl stubbed by the deploy test harness' >&2\nexit 1\n"
)


def detach_stub_bin(tmp_path: Path) -> Path:
    """A directory holding the refusing `busctl` every detached run in these tests finds."""
    return fake_bin(tmp_path / "detach-bin", busctl=BUSCTL_REFUSED)


def stub_bin(tmp_path: Path, stubs: dict[str, str]) -> Path:
    """Write each stub as an executable under `tmp_path/bin` and return that directory.

    This suite's spelling of `lib.proc_testing.fake_bin`: the directory is always
    `tmp_path/bin`, because `deploy_sh_env` and every module here agree on that name.
    """
    return fake_bin(tmp_path / "bin", **stubs)


def stub_path(tmp_path: Path, stubs: dict[str, str]) -> dict[str, str]:
    """`os.environ` with the stubs from `stub_bin` ahead of everything on PATH."""
    return dict(os.environ, PATH=path_with(stub_bin(tmp_path, stubs)))


# What `deploy.sh --detach` prints once it has forked the process that runs the playbook.
_DETACHED_PID = re.compile(r"running in background \(pid (\d+)\)")


def detached_pid(output: str) -> int:
    """The pid of the process a `--detach` run forked, from the wrapper's own output.

    That process is a grandchild of the wrapper, not its child (`lib/detach_fork.py`), which
    `wait_for_exit` handles through a pidfd. It runs the playbook, the notifier and
    `remove_snapshot`, then exits — so its
    exit is the event a test waits on for anything the detached half does, via
    `_process_waits.wait_for_exit`, rather than polling for its side effects.
    """
    match = _DETACHED_PID.search(output)
    assert match, f"deploy.sh --detach printed no background pid:\n{output}"
    return int(match.group(1))


class Execed(Exception):
    """What `run_front_half`'s fake exec raises; `argv` is the command the run became."""

    def __init__(self, argv: list[str]):
        super().__init__(argv)
        self.argv = argv


def run_front_half(
    monkeypatch,
    repo: Path,
    argv: list[str],
    *,
    stale: int = 0,
    validate: int = 0,
    changed: tuple[int, str] = (0, ""),
) -> tuple[int | None, list[tuple]]:
    """Run `deploy_run.run(argv)` in `repo` with each helper replaced by a recorder.

    The front half calls its helpers in process, so a stubbed `uv` cannot answer for them:
    `deploy_run.Tools` is the seam that injects their verdicts instead. The argument passes, the `--at`
    resolution and the order of the gates are the real code, run against a real repo.

    Returns the refusal code (None when the run reached its exec) and the calls in order:
    `("staleness", tags, at_sha)`, `("validate", tags, at_sha)`, `("changed", ref)`,
    and, once the run reaches its deploy, `("deploy", what)`: the
    argv an exec'd mode became, or `["in-process", tags_csv]` for the locked half.
    """
    import deploy_run

    # Under a prek hook GIT_DIR points at the REAL repository and beats the working directory.
    scrub_process_git_env(monkeypatch)
    monkeypatch.chdir(repo)
    calls: list[tuple] = []

    def staleness(plan):
        calls.append(("staleness", tuple(plan.tags), plan.at_sha))
        return stale

    def validate_tags(plan):
        calls.append(("validate", tuple(plan.tags), plan.at_sha))
        return validate

    def derive(plan):
        calls.append(("changed", plan.changed_ref))
        return changed

    def fake_exec(target):
        calls.append(("deploy", target))
        raise Execed(target)

    def fake_locked(plan):
        target = ["in-process", plan.tags_csv]
        calls.append(("deploy", target))
        raise Execed(target)

    tools = deploy_run.Tools(
        staleness=staleness,
        validate=validate_tags,
        changed=derive,
        exec_argv=fake_exec,
        locked_run=fake_locked,
    )
    try:
        return deploy_run.run(list(argv), tools), calls
    except Execed:
        return None, calls
