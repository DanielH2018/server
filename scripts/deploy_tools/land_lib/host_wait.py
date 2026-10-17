"""The landing's wait on the hosts a merge reaches (H3, *Steps*, H3.9a). Nothing calls it yet.

H3.9b wires it into `pipeline.py` and arms the Pi's timer. Until then no landing runs any of
this, so no landing changes. The plan is docs/target-architecture/host/h3-pi-pilot.md; the
contract is C6's *Hosts* bullet in docs/target-architecture/contracts.md.

`host_targets` names the hosts whose package the merge changes. It renders every host package
at the merge commit and at its first parent, each in a snapshot from `tools.gate_snapshot`,
and keeps the hosts whose trees differ. When a snapshot or a render fails, every host with a
`hosts/<host>/package.yml` at the merge commit is a target, so a failure waits rather than
skips.

`wait_for_hosts` then reads Prometheus until each target settles:

| What it reads | How the landing ends |
|---|---|
| every target's applied SHA is the merge or descends from it, with reconcile at 1 | returns |
| a target's `homelab_host_reconcile_ok` at 0 | `deploy-failed`, exit 1 |
| a target's first-parent package ships no enable link for the updater's timer | `deferred`, exit 75 |
| a target's deadline is more than 45 minutes away | `deferred`, exit 75 |
| a target is still unsettled past its deadline | `deferred`, exit 75 |

A target's deadline is the second updater fire after the merge plus 10 minutes, read from the
first parent's rendered schedule. The first fire can precede the builder's publish.

Typical usage example (H3.9b's, after master CI is green on the merge commit):

    targets = host_targets(ln.opts.primary, merge_sha)
    if targets:
        wait_for_hosts(ln, targets, merge_sha, merged_at)
"""

import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Verdict, say
from deploy_tools.land_lib.tools import gate_snapshot
from lib.git import git as run_git
from lib.repo_paths import ANSIBLE, REPO
from lib.systemd_timer import DAY_S, calendar_fires, span_s, timer_settings

# The renderer's host mode, run from a gate snapshot's root. A daniel-pi render takes about
# 2s; the bound stops a wedged one, which `host_targets` reads as a failed render.
HOST_RENDER_ARGV = ("uv", "run", "python", "scripts/renderer/homelab_render.py", "host")
HOST_RENDER_TIMEOUT = 300
# One rendered host package: each path under `hosts/<host>/` mapped to its kind (`file` or
# `link`), its mode, and its bytes or its link target.
HostTree = dict[str, tuple[str, int, bytes]]

PROMETHEUS_PORT = 9090
LOOPBACK = "127.0.0.1"
# observability's role defaults pin Prometheus's ClusterIP in plaintext. The address is read
# from there, the way `probe_lib/core.py:observability_loki_ip` reads Loki's, and never through
# `probe_lib/core.py:prom_endpoint`, which decrypts.
OBSERVABILITY_DEFAULTS = (
    ANSIBLE / "roles" / "k8s" / "observability" / "defaults" / "main.yml"
)
CLUSTER_IP_KEY = "observability_prometheus_cluster_ip"
QUERY_TIMEOUT_S = 10
UNREACHABLE = "Prometheus unreachable on loopback and ClusterIP"

APPLIED = "homelab_host_applied_info"
RECONCILE_OK = "homelab_host_reconcile_ok"
LAST_SUCCESS = "homelab_host_update_success_timestamp_seconds"

UPDATER_TIMER = "homelab-updater.timer"
UNIT_ROOT = PurePosixPath("root/usr/lib/systemd/system")
DROPIN = UNIT_ROOT / f"{UPDATER_TIMER}.d" / "50-homelab.conf"
# The shared timer, copied byte for byte into homelab-updater. A host drop-in overrides it.
SHARED_TIMER = Path("hosts/packages/homelab-updater/units") / UPDATER_TIMER

# DECIDED: the schedule is read in UTC. OnCalendar fires on the host's local clock, and every
# host runs Etc/UTC (`weekly_reboot_timezone` in group_vars/all.yml records the same fact for
# the reboot cron).
DEADLINE_SLACK_S = 10 * 60
MAX_WAIT_S = 45 * 60
POLL_S = 30
# Triggers that fire once after boot, so they add no fire to a host that is up.
BOOT_ONLY = frozenset({"OnBootSec", "OnStartupSec"})

