# ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_io.py
"""The boundaries the deployer crosses to act: git, the repo's files, the deploys.

`deploy_logic.py` and the `deploy_*` modules behind it hold the decisions, which are pure.
This module holds their I/O counterparts, which until now sat in `gitops_deploy.py` beside
`main()`. Splitting them out is what lets
`gitops_deploy.main()` be a sequence of named phases a test can drive one at a time.

Three boundaries are leaves of their own: `deploy_config` (the config file, `Config`, `log`),
`deploy_state` (the marker files under /var/lib/gitops-deploy) and `deploy_failtext` (bounding
a failed run's output). Each is reachable without faking a subprocess, and each imports nothing
from here. This module re-exports their names so a `deploy_io.<name>` caller outside `files/`
still resolves; nothing inside `files/` reads them that way.

Two rules hold this module's shape:

- **Nothing here reads a module-level constant of `gitops_deploy`.** Every function takes what
  it needs — `repo`, `hostname`, a timeout — as an argument, so a test can call it directly and
  so `gitops_deploy` stays the one place the deployer's configuration is bound.
- **Most of these reach a caller as a `DeployTools` field (`deploy_toolbox.py`), so a test
  replaces the field, not this module. `k8s_declarations_at` and `deploy_broad` are not
  fields, because the argv they build is what the suite asserts on.
  Each takes a required `run` instead, which a caller passes as `tools.run`.**

Stdlib only: the unit runs under `uv run --no-project`, never from a venv. The `# DECIDED:`
marker at `templates/gitops-deploy.service.j2`'s `ExecStart` says why.
"""

import os
import signal
import subprocess
from collections.abc import Callable

from deployer_lib.deploy_config import (  # noqa: F401 — re-exported for `deploy_io.<name>` callers
    WARNING,
    Config,
    ConfigError,
    load_config,
    log,
    read_config_file,
)
from deployer_lib.deploy_failtext import (  # noqa: F401 — re-exported for `deploy_io.<name>` callers
    RUN_ERROR_STDERR_TAIL,
    RUN_ERROR_STDOUT_CHARS,
    TRUNCATED,
    TimedOutWithOutput,
    decoded,
    failing_task,
    failure_detail,
    head,
    last_task,
    running_task_detail,
    tail,
    timeout_detail,
)
from deployer_lib.deploy_inventory import service_owners
from deployer_lib.deploy_locks import locked_budget
from deployer_lib.deploy_state import STATE_DIR, DeployerState  # noqa: F401 — re-exported


# ── subprocess ────────────────────────────────────────────────────────────────────────────────


def run(
    args: list[str],
    *,
    cwd: str | None,
    check: bool = True,
    timeout: float | None = None,
) -> str:
    """Run a subprocess and return its stripped stdout.

    Args:
        args: the argv to execute.
        cwd: working directory for the subprocess. Keyword-only and required: it was
            `gitops_deploy.REPO` by default while this lived there, and a call that silently
            inherited the deployer's own cwd instead would run git against the wrong tree.
        check: raise RuntimeError if the process exits non-zero.
        timeout: wall-clock bound in seconds, or None to wait indefinitely. When set, the
            process runs in its own process group so a timeout kills the whole group —
            including a grandchild like `ansible-playbook` forked by `uv run` — not just the
            direct child.

    Raises:
        RuntimeError: the process exited non-zero and `check` is True.
        deploy_failtext.TimedOutWithOutput: the process (and its group) was killed after
            `timeout`. A `subprocess.TimeoutExpired` subclass, so an `except
            subprocess.TimeoutExpired` arm still catches it; its `str()` additionally carries
            a bounded tail of what the killed process had printed, which on an
            `ansible-playbook` run names the task that was still running.
    """
    # timeout defaults to None so the long deploy/git calls are unbounded as before;
    # only the k8s deploy/rollback calls pass one.
    if timeout is None:
        r = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=None)
    else:
        # `uv run ansible-playbook ...` is a GRANDCHILD of this process (uv forks it rather
        # than exec'ing into it). `subprocess.run(timeout=)` DOES return on time even so — its
        # internal communicate() raises on the wall-clock deadline, not on pipe EOF — but on
        # timeout it kills only the direct child (uv). The grandchild (ansible-playbook) is
        # left running, unkilled, an orphan mutating the cluster with nothing left watching it.
        # Verified empirically: a plain subprocess.run(timeout=) returns promptly, and the
        # grandchild is still alive at that moment. That is how K8S_ROLLBACK_TIMEOUT_S stopped
        # being an actual bound on the underlying work: gitops_deploy.py moves on (to a second
        # rollback attempt, or exits and lets the next tick start a fresh run) while the timed-
        # out ansible-playbook keeps applying manifests in the background — the real stop
        # becomes whatever kills that orphan, normally nothing, or systemd's TimeoutStartSec
        # SIGTERM against the WRAPPING unit, which can land mid-rollback.
        #
        # start_new_session puts the direct child in a NEW process group (its pgid equals its
        # own pid), which every process it forks inherits unless one of them calls setsid
        # itself. killpg on timeout then signals that whole group at once, so uv and
        # ansible-playbook die together instead of one outliving the other.
        # The `with` closes both pipes on the way out, timeout path included; without it the
        # two read ends stay open until GC, which is a ResourceWarning under pytest's
        # filterwarnings=error. CPython's subprocess.run() wraps its Popen the same way.
        with subprocess.Popen(
            args,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        ) as proc:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # the group is already gone
                # wait(), not communicate(): if a descendant escaped the group by calling
                # setsid itself, its end of the pipe stays open and communicate() would block
                # on it forever. wait() only reaps the direct child's exit status and doesn't
                # touch the pipes — CPython's own subprocess.run() does the same on this path.
                proc.wait()
                # The stdlib already read this: Popen._communicate accumulates every chunk
                # into _fileobj2output as it goes, and _check_timeout attaches what it had
                # to the TimeoutExpired it raises. Re-reading the pipes here would risk the
                # block the wait() above exists to avoid, and would find nothing more that
                # mattered. What was missing is that TimeoutExpired.__str__ prints only the
                # argv and the deadline, so the running TASK header never reached the log.
                raise TimedOutWithOutput(
                    exc.cmd, exc.timeout, output=exc.output, stderr=exc.stderr
                ) from exc
        r = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    if check and r.returncode != 0:
        detail = "\n".join(
            part
            for part in (
                failure_detail(r.stdout, RUN_ERROR_STDOUT_CHARS),
                tail(r.stderr, RUN_ERROR_STDERR_TAIL),
            )
            if part
        )
        raise RuntimeError(f"{' '.join(args)} -> {r.returncode}\n{detail}")
    return r.stdout.strip()


