# ansible/roles/setup/gitops_deploy/files/deploy_io.py
"""Every boundary the deployer crosses: subprocess, docker, the state directory, its config.

`deploy_logic.py` and the `deploy_*` modules behind it hold the decisions, which are pure.
This module holds their I/O counterparts, which until now sat in `gitops_deploy.py` beside
`main()` — `health_ok` next to `deploy_health`, `consult_staging`'s subprocess half next to
`deploy_staging`, `deploy_k8s()` next to `deploy_k8s.py`. Splitting them out is what lets
`gitops_deploy.main()` be a sequence of named phases a test can drive one at a time.

Two rules hold this module's shape:

- **Nothing here reads a module-level constant of `gitops_deploy`.** Every function takes what
  it needs — `repo`, `hostname`, a timeout — as an argument, so a test can call it directly and
  so `gitops_deploy` stays the one place the deployer's configuration is bound.
- **Callers reach these functions qualified (`deploy_io.run(...)`), never by from-import.**
  A from-import takes a reference at import time and never sees a `monkeypatch` on this module.
  ENFORCED by `ansible/tests/deploy/test_gitops_deploy_patch_boundary.py`.

Stdlib only: the unit runs under `uv run --no-project` and the host is still on Python 3.12.
"""

import json
import os
import pathlib
import re
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import ClassVar

from deploy_health import HealthSample, containers_to_gate, health_decision
from deploy_inventory import declared_services, stale_rendered_services
from deploy_k8s import k8s_role_paths
from deploy_staging import staging_tick_outcome
from host_lib import atomic_write, parse_env_file


def log(msg: str) -> None:
    print(msg, flush=True)


# ── configuration ─────────────────────────────────────────────────────────────────────────────


class ConfigError(Exception):
    """The deployer's config file holds a value it cannot be run with.

    Raised by `Config.validate()`, never by parsing — see `load_config` for why the two are
    separate.
    """


def csv_set(raw: str) -> frozenset[str]:
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


@dataclass(frozen=True)
class Config:
    """One tick's configuration, as read from /etc/gitops-deploy/config.env.

    Frozen: nothing rebinds a value mid-tick, and the CI gate and the k8s denylist both
    disarm by deriving a NEW value rather than by writing one back.

    Attributes:
        errors: one message per key whose value could not be parsed. Empty on a good config.
            Parsing records them instead of raising so that IMPORTING the deployer cannot fail
            — `validate()` is where a bad config becomes a reportable error.
    """

    repo: str = ""
    branch: str = "master"
    hostname: str = "unknown-host"
    discord_webhook: str = ""
    health_timeout_s: int = 300
    run_budget_s: int = 1020
    require_ci: bool = False
    ci_contexts: frozenset[str] = frozenset()
    ci_repo: str = ""
    k8s_autodeploy_enabled: bool = False
    k8s_autodeploy_pilot: frozenset[str] = frozenset()
    k8s_autodeploy_denylist: frozenset[str] = frozenset()
    k8s_autodeploy_max_per_tick: int = 3
    k8s_autodeploy_max_claim_services_per_tick: int = 1
    k8s_deploy_timeout_s: int = 900
    k8s_rollback_timeout_s: int = 1320
    broad_deploy_timeout_s: int = 1800
    staging_gate: bool = False
    staging_gate_blocking: bool = False
    staging_gate_timeout_s: int = 600
    staging_expect_timeout_s: int = 120
    # STAGING_SUBSET is deliberately NOT here. Its literal fallback is parsed out of
    # gitops_deploy.py by scripts/docs/gen_doc_fragments.py, which looks for a
    # `C.get("<KEY>", "<literal>")` call in that file by name — so moving it here would leave
    # the published fragment with no source. It stays a module constant there. The two staging
    # timeouts above used to live there too; gitops_deploy.py keeps a vestigial `C.get(...)`
    # call for each purely so the generator still has one to read — see the comment there.
    errors: tuple[str, ...] = field(default=())

    def validate(self) -> None:
        """Raise `ConfigError` naming every unparseable key, or return.

        Args are none; the errors were collected at parse time.

        Raises:
            ConfigError: at least one key held a value this deployer cannot use. One line
                naming every offending key and what it held — a tick that dies on its config
                has to say which key, and it used to die during import with a bare
                `ValueError: invalid literal for int()` and no key name in it.
        """
        if self.errors:
            raise ConfigError("unusable deployer config: " + "; ".join(self.errors))