START_UPDATER = "sudo systemctl start homelab-updater.service"
CLEAR_HOLD = "sudo /usr/lib/homelab/homelab-updater --clear-hold"


@dataclass(frozen=True)
class HostTarget:
    """A host whose package the merge changes, and what its first parent's package arms.

    Attributes:
        host: The inventory host name, as `homelab-updater` labels its series.
        armed: Whether the first parent's package ships the enable link for the updater's
            timer. None when the render that would say failed.
        triggers: The timer's triggers with the host's drop-in applied, as
            `systemd_timer.timer_settings` returns them. None when unknown.
        delay: The timer's `RandomizedDelaySec`, in seconds.
    """

    host: str
    armed: bool | None = None
    triggers: dict[str, list[str]] | None = None
    delay: int = 0


def render_host_tree(snapshot: Path, host: str) -> HostTree | None:
    """`host`'s package as the renderer in `snapshot` renders it; None when the render failed.

    The renderer runs from the snapshot, never imported here, so the inventory, the templates
    and the renderer code are all the snapshot's commit's. `gate_snapshot` points
    `UV_PROJECT_ENVIRONMENT` at the primary's `.venv` for the life of the snapshot.
    """
    with tempfile.TemporaryDirectory(prefix="land-host-render-") as out:
        try:
            done = subprocess.run(
                [*HOST_RENDER_ARGV, "--host", host, "--out", out],
                cwd=snapshot,
                capture_output=True,
                text=True,
                timeout=HOST_RENDER_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            say(f"host render of {host} at {snapshot} did not finish: {exc}")
            return None
        if done.returncode != 0:
            last = (done.stderr.strip().splitlines() or ["no output"])[-1]
            say(f"host render of {host} at {snapshot} failed: {last}")
            return None
        root = Path(out) / "hosts" / host
        tree: HostTree = {}
        for path in sorted(root.rglob("*")):
            key = path.relative_to(root).as_posix()
            if path.is_symlink():
                tree[key] = ("link", 0, os.fsencode(os.readlink(path)))
            elif path.is_file():
                tree[key] = ("file", path.stat().st_mode & 0o7777, path.read_bytes())
        return tree


def fetch_url(url: str, timeout: float) -> bytes:
    """The body of an HTTP GET of `url`."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


@dataclass(frozen=True)
class HostReads:
    """Every boundary the host wait crosses, so a test replaces one field."""

    snapshot: Callable[[Path, str], AbstractContextManager[Path | None]] = gate_snapshot
    render: Callable[[Path, str], HostTree | None] = render_host_tree
    git: Callable[..., Any] = run_git
    fetch: Callable[[str, float], bytes] = fetch_url
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    defaults: Path = field(default=OBSERVABILITY_DEFAULTS)


REAL_READS = HostReads()


# -- host_targets ------------------------------------------------------------------------


def host_targets(
    primary: Path, merge: str, reads: HostReads = REAL_READS
) -> list[HostTarget]:
    """The hosts whose rendered package differs between `merge` and its first parent.

    A host with no package at the merge commit is never a target: nothing would apply. A host
    whose package the merge adds is one, with nothing armed at the first parent.
    """
    parent = reads.git("rev-parse", "--verify", f"{merge}^1", cwd=primary, check=False)
    if parent.returncode != 0:
        say(f"host targets: no first parent of {merge[:12]}; waiting on every host")
        return _every_host(primary, merge, reads)
    first_parent = parent.stdout.strip()
    with (
        reads.snapshot(primary, merge) as at_merge,
        reads.snapshot(primary, first_parent) as at_parent,
    ):
        if at_merge is None or at_parent is None:
            say("host targets: a snapshot failed; waiting on every host")
            return _every_host(primary, merge, reads)
        before = _hosts_in(at_parent)
        targets = []
        for host in sorted(_hosts_in(at_merge)):
            new = reads.render(at_merge, host)
            old = reads.render(at_parent, host) if host in before else {}
            if new is None or old is None:
                say(f"host targets: the render of {host} failed; waiting on every host")
                return _every_host(primary, merge, reads)
            if new != old:
                targets.append(_target(host, old, at_parent))
        return targets


def _hosts_in(root: Path) -> set[str]:
    return {spec.parent.name for spec in (root / "hosts").glob("*/package.yml")}


def _every_host(primary: Path, merge: str, reads: HostReads) -> list[HostTarget]:
    """Every host with a `hosts/<host>/package.yml` at `merge`, each with its schedule unknown."""
    listing = reads.git(
        "ls-tree", "--name-only", merge, "hosts/", cwd=primary, check=False
    )
    if listing.returncode == 0:
        names = [PurePosixPath(p).name for p in listing.stdout.split()]
        hosts = {
            name
            for name in names
            if reads.git(
                "cat-file",
                "-e",
                f"{merge}:hosts/{name}/package.yml",
                cwd=primary,
                check=False,
            ).returncode
            == 0
        }
    else:
        # The merge commit cannot be read, so this checkout's own tree is the nearest answer.
        hosts = _hosts_in(REPO)
    return [HostTarget(host) for host in sorted(hosts)]


def _target(host: str, old: HostTree, at_parent: Path) -> HostTarget:
    """`host` as a target, armed and scheduled the way its first-parent package is.

    DECIDED: both the enable link and the schedule come from the first parent's render, never
    the merge commit's (H3.9a item 5). Before the builder publishes this merge, a host holds at
    best the first parent's package, so a link or a drop-in that arrives in this merge changes
    nothing until an updater run installs it.
    """
    armed = any(
        kind == "link"
        and PurePosixPath(key).name == UPDATER_TIMER
        and PurePosixPath(key).parent.name.endswith(".wants")
        for key, (kind, _, _) in old.items()
    )
    shared = at_parent / SHARED_TIMER
    if not old or not shared.is_file():
        return HostTarget(host, armed=armed)
    dropin = old.get(str(DROPIN))
    dropins = [dropin[2].decode()] if dropin else []
    try:
        triggers, delay = timer_settings(shared.read_text(), dropins)
    except ValueError:
        return HostTarget(host, armed=armed)
    return HostTarget(host, armed=armed, triggers=triggers, delay=delay)


# -- the schedule ------------------------------------------------------------------------


def calendar_after(expressions: list[str], after: float, count: int) -> list[float]:
    """The first `count` fires after `after` of daily `OnCalendar=` expressions, read in UTC."""
    daily = sorted({t for e in expressions for t in calendar_fires(e)})
    if not daily:
        return []
    day = after - after % DAY_S
    fires: list[float] = []
    while len(fires) < count:
        fires.extend(day + t for t in daily if day + t > after)
        day += DAY_S
    return fires[:count]


def deadline(target: HostTarget, merged_at: float) -> float | None:
    """When `target` should have applied the merge: its second updater fire after, plus slack.

    With several triggers, each bounds the second fire on its own, so the nearest bound wins.

    Returns:
        The deadline as a Unix time, or None when the schedule is unknown or unreadable.
    """
    if not target.triggers:
        return None
    bounds = []
    try:
        for key, values in target.triggers.items():
            if key == "OnCalendar":
                bounds.append(calendar_after(values, merged_at, 2)[1])
            elif key == "OnUnitActiveSec":
                bounds.append(merged_at + 2 * min(span_s(v) for v in values))
            elif key not in BOOT_ONLY:
                return None
    except ValueError, IndexError:
        return None
    if not bounds:
        return None
    return min(bounds) + target.delay + DEADLINE_SLACK_S


def next_fire(target: HostTarget, now: float) -> float | None:
    """The target's next calendar fire after `now`, or None when it has none this can name."""
    try:
        fires = calendar_after((target.triggers or {}).get("OnCalendar", []), now, 1)
    except ValueError:
        return None
    return fires[0] if fires else None


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


# -- the transport -----------------------------------------------------------------------


class PrometheusUnreachable(Exception):
    """Neither the loopback port nor the ClusterIP answered a query."""


def prometheus_cluster_ip(defaults: Path = OBSERVABILITY_DEFAULTS) -> str | None:
    """Prometheus's ClusterIP from observability's role defaults, or None when absent."""
    try:
        lines = defaults.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith(f"{CLUSTER_IP_KEY}:"):
            return line.split(":", 1)[1].strip() or None
    return None


def _refused_or_timed_out(exc: OSError) -> bool:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, (ConnectionRefusedError, TimeoutError, socket.timeout))


