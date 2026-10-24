#!/usr/bin/env python3
"""The gates that stand between a k3s restart and the cluster: the apply-time checks of H6.

Three callers share this one stdlib-only script, shipped as `/usr/lib/homelab/k3s_gates.py`:

  server  a system-upgrade-controller (SUC) Plan's `prepare` container on daniel-box. It polls
          until every check passes, then exits 0 and the upgrade container runs.
  agent   the agent Plan's `prepare` on daniel-server. It first waits for the server Plan.
  host    `homelab-k3s-restart`, which runs the same storage, node and CoreDNS checks once,
          plus two checks of its own: no SUC run in progress and no Flux promotion in flight.

Every check refuses. A read error, a missing member, an empty list and a malformed document each
read as a refusal, never as a pass (failure class 3, an empty read taken as clean). The only
thing a check treats as clean is a document that parsed and met its rule.

The checks mirror the runbook's own predicates in `scripts/deploy_tools/runbook_gates_lib/`
(`unsafe_volumes`, `in_flight_backups`, `nodes_not_ready`, `held_sha`), which cannot ship to a
host because they import repo code. `hosts/lib/tests/test_k3s_gates_parity.py` feeds both the
same fixtures and fails when they disagree.

Exit codes: 0 every check passed; 1 a check refused (single pass) or refused until `--max-wait`
elapsed; 2 a usage error.

Usage:
    k3s_gates.py server --host-root /host
    k3s_gates.py agent  --host-root /host
    k3s_gates.py host   --server https://127.0.0.1:6443 --token-file F --ca-file F
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

# ── what the cluster is expected to hold ─────────────────────────────────────────────────────
# `lib.kubectl.CLUSTER_NODES["prod"]` is the repo's copy; the parity test holds the two equal.
NODES = ("daniel-box", "daniel-server")
# `longhorn_robustness.SAFE_ROBUSTNESS`, which this script cannot import. An allow-list: a state
# nobody has heard of stops the upgrade. `unknown` is what an idle, detached volume reports.
SAFE_ROBUSTNESS = frozenset({"healthy", "unknown"})
# `k3s_upgrade.SETTLED_BACKUP`. A Backup CR with no state yet is in flight, so it is not here.
SETTLED_BACKUP = frozenset({"Completed", "Error", "Unknown"})

PLAN_NAMESPACE = "system-upgrade"
SERVER_PLAN = "k3s-server"
AGENT_PLAN = "k3s-agent"
CONTROL_PLANE_LABEL = "node-role.kubernetes.io/control-plane"

# A server restart is recent when the controller-manager Lease was acquired under 15 minutes
# ago. A renewTime older than the lease duration plus the slack below is the handover gap.
RESTART_QUIET_S = 15 * 60
LEASE_RENEW_SLACK_S = 30

# ── API paths. H6.1b's Role and ClusterRole grant exactly these reads ────────────────────────
# `coredns` and the Lease are read by name: the Role grants `get` on them by `resourceNames`,
# so a list of either is Forbidden.
_LONGHORN = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
VOLUMES_PATH = f"{_LONGHORN}/volumes"
BACKUPS_PATH = f"{_LONGHORN}/backups"
NODES_PATH = "/api/v1/nodes"
COREDNS_PATH = "/apis/apps/v1/namespaces/kube-system/deployments/coredns"
LEASE_PATH = (
    "/apis/coordination.k8s.io/v1/namespaces/kube-system/leases/kube-controller-manager"
)
PLANS_PATH = f"/apis/upgrade.cattle.io/v1/namespaces/{PLAN_NAMESPACE}/plans"
JOBS_PATH = f"/apis/batch/v1/namespaces/{PLAN_NAMESPACE}/jobs"
KUSTOMIZATIONS_PATH = (
    "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/flux-system/kustomizations"
)
ROOT_KUSTOMIZATION = "homelab"
_SOURCE_PATHS = {
    "OCIRepository": "/apis/source.toolkit.fluxcd.io/v1/namespaces/{ns}/ocirepositories/{name}",
    "GitRepository": "/apis/source.toolkit.fluxcd.io/v1/namespaces/{ns}/gitrepositories/{name}",
}

# ── host files, read under `--host-root` ─────────────────────────────────────────────────────
RESTART_MARKER = "/run/homelab-k3s-restart"
HOLD_DIR = "/var/lib/gitops-deploy"
HOLD_FILE = "hold_sha"  # `gitops_markers.MARKERS["hold"]`, held equal by a test.
# `deploy_locks.lock_path(SERVICE_LOCK_ALL)`, held equal by a test.
ALL_LOCK = "/var/lock/server-deploy-all.lock"
_MAX_SYMLINK_HOPS = 40

_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class ReadError(Exception):
    """A read that did not return a usable JSON object. The caller refuses."""


Getter = Callable[[str], dict]


# ── transport ────────────────────────────────────────────────────────────────────────────────


class Api:
    """GET a path from the API server with a bearer token, 10 seconds a read by default.

    The token is read on every call, because a ServiceAccount token rotates. A missing or empty
    token file, a missing CA file, a non-200 answer, a timeout and a body that is not a JSON
    object are each a `ReadError`. `deadline` is a `time.monotonic()` value that caps the whole
    run: past it every read refuses, and a read near it gets only the time that is left.
    """

    def __init__(
        self,
        server: str,
        token_file: str,
        ca_file: str,
        timeout: float = 10.0,
        deadline: float | None = None,
    ) -> None:
        self.server = server.rstrip("/")
        self.token_file = token_file
        self.ca_file = ca_file
        self.timeout = timeout
        self.deadline = deadline

    def _budget(self) -> float:
        if self.deadline is None:
            return self.timeout
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ReadError("the step deadline passed before this read")
        return min(self.timeout, left)

    def __call__(self, path: str) -> dict:
        timeout = self._budget()
        try:
            token = Path(self.token_file).read_text().strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ReadError(f"cannot read the token file: {exc}") from exc
        if not token:
            raise ReadError("the token file is empty")
        context = None
        if self.server.startswith("https:"):
            try:
                context = ssl.create_default_context(cafile=self.ca_file)
            except (OSError, ssl.SSLError) as exc:
                raise ReadError(f"cannot load the CA file: {exc}") from exc
        request = urllib.request.Request(
            self.server + path,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as r:
                body = r.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ReadError(f"{path}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ReadError(f"{path}: {exc}") from exc
        try:
            doc = json.loads(body)
        except ValueError as exc:
            raise ReadError(f"{path}: the body is not JSON") from exc
        if not isinstance(doc, dict):
            raise ReadError(f"{path}: the body is not a JSON object")
        return doc


# ── cluster checks. Each returns its refusals; an empty list is a pass ───────────────────────


def _items(doc: dict) -> list[dict]:
    items = doc.get("items")
    if not isinstance(items, list):
        raise ReadError("the list has no items array")
    return [i for i in items if isinstance(i, dict)]


def _name(item: dict) -> str:
    return str((item.get("metadata") or {}).get("name", "?"))


def _condition(item: dict, kind: str) -> dict | None:
    conditions = (item.get("status") or {}).get("conditions") or []
    return next((c for c in conditions if c.get("type") == kind), None)


def check_volumes(get: Getter) -> list[str]:
    """Refuse on a volume outside `SAFE_ROBUSTNESS`, and on a list of no volumes at all."""
    items = _items(get(VOLUMES_PATH))
    if not items:
        return ["no Longhorn volume is listed"]
    refusals = []
    for item in items:
        robustness = str((item.get("status") or {}).get("robustness", ""))
        if robustness not in SAFE_ROBUSTNESS:
            refusals.append(
                f"volume {_name(item)} is {robustness or 'reporting no robustness'}"
            )
    return refusals


# DECIDED: an empty Backup list passes. `k3s_upgrade.in_flight_backups` passes it, the parity
# test holds the two equal, and the plan asks for a non-empty list only of volumes and nodes.
# The daily and weekly tiers keep dozens of Backup CRs, so an empty list is not a state a live
# cluster reaches; an API outage is a ReadError, which refuses.
def check_backups(get: Getter) -> list[str]:
    """Refuse on a Backup CR that is not settled, an empty state included."""
    refusals = []
    for item in _items(get(BACKUPS_PATH)):
        state = str((item.get("status") or {}).get("state") or "")
        if state not in SETTLED_BACKUP:
            refusals.append(
                f"backup {_name(item)} is {state or 'in flight (no state)'}"
            )
    return refusals


def check_nodes(get: Getter, expected: tuple[str, ...] = NODES) -> list[str]:
    """Refuse on an expected node that is absent or whose `Ready` condition is not True."""
    seen = {}
    for item in _items(get(NODES_PATH)):
        ready = _condition(item, "Ready")
        seen[_name(item)] = str((ready or {}).get("status", "Unknown"))
    refusals = []
    for name in expected:
        if name not in seen:
            refusals.append(f"node {name} is not in the cluster")
        elif seen[name] != "True":
            refusals.append(f"node {name} is Ready={seen[name]}")
    return refusals


def check_coredns(get: Getter) -> list[str]:
    """Refuse until the CoreDNS rollout finished, not merely while `Available` is True."""
    doc = get(COREDNS_PATH)
    generation = (doc.get("metadata") or {}).get("generation")
    status = doc.get("status") or {}
    fields = {
        k: status.get(k) for k in ("replicas", "updatedReplicas", "availableReplicas")
    }
    missing = [k for k, v in fields.items() if not isinstance(v, int)]
    if not isinstance(generation, int) or not isinstance(
        status.get("observedGeneration"), int
    ):
        missing.append("generation")
    if missing:
        return [f"coredns reports no {', '.join(missing)}"]
    refusals = []
    if status["observedGeneration"] != generation:
        refusals.append("coredns has not observed its latest generation")
    if len(set(fields.values())) != 1:
        refusals.append(f"coredns rollout is unfinished: {fields}")
    elif status["replicas"] < 1:
        refusals.append("coredns has no replica")
    return refusals


def _utc(stamp: object, what: str) -> datetime:
    if not isinstance(stamp, str) or not stamp:
        raise ReadError(f"the Lease has no {what}")
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError as exc:
        raise ReadError(f"the Lease {what} is not a timestamp") from exc
    if parsed.tzinfo is None:
        raise ReadError(f"the Lease {what} has no time zone")
    return parsed


def check_server_restart(get: Getter, now: datetime) -> list[str]:
    """Refuse while the k3s server restarted under 15 minutes ago.

    k3s runs kube-controller-manager with leader election, and each server start takes the
    `kube-system/kube-controller-manager` Lease under a fresh holder identity, which writes a
    new `acquireTime`. The node's `Ready` transition is no signal: a restart never moves it.
    """
    spec = get(LEASE_PATH).get("spec") or {}
    if not spec.get("holderIdentity"):
        return ["the controller-manager Lease has no holderIdentity"]
    duration = spec.get("leaseDurationSeconds")
    if not isinstance(duration, int) or duration <= 0:
        return ["the controller-manager Lease has no leaseDurationSeconds"]
    renewed = _utc(spec.get("renewTime"), "renewTime")
    acquired = _utc(spec.get("acquireTime"), "acquireTime")
    since_renew = (now - renewed).total_seconds()
    if since_renew > duration + LEASE_RENEW_SLACK_S:
        # Just after a restart the old holder's record keeps its old acquireTime until it
        # expires, so a stale renewTime is the handover gap.
        return [f"the Lease was last renewed {since_renew:.0f}s ago: a handover gap"]
    age = (now - acquired).total_seconds()
    if age < RESTART_QUIET_S:
        return [f"the server restarted {age:.0f}s ago, under {RESTART_QUIET_S}s"]
    return []


def _munge(version: str) -> str:
    """The form SUC uses in `latestVersion` and in the plan env: `+` becomes `-`."""
    return version.replace("+", "-")


def check_server_plan_done(get: Getter, version: str | None) -> list[str]:
    """Agent mode: refuse until the server Plan finished at `version` and every server runs it."""
    if not version:
        return ["SYSTEM_UPGRADE_PLAN_LATEST_VERSION is unset"]
    want = _munge(version)
    plan = get(f"{PLANS_PATH}/{SERVER_PLAN}")
    status = plan.get("status") or {}
    refusals = []
    if status.get("applying"):
        refusals.append(f"plan {SERVER_PLAN} is applying to {status['applying']}")
    if (_condition(plan, "Complete") or {}).get("status") != "True":
        refusals.append(f"plan {SERVER_PLAN} is not Complete")
    latest = status.get("latestVersion")
    if not isinstance(latest, str) or _munge(latest) != want:
        refusals.append(f"plan {SERVER_PLAN} resolved {latest!r}, not {want}")
    servers = [
        n
        for n in _items(get(NODES_PATH))
        if CONTROL_PLANE_LABEL in ((n.get("metadata") or {}).get("labels") or {})
    ]
    if not servers:
        refusals.append("no control-plane node is listed")
    for node in servers:
        running = str(
            (node.get("status") or {}).get("nodeInfo", {}).get("kubeletVersion", "")
        )
        if _munge(running) != want:
            refusals.append(
                f"node {_name(node)} runs {running or 'no version'}, not {want}"
            )
    return refusals


def check_suc_idle(get: Getter) -> list[str]:
    """Host mode: refuse while a Plan is applying or a SUC Job has not finished."""
    refusals = []
    for plan_name in (SERVER_PLAN, AGENT_PLAN):
        plan = get(f"{PLANS_PATH}/{plan_name}")
        applying = (plan.get("status") or {}).get("applying")
        if applying:
            refusals.append(f"plan {plan_name} is applying to {applying}")
    for job in _items(get(JOBS_PATH)):
        done = [_condition(job, k) for k in ("Complete", "Failed")]
        if not any(c and c.get("status") == "True" for c in done):
            refusals.append(f"job {_name(job)} has not finished")
    return refusals


def check_flux_settled(get: Getter, notes: list[str]) -> list[str]:
    """Host mode: refuse while any Flux Kustomization is applying, behind its source, or absent.

    The root `homelab` Kustomization is a named member, so an empty or Forbidden list refuses.
    A suspended Kustomization never advances `lastAppliedRevision`, so it is left out of every
    clause and named in `notes`.
    """
    items = _items(get(KUSTOMIZATIONS_PATH))
    if ROOT_KUSTOMIZATION not in {_name(i) for i in items}:
        return [f"Kustomization {ROOT_KUSTOMIZATION} is not listed"]
    refusals: list[str] = []
    revisions: dict[str, object] = {}
    for item in items:
        name, spec, status = (
            _name(item),
            item.get("spec") or {},
            item.get("status") or {},
        )
        if spec.get("suspend") is True:
            notes.append(f"Kustomization {name} is suspended: skipped")
            continue
        if (_condition(item, "Ready") or {}).get("status") != "True":
            refusals.append(f"Kustomization {name} is not Ready")
        if status.get("observedGeneration") != (item.get("metadata") or {}).get(
            "generation"
        ):
            refusals.append(
                f"Kustomization {name} has not observed its latest generation"
            )
        ref = spec.get("sourceRef") or {}
        template = _SOURCE_PATHS.get(str(ref.get("kind")))
        if template is None:
            refusals.append(f"Kustomization {name} follows an unknown source kind")
            continue
        path = template.format(
            ns=ref.get("namespace") or (item.get("metadata") or {}).get("namespace"),
            name=ref.get("name"),
        )
        if path not in revisions:
            revisions[path] = (
                (get(path).get("status") or {}).get("artifact") or {}
            ).get("revision")
        if not revisions[path] or status.get("lastAppliedRevision") != revisions[path]:
            refusals.append(
                f"Kustomization {name} has not applied its source's revision"
            )
    return refusals


# ── host checks, read under --host-root ──────────────────────────────────────────────────────


def resolve_under(root: str, path: str) -> str:
    """`path` with every symlink resolved inside `root`, an absolute target re-rooted there.

    Under `/host`, `/var/lock` is an absolute link to `/run/lock`, and the kernel would follow
    it to the container's own `/run/lock`. Raises OSError (ELOOP) past 40 hops.
    """
    resolved: list[str] = []
    queue = [p for p in path.split("/") if p]
    hops = 0
    while queue:
        part = queue.pop(0)
        if part == ".":
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = os.path.join(root, *resolved, part)
        if os.path.islink(candidate):
            hops += 1
            if hops > _MAX_SYMLINK_HOPS:
                raise OSError(errno.ELOOP, "too many symlinks", path)
            target = os.readlink(candidate)
            if target.startswith("/"):
                resolved = []
            queue = [p for p in target.split("/") if p] + queue
        else:
            resolved.append(part)
    return os.path.join(root, *resolved)


def check_restart_marker(root: str) -> list[str]:
    """Refuse while `homelab-k3s-restart` runs: systemd creates its RuntimeDirectory first."""
    marker = os.path.join(root, RESTART_MARKER.lstrip("/"))
    try:
        os.stat(marker)
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"cannot look for {RESTART_MARKER}: {exc}"]
    return [f"{RESTART_MARKER} exists: homelab-k3s-restart is running"]


def check_deployer_hold(root: str) -> list[str]:
    """Refuse on a held deploy, and on a state directory that is absent or unreadable.

    An absent `hold_sha` is the deployer's no-hold state. An absent DIRECTORY is not: that is a
    host that is not the deploy host, and `k3s_upgrade.held_sha` refuses it too.
    """
    state_dir = os.path.join(root, HOLD_DIR.lstrip("/"))
    if not os.path.isdir(state_dir):
        return [f"{HOLD_DIR} is not a readable directory"]
    try:
        held = Path(state_dir, HOLD_FILE).read_text().strip()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        return [f"cannot read {HOLD_FILE}: {exc}"]
    return [f"the deployer holds {held}"] if held else []


def check_deploy_lock(root: str) -> list[str]:
    """Refuse while a deploy holds the `all` lock, and when the lock file is absent.

    The open is `O_RDONLY`, never `O_CREAT`. Under `fs.protected_regular=2` a root `O_CREAT`
    open of the deployer-owned file fails with EACCES, and a file root creates there breaks
    the deployer's own open. `flock` works on a read-only descriptor.
    """
    try:
        path = resolve_under(root, ALL_LOCK)
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return [
            f"{ALL_LOCK} is absent: the restart component's tmpfiles entry creates it"
        ]
    except OSError as exc:
        return [f"cannot open {ALL_LOCK}: {exc}"]
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return ["a deploy holds the all lock"]
    except OSError as exc:
        return [f"cannot test {ALL_LOCK}: {exc}"]
    finally:
        os.close(fd)  # closing the descriptor also releases the flock
    return []


# ── the modes ────────────────────────────────────────────────────────────────────────────────

Check = tuple[str, Callable[[], list[str]]]


def checks_for(
    mode: str,
    get: Getter,
    root: str,
    version: str | None,
    now: Callable[[], datetime],
    guard_server_restart: bool,
    notes: list[str],
) -> list[Check]:
    """The ordered checks of one mode. Host mode skips the marker, the hold and the lock."""
    storage: list[Check] = [
        ("volumes", lambda: check_volumes(get)),
        ("backups", lambda: check_backups(get)),
        ("nodes", lambda: check_nodes(get)),
        ("coredns", lambda: check_coredns(get)),
    ]
    guard: Check = ("server restart", lambda: check_server_restart(get, now()))
    marker: Check = ("restart marker", lambda: check_restart_marker(root))
    if mode == "server":
        return [
            *storage,
            marker,
            guard,
            ("deployer hold", lambda: check_deployer_hold(root)),
            ("deploy lock", lambda: check_deploy_lock(root)),
        ]
    if mode == "agent":
        return [
            ("server plan", lambda: check_server_plan_done(get, version)),
            *storage,
            marker,
            guard,
        ]
    if mode == "host":
        return [
            *storage,
            ("suc idle", lambda: check_suc_idle(get)),
            ("flux settled", lambda: check_flux_settled(get, notes)),
            *([guard] if guard_server_restart else []),
        ]
    raise ValueError(f"unknown mode {mode!r}")


def run_round(checks: list[Check]) -> list[str]:
    """Run every check and return its refusals. A check that raises refuses, and the rest run."""
    refusals = []
    for name, check in checks:
        try:
            found = check()
        except (ReadError, ValueError, TypeError, AttributeError, KeyError) as exc:
            found = [f"cannot decide: {exc}"]
        refusals.extend(f"{name}: {reason}" for reason in found)
    return refusals


def wait_for_pass(
    checks: list[Check],
    notes: list[str],
    *,
    once: bool,
    poll: float,
    max_wait: float | None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out=None,
) -> int:
    """Run the checks until all pass (0), or once / past `max_wait` and refused (1)."""
    out = out or sys.stdout
    start = clock()
    while True:
        notes.clear()
        refusals = run_round(checks)
        for note in notes:
            print(f"note: {note}", file=out, flush=True)
        if not refusals:
            print("PASS: every check passed", file=out, flush=True)
            return 0
        for refusal in refusals:
            print(f"REFUSE {refusal}", file=out, flush=True)
        if once or (max_wait is not None and clock() - start + poll > max_wait):
            print(f"REFUSED: {len(refusals)} check(s) refused", file=out, flush=True)
            return 1
        sleep(poll)


def _default_server() -> str:
    host, port = (
        os.environ.get("KUBERNETES_SERVICE_HOST"),
        os.environ.get("KUBERNETES_SERVICE_PORT"),
    )
    if not host or not port:
        return ""
    return f"https://[{host}]:{port}" if ":" in host else f"https://{host}:{port}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("server", "agent", "host"))
    parser.add_argument(
        "--host-root", default="/host", help="where the node's / is mounted"
    )
    parser.add_argument("--server", default=_default_server(), help="API server URL")
    parser.add_argument("--token-file", default=f"{_SA_DIR}/token")
    parser.add_argument("--ca-file", default=f"{_SA_DIR}/ca.crt")
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="seconds per API read"
    )
    parser.add_argument(
        "--poll", type=float, default=30.0, help="seconds between rounds"
    )
    parser.add_argument(
        "--max-wait", type=float, help="give up after this many seconds"
    )
    parser.add_argument(
        "--once", action="store_true", help="run one round (host mode always does)"
    )
    parser.add_argument(
        "--guard-server-restart",
        action="store_true",
        help="host mode: also run the server-restart guard (daniel-server)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.server:
        print("REFUSED: no API server: pass --server or run in a pod", file=sys.stderr)
        return 1
    once = args.once or args.mode == "host"
    deadline = time.monotonic() + args.max_wait if args.max_wait and once else None
    api = Api(args.server, args.token_file, args.ca_file, args.timeout, deadline)
    notes: list[str] = []
    checks = checks_for(
        args.mode,
        api,
        args.host_root,
        os.environ.get("SYSTEM_UPGRADE_PLAN_LATEST_VERSION"),
        lambda: datetime.now(timezone.utc),
        args.guard_server_restart,
        notes,
    )
    return wait_for_pass(
        checks, notes, once=once, poll=args.poll, max_wait=args.max_wait
    )


if __name__ == "__main__":
    sys.exit(main())