# ── git ───────────────────────────────────────────────────────────────────────────────────────
# DECIDED: `run()` above is the deployer's one git wrapper, and every other module reaches it as
# `tools.run(["git", ...])`. The three helpers below are the only raw `subprocess` git calls, and
# each is raw on purpose: `run()` raises on a non-zero exit and `run(check=False)` discards the
# returncode and stderr, while these three must read them. `deploy_git` is not the home for git
# I/O: it is the pure decision module, unit-tested without a repo (#3730).


def is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    """True if `ancestor` is an ancestor of (or equal to) `descendant`.

    Used to decide whether origin is strictly ahead of local — only then is there anything to
    fast-forward and deploy (see next_action's origin_ahead). A git error (bad object, etc.) is a
    non-zero exit and conservatively reads False, so the tick degrades into a no-op rather than a
    mis-fired deploy.

    NOT `run(...)`: the answer IS the returncode — exit 1 means "not an ancestor", which `run()`
    would raise on and `run(check=False)` would discard.
    """
    r = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo,
        capture_output=True,
    )
    return r.returncode == 0


def git_status(repo: str) -> subprocess.CompletedProcess:
    """`git status --porcelain`, unchecked.

    NOT `run(...)`: on 2026-08-17 14:33 this raised `RuntimeError: git status --porcelain ->
    128 / fatal: this operation must be run in a work tree` and double-paged (crash Discord +
    OnFailure), while the very next tick ran normally — a transient tree state, not a broken
    checkout. The caller turns a non-zero exit into a RetryableFetchError, which skips the tick
    cleanly and does NOT write last_run. SCOPE #1223: whole tree, untracked-inclusive by design.
    """
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, text=True, capture_output=True
    )


def git_fetch(repo: str, branch: str) -> subprocess.CompletedProcess:
    """`git fetch origin <branch>`, unchecked, for the same reason as `git_status`.

    A transient fetch failure is retryable, so the caller raises RetryableFetchError and lets
    entrypoint() skip the tick. subprocess directly, to read the returncode/stderr that
    `run(check=False)` would discard.
    """
    return subprocess.run(
        ["git", "fetch", "origin", branch], cwd=repo, text=True, capture_output=True
    )


# ── the repo's files ──────────────────────────────────────────────────────────────────────────


def host_vars_text(repo: str, hostname: str) -> str | None:
    """This host's `host_vars/<hostname>.yml`, or None when it cannot be read."""
    path = os.path.join(repo, "ansible", "inventory", "host_vars", f"{hostname}.yml")
    try:
        with open(path) as fh:
            return fh.read()
    except OSError as exc:
        # Both callers read None as "no host_vars", which is a gate's answer, so say why (#6161).
        log(f"host_vars: cannot read {path}, read as absent: {exc}", level=WARNING)
        return None