def query(promql: str, reads: HostReads) -> list[dict]:
    """The instant-vector result of `promql`: loopback first, then the ClusterIP.

    The ClusterIP is asked only when the loopback port refuses or times out.

    Raises:
        PrometheusUnreachable: Both refused or timed out, or the ClusterIP is not pinned.
        OSError: The answering Prometheus failed some other way.
        ValueError: The answer is not a Prometheus success.
    """
    params = urllib.parse.urlencode({"query": promql})
    cluster_ip = prometheus_cluster_ip(reads.defaults)
    for address in (LOOPBACK, cluster_ip):
        if address is None:
            continue
        url = f"http://{address}:{PROMETHEUS_PORT}/api/v1/query?{params}"
        try:
            body = reads.fetch(url, QUERY_TIMEOUT_S)
        except OSError as exc:
            if _refused_or_timed_out(exc):
                continue
            raise
        answer = json.loads(body)
        if not isinstance(answer, dict) or answer.get("status") != "success":
            raise ValueError(f"Prometheus did not answer {promql!r} with success")
        return answer["data"]["result"]
    raise PrometheusUnreachable(UNREACHABLE)


# -- the settle rule ---------------------------------------------------------------------


@dataclass
class HostState:
    """What Prometheus reports for one host.

    Attributes:
        applied: The `sha` label of each `homelab_host_applied_info` series, 12 hex digits.
        reconcile_ok: `homelab_host_reconcile_ok`, or None when the series is absent.
        last_success: `homelab_host_update_success_timestamp_seconds`, or None when absent.
    """

    applied: list[str] = field(default_factory=list)
    reconcile_ok: float | None = None
    last_success: float | None = None