def read_config_file(path: str) -> dict[str, str]:
    """The KEY=VALUE pairs in `path`, or {} when the file is absent.

    A missing file used to crash the import, which paged through the unit's OnFailure. It now
    leaves every value at its default and `repo` empty, and `main()` refuses to tick on an
    empty repo: the same page, raised from a function a test can call.
    """
    try:
        return parse_env_file(path)
    except FileNotFoundError:
        return {}


def load_config(env: Mapping[str, str]) -> Config:
    """Parse `env` into a `Config`. Never raises.

    A malformed numeric value is recorded in `Config.errors` and the field keeps its default,
    so importing the deployer cannot fail on a half-written config.env — the failure surfaces
    from `Config.validate()` inside `main()`, where it can be logged and posted rather than
    printed as an import traceback before the heartbeat exists.

    `require_ci` also disarms itself here, loudly, when `REQUIRE_CI=true` but `CI_CONTEXTS` or
    `GITHUB_REPO` is empty (a half-rendered config.env) — done in this one place so `Config` is
    never split from the value a caller reads off it.
    """
    errors: list[str] = []

    def _int(key: str, default: int) -> int:
        raw = env.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            errors.append(f"{key}={raw!r} is not a whole number")
            return default

    def _bool(key: str, default: bool = False) -> bool:
        return env.get(key, str(default).lower()).lower() == "true"

    require_ci = _bool("REQUIRE_CI")
    ci_contexts = csv_set(env.get("CI_CONTEXTS", ""))
    ci_repo = env.get("GITHUB_REPO", "")
    if require_ci and not (ci_contexts and ci_repo):
        # Fail closed the same way the k8s denylist does, but in the opposite direction: an
        # empty context list would make ci_verdict() return `pass` for everything, turning a
        # half-rendered config.env into a silently ungated deployer. Better to disarm loudly.
        log(
            "REQUIRE_CI is set but CI_CONTEXTS/GITHUB_REPO is empty — disabling the CI gate"
        )
        require_ci = False

    return Config(
        repo=env.get("REPO_DIR", ""),
        branch=env.get("BRANCH", "master"),
        hostname=env.get("HOSTNAME", "unknown-host"),
        discord_webhook=env.get("DISCORD_WEBHOOK", ""),
        health_timeout_s=_int("HEALTH_TIMEOUT_S", 300),
        run_budget_s=_int("RUN_BUDGET_S", 1020),
        require_ci=require_ci,
        ci_contexts=ci_contexts,
        ci_repo=ci_repo,
        k8s_autodeploy_enabled=_bool("K8S_AUTODEPLOY_ENABLED"),
        k8s_autodeploy_pilot=csv_set(env.get("K8S_AUTODEPLOY_PILOT", "")),
        k8s_autodeploy_denylist=csv_set(env.get("K8S_AUTODEPLOY_DENYLIST", "")),
        k8s_autodeploy_max_per_tick=_int("K8S_AUTODEPLOY_MAX_PER_TICK", 3),
        k8s_autodeploy_max_claim_services_per_tick=_int(
            "K8S_AUTODEPLOY_MAX_CLAIM_SERVICES_PER_TICK", 1
        ),
        k8s_deploy_timeout_s=_int("K8S_DEPLOY_TIMEOUT_S", 900),
        k8s_rollback_timeout_s=_int("K8S_ROLLBACK_TIMEOUT_S", 1320),
        broad_deploy_timeout_s=_int("BROAD_DEPLOY_TIMEOUT_S", 1800),
        staging_gate=_bool("STAGING_GATE"),
        staging_gate_blocking=_bool("STAGING_GATE_BLOCKING"),
        staging_gate_timeout_s=_int("STAGING_GATE_TIMEOUT_S", 600),
        staging_expect_timeout_s=_int("STAGING_EXPECT_TIMEOUT_S", 120),
        errors=tuple(errors),
    )


# ── the state directory ───────────────────────────────────────────────────────────────────────

STATE_DIR = "/var/lib/gitops-deploy"


