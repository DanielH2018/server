# ansible/roles/setup/gitops_deploy/files/deploy_io.py
"""The boundaries the deployer crosses to act: git, the repo's files, the deploys.

`deploy_logic.py` and the `deploy_*` modules behind it hold the decisions, which are pure.
This module holds their I/O counterparts, which until now sat in `gitops_deploy.py` beside
`main()` — `deploy_k8s()` next to `deploy_k8s.py`. Splitting them out is what lets
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
  replaces the field, not this module. `k8s_declarations_at`, `k8s_image_diff`, `deploy_k8s`
  and `deploy_broad` are not fields, because the argv they build is what the suite asserts on.
  Each takes a required `run` instead, which a caller passes as `tools.run`.**

Stdlib only: the unit runs under `uv run --no-project`, never from a venv. The `# DECIDED:`
marker at `templates/gitops-deploy.service.j2`'s `ExecStart` says why.
"""

import os
import pathlib
import signal
import subprocess
from collections.abc import Callable

from deploy_config import (  # noqa: F401 — re-exported for `deploy_io.<name>` callers
    Config,
    ConfigError,
    load_config,
    log,
    read_config_file,
)
from deploy_failtext import (  # noqa: F401 — re-exported for `deploy_io.<name>` callers
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
from deploy_k8s import k8s_role_paths
from deploy_locks import locked_budget
from deploy_state import STATE_DIR, DeployerState  # noqa: F401 — re-exported


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
    except OSError:
        return None


# ── reading k8s roles out of git ──────────────────────────────────────────────────────────────


def k8s_declarations_at(
    repo: str, ref: str, *, run: Callable[..., str]
) -> dict[str, str | None]:
    """Every k8s role's defaults/main.yml as it exists at `ref`.

    Reads the ref directly rather than the working tree: the promotion decision runs BEFORE the
    ff-merge, so the working tree still holds the pre-merge declarations — exactly as stale as
    the config we are checking it against.

    A role directory present at the ref with no defaults/main.yml maps to None, which
    declared_denylist() reads as denied. The path parsing itself is k8s_role_paths(), a pure
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


def k8s_image_diff(
    repo: str, local: str, origin: str, svc: str, *, run: Callable[..., str]
) -> str:
    """Unified diff of one k8s role's defaults/main.yml across the incoming range.

    -U0 drops context lines, so is_image_only_diff classifies changed lines only — an
    unrelated neighbouring var sitting next to the pin cannot make a clean bump look dirty.
    """
    return run(
        [
            "git",
            "diff",
            "-U0",
            f"{local}..{origin}",
            "--",
            f"ansible/roles/k8s/{svc}/defaults/main.yml",
        ],
        cwd=repo,
    )


def read_local_k8s_default(repo: str, role: str) -> str | None:
    """Read a k8s role's defaults/main.yml from the CURRENT working tree, not via `git show`.

    Only called from the rollback path, after `git reset --hard local` — so a plain read
    matches exactly what roles/k8s/manifests itself reads for the claim list (see the comment
    above the revert task in that role's tasks/main.yml).
    """
    path = (
        pathlib.Path(repo)
        / "ansible"
        / "roles"
        / "k8s"
        / role
        / "defaults"
        / "main.yml"
    )
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


# ── deploying ─────────────────────────────────────────────────────────────────────────────────

# The argv prefix every deploy shares. `uv run` gives the deploy the repo's pinned env
# (ansible-core plus the community.docker deps requests/docker) — the same toolchain the
# operator uses; `--frozen` installs from the committed uv.lock rather than mutating it here.
PLAYBOOK_ARGV = ("uv", "run", "--frozen", "ansible-playbook")


def deploy_k8s(
    repo: str,
    services: set[str],
    timeout: float,
    restore_sha: str | None = None,
    *,
    run: Callable[..., str],
) -> None:
    """Deploy k8s services by tag. The rollout gate lives INSIDE the role.

    No health-poll phase here on purpose: the play already runs apply (roles/k8s/manifests)
    -> `rollout status --timeout` (roles/k8s/manifests/tasks/drain.yml) -> a post-Available soak
    (post_tasks/k8s_stabilise_gate.yml) that hard-fails on a restart-count delta or a
    readiness shortfall. Polling again would duplicate it.

    The wait and the soak moved out of roles/k8s/manifests in 5eea64e6, when rollouts were
    batched and the stabilisation window deferred to end-of-play; the sequence above is
    unchanged. assert_stable.yml is gone entirely as of 2026-08-22: observability was its last
    caller, and it now hands its six telemetry workloads to the same end-of-play gate as
    everything else rather than running a second 60s window of its own.

    restore_sha, when given, is passed to the play as the `k8s_restore_snapshot_sha` extra-var,
    which roles/k8s/manifests reads to revert each service's claimed volumes to the snapshot
    named for that SHA before re-applying. Omitted (the ordinary deploy) or blank, the extra-var
    is never added — the call is byte-identical to before this argument existed.
    """
    tags = ",".join(sorted(services))
    log(f"deploying k8s services: {tags} (timeout {timeout:.0f}s)")
    argv = [*PLAYBOOK_ARGV, "ansible/deploy.yml", "--tags", tags]
    if restore_sha is not None and restore_sha.strip():
        argv += ["-e", f"k8s_restore_snapshot_sha={restore_sha}"]
    with locked_budget(services, timeout) as budget:
        run(argv, cwd=repo, timeout=budget)


def deploy_broad(
    repo: str,
    playbook: str,
    tags: list[str],
    timeout: float,
    *,
    run: Callable[..., str],
) -> None:
    """Run a broad-plane playbook, bounded. Raises on failure or timeout.

    `uv run --frozen` for the same reason deploy() uses it: the repo's pinned env, and never
    mutating uv.lock on the host.

    No tags would mean the whole playbook, and the tick never asks for it. A refused
    deploy-plane narrowing defers the plane instead (#4333), because the whole
    `ansible/deploy.yml` does not fit the budget this call is given. The setup plane never
    lands here unscoped either: setup_tags_for returning an empty set routes to the
    defer-and-alert arm, because an unscoped initial_setup.yml is a whole-host reprovision.
    """
    cmd = [*PLAYBOOK_ARGV, playbook]
    if tags:
        cmd += ["--tags", ",".join(tags)]
    # `all` EXCLUSIVE whatever the tags: this reconfigures the host, not one service.
    with locked_budget(tags, timeout, exclusive_all=True) as budget:
        run(cmd, cwd=repo, timeout=budget)


def emit_deploy_annotation(services: set[str], sha: str) -> None:
    """Record a successful auto-deploy where Grafana can draw it as a dashboard annotation.

    A LOG LINE, not a POST to Grafana's /api/annotations, and the peer of `annotate` in
    scripts/deploy_tools/deploy_playbook.py (bash's for `--detach`) — the deploy paths must
    annotate the same way or the dashboards show only half the deploys. Grafana has no hostPort
    and no pinned ClusterIP, and this runs on the HOST, so calling in would mean pinning a fourth
    address or routing through Traefik with a standing write credential. Neither is needed: the
    Alloy shipper tails /var/log/syslog into loki-homelab, and Grafana reads that Loki by DNS.

    Only the k8s auto-deploy path and the broad arm call this.

    Fire-and-forget: any failure is logged and swallowed. An annotation is a convenience, and a
    deploy that succeeded must not be reported as failed because recording it did not.
    """
    try:
        subprocess.run(
            [
                "logger",
                "-t",
                "deploy-annotation",
                f"event=deploy services={','.join(sorted(services))} "
                f"sha={sha[:8]} result=ok source=gitops",
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:
        log(f"deploy annotation failed (deploy itself succeeded): {exc}")