def read_states(hosts: list[str], reads: HostReads) -> dict[str, HostState]:
    """Each host's state, from three queries. A host Prometheus reports nothing for is absent."""
    selector = '{host=~"%s"}' % "|".join(hosts)
    states: dict[str, HostState] = {}
    for sample in query(APPLIED + selector, reads):
        labels = sample["metric"]
        states.setdefault(labels["host"], HostState()).applied.append(
            labels.get("sha", "")
        )
    for sample in query(RECONCILE_OK + selector, reads):
        state = states.setdefault(sample["metric"]["host"], HostState())
        state.reconcile_ok = float(sample["value"][1])
    for sample in query(LAST_SUCCESS + selector, reads):
        state = states.setdefault(sample["metric"]["host"], HostState())
        state.last_success = float(sample["value"][1])
    return states


def covers(applied: str, merge: str, primary: Path, reads: HostReads) -> bool:
    """Whether the applied commit `applied`, a SHA prefix, is `merge` or descends from it.

    A commit the primary checkout does not know is not a descendant: the host is not settled.
    """
    if len(applied) < 7:
        return False
    if merge.startswith(applied):
        return True
    return (
        reads.git(
            "merge-base", "--is-ancestor", merge, applied, cwd=primary, check=False
        ).returncode
        == 0
    )


def settled(
    state: HostState | None, merge: str, primary: Path, reads: HostReads
) -> bool:
    """Whether a host has applied `merge` or a descendant, with reconcile at 1.

    An absent series is not settled.
    """
    if state is None or state.reconcile_ok != 1:
        return False
    return any(covers(sha, merge, primary, reads) for sha in state.applied)


# -- wait_for_hosts ----------------------------------------------------------------------