class DeployerState:
    """The marker files under /var/lib/gitops-deploy, as one object with typed accessors.

    Fifteen files recorded what this host believes — the held SHA, the plane that failed, how
    long it has been behind origin, and one dedupe marker per alert channel — through fifteen
    module constants and a pair of bare `_read_marker`/`_write_marker` helpers, so nothing
    described the state as a whole. This is that description. The paths, the file contents and
    the empty-vs-missing semantics are unchanged; `gitops_deploy.py` still holds the literal
    constants because the tick ledger's Ansible default is pinned against one of them and the
    test suite repoints the rest, and `tests/test_deployer_state.py` asserts the two agree.

    Attributes:
        directory: where the markers live. `/var/lib/gitops-deploy` on a host; a tmp_path
            under test.
    """

    # Attribute name -> basename on disk. Every entry is a file `_read_marker` used to read.
    MARKERS: ClassVar[dict[str, str]] = {
        "hold": "hold_sha",
        "hold_plane": "hold_plane",
        "last_run": "last_run",
        "diverged": "diverged_sha",
        "behind": "behind_since",
        "stale_composes": "stale_composes_alerted",
        "broad_alerted": "broad_alerted_sha",
        "secrets_alerted": "secrets_alerted_sha",
        "tasks_alerted": "tasks_alerted_sha",
        "meta_alerted": "meta_alerted_sha",
        "k8s_alerted": "k8s_alerted_sha",
        "stale_denylist_alerted": "stale_denylist_alerted_sha",
        "ci_alerted": "ci_alerted_sha",
        "staging_alerted": "staging_alerted_sha",
        "dirty_alerted": "dirty_alerted_date",
    }

    def __init__(self, directory: str | pathlib.Path = STATE_DIR) -> None:
        self.directory = str(directory)

    def path(self, marker: str) -> str:
        """The absolute path of one marker.

        Raises:
            KeyError: `marker` is not one of `MARKERS` — a typo is a mistake, not a new file.
        """
        return os.path.join(self.directory, self.MARKERS[marker])

    def read(self, marker: str) -> str | None:
        """The marker's stripped contents, or None when it is absent or empty.

        Absent and empty deliberately read the same: a marker is armed by holding a SHA and
        disarmed by being removed, and a torn write that left a zero-length file must read as
        disarmed rather than as a SHA of "".

        Raises:
            OSError: the file exists but could not be read — an unreadable state directory
                (a wrong mode, a failed mount) is NOT "no hold". Swallowing it here is how a
                held host reports converged, so it propagates and the tick pages.
        """
        try:
            with open(self.path(marker)) as fh:
                return fh.read().strip() or None
        except FileNotFoundError:
            return None

    def write(self, marker: str, value: str | None) -> None:
        """Set the marker to `value`, or remove it when `value` is None.

        The write is atomic (temp + rename, see `host_lib.atomic_write`): a torn marker is a
        hold that reads as cleared.
        """
        if value is None:
            try:
                os.remove(self.path(marker))
            except FileNotFoundError:
                pass
        else:
            atomic_write(self.path(marker), value)

    # The four markers with a reader outside this deployer (monitor-bridge reads three of them
    # off the same mount) get a named property; the per-channel dedupe markers are reached
    # through read()/write() by the alert code that owns them.
    @property
    def hold_sha(self) -> str | None:
        """The commit this host refuses to redeploy, or None."""
        return self.read("hold")

    @property
    def hold_plane(self) -> str | None:
        """The playbook (and tags) whose broad apply failed, or None."""
        return self.read("hold_plane")

    @property
    def diverged_sha(self) -> str | None:
        """The origin SHA recorded while local and origin have diverged, or None."""
        return self.read("diverged")

    @property
    def behind_since(self) -> str | None:
        """`"<origin_sha> <unix_ts_first_seen>"` while behind origin, or None."""
        return self.read("behind")


# ── bounding a failed run's output ────────────────────────────────────────────────────────────

# ansible-playbook writes the failing TASK header, the `fatal:` line carrying its msg and the
# PLAY RECAP to STDOUT. stderr carries only warnings and deprecation notices, so an error string
# built from stderr alone says nothing about what broke — the 2026-09-02 broad apply that failed
# on `2d25ced3` left a deprecation warning's origin as the only surviving detail, and the broad
# arm is forward-only, so the operator had nothing to fix forward from. Both halves are bounded
# because a failed run can print megabytes into the journal.
#
# The stdout half is NOT a plain tail. The profile_tasks callback prints its timing table after
# the PLAY RECAP, and on a 1950-task deploy.yml that table plus the recap is more than the whole
# budget, so a positional tail held `ok=1950 failed=1` and nothing naming the task (issue #907,
# the 21:26 failure on `55c33965`). _failure_detail lifts the failing task's own lines out first
# and spends what is left of the budget on the tail.
RUN_ERROR_STDOUT_CHARS = 4000
RUN_ERROR_STDERR_TAIL = 4000

