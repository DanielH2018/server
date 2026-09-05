# ansible/roles/setup/gitops_deploy/files/deploy_io.py
"""The boundaries the deployer crosses to act: git, docker, the repo's files, the deploys.

`deploy_logic.py` and the `deploy_*` modules behind it hold the decisions, which are pure.
This module holds their I/O counterparts, which until now sat in `gitops_deploy.py` beside
`main()` — `health_ok` next to `deploy_health`, `consult_staging`'s subprocess half next to
`deploy_staging`, `deploy_k8s()` next to `deploy_k8s.py`. Splitting them out is what lets
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
  replaces the field, not this module. `deploy`, `deploy_k8s` and `deploy_broad` are the exception:
  they call `run` qualified and the suite patches `deploy_io.run` to read the argv they build.**

Stdlib only: the unit runs under `uv run --no-project` and the host is still on Python 3.12.
"""

import json
import os
import pathlib
import signal
import subprocess
import time
from datetime import tzinfo

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
    failing_task,
    failure_detail,
    head,
    tail,
)
from deploy_health import HealthSample, containers_to_gate, health_decision
from deploy_inventory import declared_services, stale_rendered_services
from deploy_k8s import k8s_role_paths
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
        subprocess.TimeoutExpired: the process (and its group) was killed after `timeout`.
    """
    # timeout defaults to None so the long deploy/git calls are unbounded as before;
    # only the health-gate's docker inspects and the k8s deploy/rollback calls pass one.
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
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # the group is already gone
                # wait(), not communicate(): if a descendant escaped the group by calling
                # setsid itself, its end of the pipe stays open and communicate() would block
                # on it forever. wait() only reaps the direct child's exit status and doesn't
                # touch the pipes — CPython's own subprocess.run() does the same on this path.
                proc.wait()
                raise
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


def is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    """True if `ancestor` is an ancestor of (or equal to) `descendant`.

    Used to decide whether origin is strictly ahead of local — only then is there anything to
    fast-forward and deploy (see next_action's origin_ahead). A git error (bad object, etc.) is a
    non-zero exit and conservatively reads False, so the tick degrades into a no-op rather than a
    mis-fired deploy.
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
    cleanly and does NOT write last_run.
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


# ── docker ────────────────────────────────────────────────────────────────────────────────────


def inspect_field(fmt: str, container: str, timeout: float = 15.0) -> str:
    """One `docker inspect -f` field, or '' if empty/gone — or if the call exceeds `timeout`.

    The deadline in health_ok() is only checked between calls, so a wedged daemon on an unbounded
    inspect would block the whole deployer forever; bounding each inspect lets a hang degrade into a
    failed gate instead.
    """
    try:
        return run(
            ["docker", "inspect", "-f", fmt, container],
            cwd=None,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ""


def health_ok(
    container: str,
    poll_timeout_s: float,
    settle_checks: int = 3,
    deadline: float | None = None,
) -> bool:
    """True if `container` reaches 'healthy', or settles as 'running' with no HEALTHCHECK.

    For an image with no HEALTHCHECK, stays 'running' across `settle_checks` consecutive polls
    (~20s) so a boot-then-crash loop doesn't slip the gate the way a single 'running' sample
    would. Polls until `poll_timeout_s` (HEALTH_TIMEOUT_S) — or the earlier `deadline` (the
    run-wide gate budget), so one slow container can't blow the whole gate past the unit
    timeout — then fails.

    The per-sample pass/wait + streak transition is the pure, unit-tested
    `deploy_health.health_decision`; this function is just its I/O shell (docker inspect, the
    10s poll, and the wall-clock deadline). `.State.Running` is only inspected in the
    no-healthcheck case (status == ''), matching the decision's use.
    """
    per_deadline = time.time() + poll_timeout_s
    if deadline is not None:
        per_deadline = min(per_deadline, deadline)
    running_streak = 0
    while time.time() < per_deadline:
        status = inspect_field("{{.State.Health.Status}}", container)
        sample = HealthSample(
            status=status,
            running=status == ""
            and inspect_field("{{.State.Running}}", container) == "true",
        )
        verdict, running_streak = health_decision(
            sample.status, sample.running, running_streak, settle_checks
        )
        if verdict == "healthy":
            return True
        time.sleep(10)
    return False


def containers_for(repo: str, service: str) -> list[str]:
    """Container names to health-gate for a deployed service, from its rendered compose.

    Empty when the service isn't deployed on THIS host — its rendered file doesn't exist (dozzle is
    daniel-pi-only, and the deployer doesn't run on the Pi at all) — so the caller skips it instead
    of gating a phantom container (see deploy_health.containers_to_gate). A present compose that
    declares no container_name falls back to [service].
    """
    path = os.path.join(repo, "containers", service, "docker-compose.yml")
    try:
        with open(path) as fh:
            text: str | None = fh.read()
    except FileNotFoundError:
        text = None
    return containers_to_gate(text, service)


def service_healthy(
    repo: str, service: str, poll_timeout_s: float, deadline: float | None = None
) -> bool:
    """True when every container this service renders here reaches healthy.

    A role may run several containers; gate every one (the bumped image's container is often not
    the role-named one). `deadline` (the run-wide gate budget) is threaded to each container's
    poll loop.
    """
    return all(
        health_ok(c, poll_timeout_s, deadline=deadline)
        for c in containers_for(repo, service)
    )


def stale_composes(repo: str, hostname: str) -> list[str] | None:
    """Rendered composes on disk with no `containers_list` entry, or None if unreadable.

    The stale-compose trap (see deploy_inventory.stale_rendered_services for the incident
    history). None — not [] — when the inventory or the tree cannot be read: an unreadable
    checkout is not evidence that nothing is stale, and the caller stays silent rather than
    clearing its alert marker on it.
    """
    containers_dir = os.path.join(repo, "containers")
    hostvars = os.path.join(
        repo, "ansible", "inventory", "host_vars", f"{hostname}.yml"
    )
    try:
        with open(hostvars) as fh:
            declared = declared_services(fh.read())
        rendered = [
            d
            for d in os.listdir(containers_dir)
            if os.path.isfile(os.path.join(containers_dir, d, "docker-compose.yml"))
        ]
    except OSError:
        return None
    return stale_rendered_services(rendered, declared)


def host_vars_text(repo: str, hostname: str) -> str | None:
    """This host's `host_vars/<hostname>.yml`, or None when it cannot be read."""
    path = os.path.join(repo, "ansible", "inventory", "host_vars", f"{hostname}.yml")
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


# ── reading k8s roles out of git ──────────────────────────────────────────────────────────────


def k8s_declarations_at(repo: str, ref: str) -> dict[str, str | None]:
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


def k8s_image_diff(repo: str, local: str, origin: str, svc: str) -> str:
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


# ── the staging gate's transport ──────────────────────────────────────────────────────────────

# How much sooner each child times out than the subprocess.run wrapping it, so the CHILD wins
# the race. Both children map their own timeout to NO VERDICT and say which stage wedged; the
# outer subprocess.run raises TimeoutExpired instead, which the broad `except` in the caller
# logs and returns on — BEFORE the alert. Without this margin a slow or wedged staging is the
# one failure mode that pages nobody, while a staging that is merely down pages normally. The
# outer timeout stays as the backstop for a child that cannot honour its own.
INNER_TIMEOUT_MARGIN_S = 30

# Both staging scripts import yaml and jinja2, so they need the repo's pinned env — the same one
# deploy_k8s already runs ansible-playbook in. NOT sys.executable: this unit's ExecStart is
# `uv run --no-project`, which never creates or syncs a venv, so sys.executable is whatever
# venv happens to sit in WorkingDirectory. It resolves to the repo's today, and a missing or
# unsynced one would make the expectation script die at import with exit 1 — which
# staging_verdict_summary reads as REJECTED. An infrastructure fault would then report as a
# rejection on every gated tick, poisoning the one number this slice exists to collect.
#
# Both call sites pass cwd=repo, and that is load-bearing rather than tidiness: `uv run` picks
# its project from the working directory, so outside one it falls back to a bare interpreter and
# reproduces the same ModuleNotFoundError this constant exists to avoid. Observed 2026-08-28
# driving consult_staging from a scratch directory, which is exactly how a caller with a
# different cwd would hit it. The unit's WorkingDirectory happens to be REPO today; relying on
# that is what made the first version of this fix incomplete.
UV_PYTHON = ("uv", "run", "--frozen", "python")


def staging_gate_script(repo: str) -> str:
    """The staging gate's own path inside the repo the deployer renders from.

    In the repo rather than a deployed copy, so the gate is always the version under test.
    """
    return os.path.join(repo, "scripts", "deploy_tools", "staging_gate.py")


def staging_expect_script(repo: str) -> str:
    """The expectation checker's path, in the repo for the same reason as the gate's."""
    return os.path.join(repo, "scripts", "deploy_tools", "staging_expectations.py")


def run_staging_scripts(
    repo: str, sha: str, tags: str, gate_timeout_s: float, expect_timeout_s: float
) -> tuple[int, int]:
    """Deploy `tags` to staging at `sha`, then check its expectations. (deploy_rc, expect_rc).

    2 is "no verdict", and both codes start there: nothing about this function may break a prod
    deploy, so every failure — a missing script, an ssh outage, a wedged guest, a bug here —
    comes back as no verdict rather than as a rejection or an exception.

    The expectation check only means anything against what was just deployed, so it is skipped
    (left at no verdict) when the deploy itself produced no verdict.
    """
    deploy_rc = expect_rc = 2
    try:
        deploy_rc = subprocess.run(
            [
                *UV_PYTHON,
                staging_gate_script(repo),
                sha,
                "--tags",
                tags,
                "--timeout",
                str(gate_timeout_s - INNER_TIMEOUT_MARGIN_S),
            ],
            cwd=repo,
            timeout=gate_timeout_s,
            check=False,
        ).returncode
        if deploy_rc == 0:
            expect_rc = subprocess.run(
                [
                    *UV_PYTHON,
                    staging_expect_script(repo),
                    # Measure only what this tick deployed. Unscoped, a broken staging traefik
                    # made the summary reject the service that WAS gated, since the summary
                    # names the gated set and not the failing one.
                    "--services",
                    tags,
                    "--timeout",
                    str(expect_timeout_s - INNER_TIMEOUT_MARGIN_S),
                ],
                cwd=repo,
                timeout=expect_timeout_s,
                check=False,
            ).returncode
    except Exception as exc:
        # Reported as NO VERDICT rather than raised, so the caller alerts on it like every other
        # non-PASS. A bug in this function must not become a silent way past the gate, and the
        # pass-through is only acceptable on the strength of every NO VERDICT being loud.
        log(f"staging gate errored ({exc}); continuing to prod unchecked")
        return 2, 2
    return deploy_rc, expect_rc


def consume_override(path: str) -> bool:
    """Spend the operator's one-tick staging override, if it is armed. True when it was.

    Armed by creating the file, disarmed by removing it, and spent here — at the point the gate
    would block, never on entry, so arming it before a quiet tick does not burn it on a tick
    that needed nothing.

    One-shot by removal rather than by expiry: an override left armed is an override nobody
    remembers turning off, and Decision 4 asks for one that is easy and VISIBLE to use rather
    than hard. The visibility is the Discord post at the call site.
    """
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        # A marker that cannot be removed must not become a permanent override.
        log(f"staging override at {path} could not be consumed ({exc})")
        return False
    return True


def record_staging_tick(
    ledger: str, tz: tzinfo, now, sha: str, gated: set[str], verdict: str, outcome: str
) -> None:
    """Append this tick's verdict to the tick ledger. Never raises.

    Args:
        ledger: the JSONL file to append to.
        tz: the timezone the `at` stamp is rendered in (America/Chicago on the host).
        now: the clock, as a callable taking a tzinfo — `datetime.datetime.now` in production.
        sha: the commit the gate was asked about.
        gated: the services it was asked about, which is the promoted set, not the changed set.
        verdict: one of `staging_verdict`'s words, or STAGING_SKIPPED.
        outcome: `deploy_staging.staging_tick_outcome`'s word for that verdict.

    The caller decides whether there is anything to record: `deploy_handlers.record_staging_tick`
    drops a verdict that measured nothing — `staging_tick_outcome` returns None for SKIPPED —
    and only then calls this. Taking the word as an argument rather than deriving it here also
    keeps this module from importing `deploy_staging`.

    Every failure is swallowed. This runs inside the staging consultation, which may not break a
    prod deploy for any reason; a full disk or a bad permission on the ledger must cost the
    measurement, never the deploy.
    """
    record = {
        "at": now(tz).isoformat(timespec="seconds"),
        "sha": sha,
        "tags": ",".join(sorted(gated)),
        "verdict": verdict,
        "outcome": outcome,
    }
    try:
        with open(ledger, "a") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError as exc:
        log(f"could not record the staging tick in {ledger}: {exc}")


# ── deploying ─────────────────────────────────────────────────────────────────────────────────


def deploy(repo: str, services: set[str]) -> None:
    """Deploy Docker-platform `services` via `ansible/deploy.yml --tags <services>`.

    Args:
        repo: the checkout to run the playbook from.
        services: service tags to deploy, joined into one comma-separated `--tags` value.
    """
    tags = ",".join(sorted(services))
    # Run via `uv run` so the deploy uses the repo's pinned env (ansible-core plus
    # the community.docker deps requests/docker) — the same toolchain the operator
    # uses. --frozen: install from the committed uv.lock, never mutate it on the host.
    run(
        [
            "uv",
            "run",
            "--frozen",
            "ansible-playbook",
            "ansible/deploy.yml",
            "--tags",
            tags,
        ],
        cwd=repo,
    )


def deploy_k8s(
    repo: str, services: set[str], timeout: float, restore_sha: str | None = None
) -> None:
    """Deploy k8s services by tag. The rollout gate lives INSIDE the role.

    No health-poll phase here on purpose: the play already runs apply (roles/k8s/manifests)
    -> `rollout status --timeout` (roles/k8s/rollout-drain) -> a post-Available soak
    (post_tasks/k8s_stabilise_gate.yml) that hard-fails on a restart-count delta or a
    readiness shortfall. Polling again would duplicate it, and containers_for() — the Docker
    gate's input — returns [] for a k8s service, which is exactly the 2026-08-08 configarr
    false-rollback.

    The wait and the soak moved out of roles/k8s/manifests in 5eea64e6, when rollouts were
    batched and the stabilisation window deferred to end-of-play; the sequence above is
    unchanged. assert_stable.yml is gone entirely as of 2026-08-22: claude-otel was its last
    caller, and it now hands its six telemetry workloads to the same end-of-play gate as
    everything else rather than running a second 60s window of its own.

    restore_sha, when given, is passed to the play as the `k8s_restore_snapshot_sha` extra-var,
    which roles/k8s/manifests reads to revert each service's claimed volumes to the snapshot
    named for that SHA before re-applying. Omitted (the ordinary deploy) or blank, the extra-var
    is never added — the call is byte-identical to before this argument existed.
    """
    tags = ",".join(sorted(services))
    log(f"deploying k8s services: {tags} (timeout {timeout:.0f}s)")
    argv = [
        "uv",
        "run",
        "--frozen",
        "ansible-playbook",
        "ansible/deploy.yml",
        "--tags",
        tags,
    ]
    if restore_sha is not None and restore_sha.strip():
        argv += ["-e", f"k8s_restore_snapshot_sha={restore_sha}"]
    run(argv, cwd=repo, timeout=timeout)


def deploy_broad(repo: str, playbook: str, tags: list[str], timeout: float) -> None:
    """Run a broad-plane playbook, bounded. Raises on failure or timeout.

    `uv run --frozen` for the same reason deploy() uses it: the repo's pinned env, and never
    mutating uv.lock on the host.

    No tags means the whole playbook — only ever reached for the deploy plane, where
    `ansible/deploy.yml` unscoped IS the remediation. The setup plane never lands here
    unscoped: setup_tags_for returning an empty set routes to the defer-and-alert arm
    instead, because an unscoped initial_setup.yml is a whole-host reprovision.
    """
    cmd = ["uv", "run", "--frozen", "ansible-playbook", playbook]
    if tags:
        cmd += ["--tags", ",".join(tags)]
    run(cmd, cwd=repo, timeout=timeout)


def emit_deploy_annotation(services: set[str], sha: str) -> None:
    """Record a successful auto-deploy where Grafana can draw it as a dashboard annotation.

    A LOG LINE, not a POST to Grafana's /api/annotations, and the peer of the identically-named
    function in scripts/deploy.sh — the two deploy paths must annotate the same way or the
    dashboards show only half the deploys. Grafana has no hostPort and no pinned ClusterIP, and
    this runs on the HOST, so calling in would mean pinning a fourth address or routing through
    Traefik with a standing write credential. Neither is needed: the Alloy shipper already tails
    /var/log/syslog into loki-homelab, and Grafana already reads that Loki by Service DNS.

    Only the k8s auto-deploy path calls this. The Docker branch is unreachable on both cluster
    nodes (neither has had Docker since 2026-08-14), so wiring it there would be dead code
    rather than symmetry.

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