def wait_for_hosts(
    ln: Landing,
    targets: list[HostTarget],
    merge: str,
    merged_at: float,
    reads: HostReads = REAL_READS,
) -> None:
    """Return once every target has settled on `merge`; otherwise end the landing.

    It queries once before anything else, so a target that already settled never waits.

    Args:
        ln: The landing, which `finish` ends.
        targets: What `host_targets` returned.
        merge: The merge commit's full SHA.
        merged_at: When the PR merged, as a Unix time. The deadline counts fires after it.
        reads: The boundaries.

    Raises:
        Outcome: Through `ln.finish`: `deploy-failed` for a held host, and `deferred` for a
            target the landing cannot wait for, or one still unsettled past its deadline.
    """
    pr = ln.opts.pr
    primary = ln.opts.primary
    head = f"PR #{pr}, {merge[:12]}"
    pending = list(targets)
    deadlines: dict[str, float] = {}
    first = True
    while True:
        states = _poll(pending, reads)
        if states is not None:
            held = [
                t.host
                for t in pending
                if (states.get(t.host) or HostState()).reconcile_ok == 0
            ]
            if held:
                ln.finish(
                    Verdict.DEPLOY_FAILED,
                    1,
                    f"{head} — reconcile failed on {', '.join(held)}, which holds every "
                    f"update. Fix the cause, then run `{CLEAR_HOLD}` on each held host",
                )
            pending = [
                t
                for t in pending
                if not settled(states.get(t.host), merge, primary, reads)
            ]
        if not pending:
            say(f"hosts applied: {', '.join(t.host for t in targets)}")
            return
        now = reads.clock()
        if first:
            first = False
            _refuse_unwaitable(ln, head, pending, merged_at, now, deadlines)
        late = [t for t in pending if now > deadlines[t.host]]
        if late:
            ln.finish(
                Verdict.DEFERRED, 75, f"{head} — {_late(late, states, deadlines)}"
            )
        say(f"waiting for hosts to apply: {', '.join(t.host for t in pending)}")
        reads.sleep(POLL_S)


def _poll(pending: list[HostTarget], reads: HostReads) -> dict[str, HostState] | None:
    """The pending hosts' states, or None, printing why, when Prometheus did not answer."""
    try:
        return read_states([t.host for t in pending], reads)
    except PrometheusUnreachable as exc:
        say(f"{exc}; not settled")
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        say(f"Prometheus query failed ({exc}); not settled")
    return None


def _refuse_unwaitable(
    ln: Landing,
    head: str,
    pending: list[HostTarget],
    merged_at: float,
    now: float,
    deadlines: dict[str, float],
) -> None:
    """End the landing `deferred` for a target no wait can see settle; else fill `deadlines`.

    DECIDED: a target whose schedule is unknown, because its render failed or its timer is
    one this cannot read, waits the full 45 minutes rather than being refused, so a failure
    waits rather than skips (H3.9a item 2).
    """
    reasons = []
    for target in pending:
        if target.armed is False:
            reasons.append(
                f"{target.host}'s package at the first parent ships no enable link for "
                f"{UPDATER_TIMER}, so no timer is armed. Start the updater on "
                f"{target.host}: `{START_UPDATER}`"
            )
            continue
        due = deadline(target, merged_at)
        deadlines[target.host] = merged_at + MAX_WAIT_S if due is None else due
        if due is not None and due - now > MAX_WAIT_S:
            fire = next_fire(target, now)
            when = utc(fire) if fire is not None else "an unknown time"
            reasons.append(
                f"{target.host}'s next updater fire is at {when}, and its deadline "
                f"{utc(due)} is more than {MAX_WAIT_S // 60} minutes away. To apply "
                f"sooner, start the updater on {target.host}: `{START_UPDATER}`"
            )
    if reasons:
        ln.finish(Verdict.DEFERRED, 75, f"{head} — {'; '.join(reasons)}")


def _late(
    late: list[HostTarget],
    states: dict[str, HostState] | None,
    deadlines: dict[str, float],
) -> str:
    parts = []
    for target in late:
        state = (states or {}).get(target.host) or HostState()
        success = (
            f"its last update success was at {utc(state.last_success)}"
            if state.last_success is not None
            else f"it reports no {LAST_SUCCESS}"
        )
        parts.append(
            f"{target.host} has not applied it by {utc(deadlines[target.host])}; {success}"
        )
    return "; ".join(parts)