# The default stdout callback opens every section with one of these, and profile_tasks adds
# TASKS RECAP. A failure inside a section is a `fatal: [host]: ...` line (FAILED! or
# UNREACHABLE!) or a per-item `failed: [host] (item=...)` line; ignore_errors prints
# `...ignoring` on the line after each one.
_SECTION_HEADER = re.compile(
    r"^(TASK|RUNNING HANDLER|PLAY|PLAY RECAP|NO MORE HOSTS LEFT|TASKS RECAP)\b"
)
_TASK_HEADER = re.compile(r"^(TASK|RUNNING HANDLER) \[")
_FAILURE_LINE = re.compile(r"^(fatal|failed): \[")
TRUNCATED = "[...truncated...]"


def tail(text: str, limit: int) -> str:
    """Return at most the last ``limit`` characters of ``text``, cut at a line boundary.

    The right slice for stderr and for stdout with no `fatal:` line in it: what ansible prints
    last is the diagnostic part. It is the wrong slice once profile_tasks pads the end, which
    is why failure_detail exists.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    _, newline, rest = cut.partition("\n")
    return f"{TRUNCATED}\n" + (rest if newline else cut)


def head(text: str, limit: int) -> str:
    """Return at most the first ``limit`` characters of ``text``, cut at a line boundary."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    kept, newline, _ = cut.rpartition("\n")
    return (kept if newline else cut) + f"\n{TRUNCATED}"


def failing_task(text: str) -> tuple[str, str] | None:
    """Find the last task in ansible stdout whose failure was not ignored.

    Returns:
        The task's lines and everything printed after them, or None when no section of
        ``text`` carries an un-ignored `fatal:`/`failed:` line. The task's lines are its
        header plus its output from the first failure line to the end of the section: a loop
        task prints one line per ok item before the one that failed, and those are padding.
        A failure a `rescue:` block caught is still a candidate — nothing marks it as
        rescued at the point it prints — but a later un-ignored failure wins over it.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if _SECTION_HEADER.match(line)]
    for n in range(len(starts) - 1, -1, -1):
        start = starts[n]
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        if not _TASK_HEADER.match(lines[start]):
            continue
        section = lines[start:end]
        failed_at = next(
            (i for i, line in enumerate(section) if _FAILURE_LINE.match(line)), None
        )
        if failed_at is None or any(line.startswith("...ignoring") for line in section):
            continue
        task = "\n".join([section[0], *section[failed_at:]]).strip()
        return task, "\n".join(lines[end:])
    return None


def failure_detail(stdout: str, limit: int) -> str:
    """Bound ansible's stdout to ``limit`` characters, keeping the failing task's lines.

    The failing task comes first and gets the budget first; the tail of what follows it gets
    the remainder, minus profile_tasks' timing table, which is the padding that evicted the
    task in the first place and diagnoses nothing. Stdout with no failing task in it is a
    plain tail, as before.
    """
    found = failing_task(stdout)
    if found is None:
        return tail(stdout, limit)
    task, rest = found
    rest, _, _ = rest.partition("\nTASKS RECAP")
    task = head(task, limit)
    rest = tail(rest, limit - len(task))
    return f"{task}\n{rest}" if rest else task


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
    ledger: str, tz: tzinfo, now, sha: str, gated: set[str], verdict: str
) -> None:
    """Append this tick's verdict to the tick ledger. Never raises.

    Args:
        ledger: the JSONL file to append to.
        tz: the timezone the `at` stamp is rendered in (America/Chicago on the host).
        now: the clock, as a callable taking a tzinfo — `datetime.datetime.now` in production.
        sha: the commit the gate was asked about.
        gated: the services it was asked about, which is the promoted set, not the changed set.
        verdict: one of `staging_verdict`'s words, or STAGING_SKIPPED.

    A tick that measured nothing writes nothing — `staging_tick_outcome` returns None for
    SKIPPED, and the tick runs every ten minutes, so recording those would bury the real samples.

    Every failure is swallowed. This runs inside the staging consultation, which may not break a
    prod deploy for any reason; a full disk or a bad permission on the ledger must cost the
    measurement, never the deploy.
    """
    outcome = staging_tick_outcome(verdict)
    if outcome is None:
        return
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