def host_vars_owners(
    config: Config, ref: str, *, run: Callable[..., str]
) -> tuple[str, dict[str, str]]:
    """This host's host_vars text in the checkout, and `service_owners` of the file at `ref`.

    The text is `""` when the checkout's file cannot be read. The owners are read at `ref`,
    the commit being applied, with `git show` rather than from the checkout, because
    `plan_tick` runs before the ff-merge and the range that sets `owner` on an entry must
    already skip that entry's tag.

    Raises:
        ValueError: from `service_owners`, for an `owner` value outside its two.
    """
    checkout = host_vars_text(config.repo, config.hostname) or ""
    path = f"ansible/inventory/host_vars/{config.hostname}.yml"
    try:
        text = run(["git", "show", f"{ref}:{path}"], cwd=config.repo)
    except Exception as exc:
        # DECIDED: an unreadable file at `ref` falls back to the checkout, not to "every tag
        # is Ansible-owned". The checkout is the last commit a tick applied, so it carries
        # every `owner` an earlier range merged, and a tag Flux already applies stays
        # dropped. Only an `owner` added in this very range is missed, and that tag then
        # takes the Ansible path for one tick, as every tag did before D2.2. An empty mapping
        # would instead hand every Flux-owned tag back to Ansible.
        log(
            f"owners: could not read {path} at {ref[:8]} ({exc}) — reading ownership "
            "from the checkout",
            level=WARNING,
        )
        text = checkout
    return checkout, service_owners(text)


# ── reading k8s roles out of git ──────────────────────────────────────────────────────────────


def k8s_role_paths(listing: str) -> dict[str, str | None]:
    """Map each k8s role to its defaults/main.yml path in a `git ls-tree -r --name-only` listing.

    A role with no defaults/main.yml at that ref maps to None, which
    `deploy_phases.declared_denylist` reads as denied. Paths look like
    `ansible/roles/k8s/<role>/<rest...>`, so the role name is the FOURTH segment — this
    indexing shipped an off-by-one once already.

    Pure string parsing, callable on its own so it's unit-testable without git; the I/O caller
    is `k8s_declarations_at`.
    """
    roles: dict[str, str | None] = {}
    for path in listing.splitlines():
        # "ansible/roles/k8s/<role>/<rest...>" — a real role always has a file at least one
        # directory deep, so anything shallower (a stray file directly under roles/k8s/) is
        # not a role and must not be recorded as one.
        parts = path.split("/")
        if len(parts) < 5:
            continue
        role = parts[3]
        if parts[4] == "defaults" and path.endswith("defaults/main.yml"):
            roles[role] = path
        else:
            # Still record the role so one with no defaults/ at all is visible as None. A
            # setdefault here never clobbers an already-found defaults/main.yml path, and
            # doesn't need to run before it either — the listing's order doesn't matter.
            roles.setdefault(role, None)
    return roles


def k8s_declarations_at(
    repo: str, ref: str, *, run: Callable[..., str]
) -> dict[str, str | None]:
    """Every k8s role's defaults/main.yml as it exists at `ref`.

    Reads the ref directly rather than the working tree. `deploy_phases.reconcile_denylist`
    compares it with the denylist the deployer's config was rendered from.

    A role directory present at the ref with no defaults/main.yml maps to None, which
    `deploy_phases.declared_denylist` reads as denied. The path parsing itself is k8s_role_paths(), a pure
    function unit-tested without git; this function does only the git I/O around it.
    """
    listing = run(
        ["git", "ls-tree", "-r", "--name-only", ref, "ansible/roles/k8s/"], cwd=repo
    )
    paths = k8s_role_paths(listing)
    return {
        role: run(["git", "show", f"{ref}:{path}"], cwd=repo)
        if path is not None
        else None
        for role, path in paths.items()
    }


# ── deploying ─────────────────────────────────────────────────────────────────────────────────

# The argv prefix every deploy shares. `uv run` gives the deploy the repo's pinned env
# (ansible-core plus the community.docker deps requests/docker) — the same toolchain the
# operator uses; `--frozen` installs from the committed uv.lock rather than mutating it here.
PLAYBOOK_ARGV = ("uv", "run", "--frozen", "ansible-playbook")


def deploy_broad(
    repo: str,
    playbook: str,
    tags: list[str],
    timeout: float,
    *,
    run: Callable[..., str],
) -> None:
    """Run a broad-plane playbook, bounded. Raises on failure or timeout.

    `uv run --frozen` gives the run the repo's pinned env, and never mutates uv.lock on the
    host.

    No tags would mean the whole playbook, and the tick never asks for it: setup_tags_for
    returning an empty set routes to the defer-and-alert arm, because an unscoped
    initial_setup.yml is a whole-host reprovision.
    """
    cmd = [*PLAYBOOK_ARGV, playbook]
    if tags:
        cmd += ["--tags", ",".join(tags)]
    # `all` EXCLUSIVE whatever the tags: this reconfigures the host, not one service.
    with locked_budget(tags, timeout, exclusive_all=True) as budget:
        run(cmd, cwd=repo, timeout=budget)
