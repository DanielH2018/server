"""`probe.py health <svc>` — the post-deploy gate, plus the argv builders it shares.

Split out of probe.py, which had grown to 1349 lines across thirteen subcommands.
`readonly-rbac` and `vip-placement` have since moved out too, to probe_lib/readonly_rbac.py and
probe_lib/vip_placement.py — the three subcommands shared a file but never shared logic, so this
one keeps only `health` and the argv builders/format functions `run_health` uses.

The gate exits 0 only when the workload is fully rolled out AND nothing restarted recently.
Both halves are load-bearing, and the reasoning for each sits with the function it governs. A
role with no Deployment/DaemonSet/StatefulSet but a CronJob (configarr, pi-peer-backup) is
gated the same way on its most recent Job instead — see format_cronjob_health.

WHAT "NOT FOUND" IS ALLOWED TO MEAN. `deploy_detach_notify.py` turns some of this command's
failures into a `skipped` that does not fail the deploy verdict, matched by substring against
its `NOT_APPLICABLE_MARKERS`. Which failures may carry such a marker is positional, not a
property of any one message:

  - a name GUESSED from the deploy tag, absent from the cluster, may skip: the guess only ever
    meant "maybe this tag names a workload", and a miss lets the `--docker` fallback try the Pi.
  - a name RESOLVED from the role's own rendered manifests, absent from the cluster, must NOT
    skip: the manifests say that object should exist, so its absence is a failed deploy.

Every message below is written to that rule, and `test_probe_health.py` asserts each one lands
on the intended side of it. PR #685 is the reason: `land.sh` printed `VERDICT: settled` for a
claude-otel deploy whose health gate never ran.
"""

import json
import re
import subprocess
from datetime import datetime, timezone

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# `core.<name>` for anything the tests monkeypatch — binding those into this module's
# globals with a `from core import ...` would take a snapshot the patch never reaches.
from diagnostics.probe_lib import core
from diagnostics.probe_lib.core import PI_HOST

from lib.repo_paths import HOST_VARS, K8S_ROLES, REPO

PI_HOST_VARS = HOST_VARS / "daniel-pi.yml"

# The kinds a rollout gate can actually check, and kubectl's spelling for each. Deployment
# first because the fleet is overwhelmingly Deployments. StatefulSet is here with no live
# instance today on purpose: resolving a kind the lookup below cannot ask for would make the
# first StatefulSet added to this repo read as MISSING.
WORKLOAD_KINDS = {
    "Deployment": "deploy",
    "DaemonSet": "daemonset",
    "StatefulSet": "statefulset",
}


def inspect_ip_argv(container):
    return [
        "docker",
        "inspect",
        "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
        container,
    ]


def parse_ip(inspect_output):
    """First non-empty token of `docker inspect`'s IP list.

    Host can reach any of a container's bridge IPs. None if the container has no
    address.
    """
    for tok in inspect_output.split():
        if tok:
            return tok
    return None


def inspect_argv(container):
    return ["docker", "inspect", container]


def k8s_service_ip_argv(service, namespace):
    """kubectl argv for a Service's ClusterIP.

    The k8s analog of inspect_ip_argv, for apps (arr) that must be reached directly
    rather than through k8s_endpoint.
    """
    return [
        "k3s",
        "kubectl",
        "-n",
        namespace,
        "get",
        "service",
        service,
        "-o",
        "jsonpath={.spec.clusterIP}",
    ]


def format_health(data, container, declared=False):
    """Summarize a container's state + healthcheck from `docker inspect` output.

    Pure: takes the parsed JSON list and returns (text, exit_code). exit_code is 0
    only when the container is running and (has no healthcheck, or is healthy) — so
    `probe.py health <svc>` is usable as a post-deploy gate.

    `declared` says whether daniel-pi's inventory lists a Docker service by this name;
    `run_health` resolves it. It splits the two situations an absent container can mean, which
    a single "not found" message conflated: a declared service that is missing is a deploy that
    failed and must fail the gate, while an undeclared name is a block tag or a typo and is the
    only one the notifier may skip.
    """
    if not data:
        if declared:
            return (
                f"{container}: MISSING — daniel-pi's inventory declares this service and the "
                "host has no such container, so the deploy did not create it",
                1,
            )
        return (
            f"{container}: not found, and not a declared service on any host "
            "(nothing to health-check)",
            1,
        )
    state = data[0].get("State") or {}
    status = state.get("Status", "unknown")
    restarts = data[0].get("RestartCount", 0)
    health = state.get("Health")
    if health:
        hstatus = health.get("Status", "unknown")
        line = f"{container}: {status}, health={hstatus}, restarts={restarts}"
        if hstatus != "healthy":
            line += f" — failing streak {health.get('FailingStreak', 0)}"
            log = health.get("Log") or []
            last = (log[-1].get("Output") or "").strip().splitlines() if log else []
            if last:
                line += f"; last check: {last[-1][:160]}"
        return (line, 0 if status == "running" and hstatus == "healthy" else 1)
    return (
        f"{container}: {status} (no healthcheck), restarts={restarts}",
        0 if status == "running" else 1,
    )


# A container that crashlooped this recently is not healthy, however ready it reads right now.
# Ansible's post-rollout gate soaks for 60s (k8s_rollout_stabilise_seconds) watching the restart
# COUNT, because a pod that crashes and recovers within a second passes every readiness-derived
# field — see roles/k8s/manifests/tasks/assert_stable.yml. probe.py takes ONE sample instead of
# soaking, so it reads the same signal from the other end: how long ago the last restart was.
# Wider than the Ansible window, since a single sample can land anywhere in a crash cycle.
RECENT_RESTART_SECONDS = 180


def k8s_deploy_argv(service, namespace, kind="deploy"):
    """kubectl argv to fetch a Deployment/DaemonSet/StatefulSet as JSON.

    Args:
        service: The workload name.
        namespace: The k8s namespace it runs in.
        kind: The workload kind to fetch (``deploy``, ``daemonset`` or ``statefulset``).
    """
    return [
        "k3s",
        "kubectl",
        "-n",
        namespace,
        "get",
        kind,
        service,
        "-o",
        "json",
    ]


def k8s_pods_argv(service, namespace, selector=None):
    """kubectl argv for a workload's pods.

    `selector` overrides the `app=<service>` guess — pass `pod_selector(workload)` whenever the
    workload document is in hand.
    """
    return [
        "k3s",
        "kubectl",
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        selector or f"app={service}",
        "-o",
        "json",
    ]


# RBAC decides which of two paths this gate can take: trigger a fresh run itself, the way
# `k8s/cronjob-gate` does at deploy time, or fall back to reading the most recent existing run.
# probe.py runs as `homelab-readonly` (see `roles/setup/k3s/templates/readonly-rbac.yaml.j2`),
# bound to the built-in `view` ClusterRole plus one additive ClusterRole, neither granting any
# write verb. Verified live 2026-09-03, running as that identity:
#   k3s kubectl auth can-i create jobs -n homelab   -> no
#   k3s kubectl auth can-i get jobs -n homelab      -> yes
#   k3s kubectl auth can-i list jobs -n homelab     -> yes
#   k3s kubectl auth can-i get cronjobs -n homelab  -> yes
#   k3s kubectl auth can-i list cronjobs -n homelab -> yes
# So the READ-ONLY FALLBACK IS THE ONLY PATH LIVE HERE: this module never creates a Job, only
# reads the CronJob and its existing Jobs. That is the whole reason `format_cronjob_health`
# has a schedule-fallback branch at all — the trigger-a-run half `k8s/cronjob-gate` uses is not
# available to this identity, ever, by design (the readonly SA is a read path, deliberately,
# and `k8s/cronjob-gate` runs under Ansible's escalated connection instead).


def k8s_cronjob_argv(name, namespace):
    """kubectl argv to fetch a CronJob as JSON."""
    return ["k3s", "kubectl", "-n", namespace, "get", "cronjob", name, "-o", "json"]


def k8s_jobs_argv(namespace):
    """kubectl argv for every Job in a namespace, as JSON.

    Not filtered server-side: a CronJob's Jobs carry no label naming their owner, only an
    `ownerReferences` entry, and kubectl has no field selector for that. The namespaces this
    runs against (`homelab`, `longhorn-system`) hold at most a handful of Jobs each — bounded
    by each CronJob's `successfulJobsHistoryLimit` — so filtering client-side in
    latest_owned_job costs nothing worth avoiding.
    """
    return ["k3s", "kubectl", "-n", namespace, "get", "jobs", "-o", "json"]


def k8s_job_pods_argv(job_name, namespace):
    """kubectl argv for one Job's pods, as JSON.

    `batch.kubernetes.io/job-name`, not the deprecated bare `job-name` — the same choice
    `roles/k8s/cronjob-gate/tasks/main.yml` documents and makes, for the same reason: if the
    legacy label is ever dropped, the other selector silently matches nothing.
    """
    return [
        "k3s",
        "kubectl",
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        f"batch.kubernetes.io/job-name={job_name}",
        "-o",
        "json",
    ]


def latest_owned_job(jobs_doc, cronjob_name):
    """The most recently created Job owned by `cronjob_name`, or None if it has none.

    `kubectl create job --from=cronjob/<name>` sets a controller `ownerReferences` entry
    pointing at the CronJob, the same as a scheduled firing does — verified live, see
    `roles/k8s/cronjob-gate/CLAUDE.md` ("Why a CronJob needs this at all"). So this covers
    both ways a Job can exist for a CronJob: the schedule firing it itself, and
    `k8s/cronjob-gate` triggering an out-of-band run at deploy time.
    """
    owned = [
        job
        for job in (jobs_doc or {}).get("items") or []
        for ref in (job.get("metadata") or {}).get("ownerReferences") or []
        if ref.get("kind") == "CronJob"
        and ref.get("name") == cronjob_name
        and ref.get("controller")
    ]
    if not owned:
        return None
    return max(
        owned,
        key=lambda job: (job.get("metadata") or {}).get("creationTimestamp") or "",
    )


def pod_selector(workload):
    """The `-l` expression matching a workload's OWN pods, read from its pod template labels.

    Not `app=<name>`. pihole-2's Deployment selects `app: pihole`, so `app=pihole-2` matched
    no pods at all while `app=pihole` matched both piholes' — and a pod query that matches
    nothing yields `restarts=0` with an empty recent-restart list, which is byte-identical to a
    genuinely quiet workload. That would leave the restart half of this gate silently inert,
    the half that caught a crashlooping kube-state-metrics on 2026-08-07.

    Nor `spec.selector.matchLabels`, which is what this read until 2026-09-02. `spec.selector`
    is `apps/v1` immutable, so a role running two instances off one pod template cannot
    discriminate them there: both pihole Deployments select `app: pihole`, and each therefore
    read the union of the two instances' pods. The pod template is where the discriminating
    label can live — pihole's carries `instance: pihole` / `instance: pihole-2` for exactly
    that reason (`roles/k8s/pihole/templates/deployment.yaml.j2`), and the pihole web Service
    already selects on it. k8s requires `spec.template.metadata.labels` to be a superset of
    `spec.selector.matchLabels`, so this is never wider than the selector.

    It can be narrower than the running set during a deploy that changes a pod template label:
    pods from the previous revision do not carry the new label and drop out of the query. The
    rollout half of the gate still sees them, because that half reads the Deployment's own
    replica counts rather than this query.

    Falls back to the selector, then to the `app=<name>` guess, for a workload carrying
    neither — which the k8s API rejects, so it is unreachable. An empty `-l` would query every
    pod in the namespace, which is worse than the guess.
    """
    spec = (workload or {}).get("spec") or {}
    template = ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
    matched = template or (spec.get("selector") or {}).get("matchLabels") or {}
    return ",".join(f"{key}={value}" for key, value in sorted(matched.items()))


def _seconds_since(timestamp, now):
    """Seconds between an RFC3339 kubectl timestamp and `now`, or None if unparseable."""
    if not timestamp:
        return None
    try:
        when = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError, TypeError:
        return None
    return (now - when).total_seconds()


def _rollout_counts(workload):
    """(desired, updated, ready, available) for a Deployment or a DaemonSet.

    DaemonSets carry the same four numbers under different names, and `desired` is the count of
    nodes the scheduler picked rather than a spec field — so a DaemonSet pinned to one node is
    complete at 1/1, not at one-per-node.
    """
    spec, status = workload.get("spec") or {}, workload.get("status") or {}
    if workload.get("kind") == "DaemonSet":
        return (
            status.get("desiredNumberScheduled", 0),
            status.get("updatedNumberScheduled", 0),
            status.get("numberReady", 0),
            status.get("numberAvailable", 0),
        )
    return (
        spec.get("replicas", 1),
        status.get("updatedReplicas", 0),
        status.get("readyReplicas", 0),
        status.get("availableReplicas", 0),
    )


def format_k8s_health(deploy, pods, service, now):
    """Summarize a workload's rollout + its pods' restarts. Returns (text, exit_code).

    Pure: takes the two parsed kubectl JSON documents and a `now`, returns what to print.
    `deploy` is a Deployment or a DaemonSet — six workloads here are DaemonSets (promtail,
    node-exporter, the crowdsec node agent, dri-device-plugin, ...) and a gate that silently
    could not check them would be a gate with holes exactly where the node-level agents are.

    exit_code is 0 only when the rollout is COMPLETE (the observed generation has caught up and
    every replica is updated, ready and available) AND no container restarted within
    RECENT_RESTART_SECONDS. Both halves are load-bearing: readiness alone flips Available before
    a bad liveness probe starts killing the container, which is the failure that produced a green
    deploy and a crashlooping kube-state-metrics on 2026-08-07.
    """
    if not deploy:
        return (
            f"{service}: no Deployment or DaemonSet in this namespace "
            "(wrong name, wrong namespace, or the deploy never ran?)",
            1,
        )

    meta, status = deploy.get("metadata") or {}, deploy.get("status") or {}
    desired, updated, ready, available = _rollout_counts(deploy)
    # A spec edit bumps metadata.generation immediately; status.observedGeneration only catches
    # up once the controller has acted. Comparing them is what distinguishes "rolled out" from
    # "the controller has not looked at my change yet" — the old ReplicaSet satisfies every
    # replica count in the meantime.
    stale = status.get("observedGeneration", 0) < meta.get("generation", 0)
    rolled_out = (
        not stale and updated == desired and ready == desired and available == desired
    )

    restarts, recent = 0, []
    for pod in (pods or {}).get("items") or []:
        pod_name = (pod.get("metadata") or {}).get("name", "?")
        for cs in (pod.get("status") or {}).get("containerStatuses") or []:
            count = cs.get("restartCount", 0)
            restarts += count
            if not count:
                continue
            finished = ((cs.get("lastState") or {}).get("terminated") or {}).get(
                "finishedAt"
            )
            age = _seconds_since(finished, now)
            where = f"{pod_name}/{cs.get('name', '?')}"
            # A restart whose time cannot be read counts as RECENT. Treating "unknown" as "long
            # ago" would fail open — the one direction a gate must never fail — and this branch
            # is reachable whenever kubectl's timestamp format shifts under us (every finishedAt
            # in this cluster is second-precision UTC today; fractional seconds parse as None).
            if age is None:
                recent.append(f"{where} restarted at an unreadable time ({finished!r})")
            elif age < RECENT_RESTART_SECONDS:
                recent.append(f"{where} restarted {int(age)}s ago")

    line = f"{service}: {ready}/{desired} ready, {updated} updated, restarts={restarts}"
    if stale:
        line += " — spec changed, rollout not observed yet"
    elif not rolled_out:
        line += " — rollout incomplete"
    if recent:
        line += f" — RECENT RESTART: {'; '.join(recent)}"
    return (line, 0 if rolled_out and not recent else 1)


def _job_outcome(job):
    """'succeeded', 'failed' or 'running' for a Job, read from its status.

    `status.conditions` is the documented source (`Complete`/`Failed`, both `status: "True"`
    once set), but `status.succeeded`/`status.failed` are populated first and conditions can
    lag a beat behind them — so both are checked and either is enough.
    """
    status = (job or {}).get("status") or {}
    conditions = {
        c.get("type"): c.get("status") for c in status.get("conditions") or []
    }
    if conditions.get("Complete") == "True" or status.get("succeeded", 0) >= 1:
        return "succeeded"
    if conditions.get("Failed") == "True" or status.get("failed", 0) >= 1:
        return "failed"
    return "running"


# `M H * * *` (daily) and `M H * * D` (weekly) are the only two schedule shapes any CronJob in
# this cluster uses today — `k3s kubectl get cronjob -A`, checked 2026-09-03: configarr,
# pi-peer-backup and all seven Longhorn backup jobs. This is deliberately not a general cron
# parser: a schedule outside these two shapes returns None and the caller fails closed rather
# than guess what "normal interval" means for it.
_DAILY_SCHEDULE = re.compile(r"^\s*\d+\s+\d+\s+\*\s+\*\s+\*\s*$")
_WEEKLY_SCHEDULE = re.compile(r"^\s*\d+\s+\d+\s+\*\s+\*\s+[0-6]\s*$")

# How many schedule intervals a CronJob-only role's last known run may age past before the
# schedule-fallback path (below) calls it overdue rather than merely "hasn't fired again yet".
# 2x rather than 1x: a run that fires a few minutes late every so often is normal cluster jitter,
# not a stalled CronJob, and this path is only reached when release_stamp.yml also says the
# deploy-time k8s/cronjob-gate run either never happened or predates what it should be proving.
CRONJOB_STALE_MULTIPLIER = 2


def _schedule_interval_seconds(schedule):
    """Seconds between firings for a daily or weekly CronJob `schedule`, or None if neither."""
    if not schedule:
        return None
    if _DAILY_SCHEDULE.match(schedule):
        return 86400
    if _WEEKLY_SCHEDULE.match(schedule):
        return 7 * 86400
    return None


def format_cronjob_health(name, cronjob, latest_job, pods, deploy_applied_at, now):
    """(text, exit code) for one CronJob-only workload's post-deploy verification.

    The CronJob analog of format_k8s_health: a Deployment role is gated on `rollout status`
    plus a restart check, and a CronJob has no rollout, so this reads the CronJob's most
    recent owned Job instead — the same Job `k8s/cronjob-gate` creates at deploy time
    (`roles/k8s/cronjob-gate/CLAUDE.md`), or the next scheduled firing if that hasn't landed.

    Two paths, chosen by comparing `latest_job`'s creation time against `deploy_applied_at`
    (`release_stamp.yml`'s `applied_at` for this service, or None when unreadable):

      - `latest_job` is newer than the deploy (the normal case: `k8s/cronjob-gate` runs one at
        every real deploy) — gated directly on IT: it must have succeeded, and no container in
        its pod may have restarted.
      - `latest_job` predates the deploy, or `deploy_applied_at` is unreadable — the gate this
        role's own deploy should have run either did not run or has not been read yet. Falls
        back to the CronJob's own schedule: the previous run must have succeeded, and its age
        must be within CRONJOB_STALE_MULTIPLIER schedule intervals — a schedule shape this
        cannot size an interval for (anything but plain daily/weekly) fails closed rather than
        guess.

    Unlike `k8s/cronjob-gate` itself, this does not distinguish "the image never started" from
    "the application failed" — cronjob-gate is lenient on the latter because it runs on every
    deploy, including ones where the failure is a known-transient dependency; this is a general
    health read with no such context, so it is a plain pass/fail, the same as
    `format_k8s_health`.
    """
    if cronjob is None:
        return (
            f"{name}: no CronJob in this namespace "
            "(wrong name, wrong namespace, or the deploy never ran?)",
            1,
        )
    if latest_job is None:
        return (
            f"{name}: no Job found for this CronJob — no evidence it has ever run",
            1,
        )

    job_name = (latest_job.get("metadata") or {}).get("name", "?")
    job_created = (latest_job.get("metadata") or {}).get("creationTimestamp")
    job_age = _seconds_since(job_created, now)
    if job_age is None:
        return (
            f"{name}: could not read {job_name}'s creation time ({job_created!r}) — "
            "failing closed",
            1,
        )

    deploy_age = _seconds_since(deploy_applied_at, now)
    fresh = deploy_age is None or job_age < deploy_age

    outcome = _job_outcome(latest_job)
    if outcome == "running":
        return (f"{name}: {job_name} has not finished yet — cannot confirm success", 1)
    if outcome == "failed":
        return (
            f"{name}: {job_name} FAILED"
            + ("" if fresh else " (and it predates the last deploy)"),
            1,
        )

    restarts = sum(
        cs.get("restartCount", 0)
        for pod in (pods or {}).get("items") or []
        for cs in (pod.get("status") or {}).get("containerStatuses") or []
    )
    if restarts:
        return (
            f"{name}: {job_name} succeeded but a container in its pod restarted "
            f"(restarts={restarts})",
            1,
        )

    if fresh:
        return (f"{name}: {job_name} succeeded since the last deploy, no restarts", 0)

    schedule = (cronjob.get("spec") or {}).get("schedule")
    interval = _schedule_interval_seconds(schedule)
    if interval is None:
        return (
            f"{name}: no Job has run since the last deploy (the last one, {job_name}, "
            f"predates it), and its schedule ({schedule!r}) is not one this gate can size an "
            "interval for — failing closed rather than guess",
            1,
        )
    if job_age > interval * CRONJOB_STALE_MULTIPLIER:
        return (
            f"{name}: no Job has run since the last deploy, and the previous successful run "
            f"({job_name}) is {int(job_age)}s old against a {interval}s schedule — overdue",
            1,
        )
    return (
        f"{name}: no Job has run since the last deploy yet, but the previous run "
        f"({job_name}) succeeded {int(job_age)}s ago and is within its {schedule!r} schedule",
        0,
    )


def resolve_service_ip(name):
    """A workload's k8s Service ClusterIP.

    The k8s replacement for `docker inspect`ing a container's bridge IP. A ClusterIP is
    stable across pod restarts and redeploys, so this does not reintroduce
    the hand-copied-IP staleness the docker lookup existed to avoid. Callers reach the
    Service directly rather than through k8s_endpoint, which would put Traefik and Authelia
    in front of an API path that has no bypass rule.
    """
    ns = core.k8s_namespace()
    out = subprocess.run(k8s_service_ip_argv(name, ns), capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"kubectl get service {name} failed: {out.stderr.strip()}")
    ip = out.stdout.strip()
    if not ip:
        raise SystemExit(f"{name} has no ClusterIP (does the Service exist?)")
    return ip


def resolve_ip(container):
    """A Docker container's bridge IP.

    daniel-pi is the only host that still has Docker — on either cluster node this raises
    FileNotFoundError, so use resolve_service_ip.
    """
    out = subprocess.run(inspect_ip_argv(container), capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"docker inspect {container} failed: {out.stderr.strip()}")
    ip = parse_ip(out.stdout)
    if not ip:
        raise SystemExit(f"{container} has no container IP (is it running?)")
    return ip


def declared_on_pi(container):
    """Does daniel-pi's inventory declare a Docker service by this name?

    The Pi is the only Docker host left, so its `containers_list` is the whole population an
    absent container can be measured against.
    """
    import yaml

    try:
        entries = (yaml.safe_load(PI_HOST_VARS.read_text()) or {}).get(
            "containers_list"
        ) or []
    except OSError, yaml.YAMLError:
        # Fail closed: an unreadable inventory must not turn a missing container into a skip.
        return True
    return container in {
        entry.get("name") for entry in entries if isinstance(entry, dict)
    }


_RENDER_CONTEXT = None


def _render_context():
    """(validator module, base var context, containers_list entries), built once per process.

    The import is deferred because it pulls in ansible-core, PyYAML and kubernetes_validate,
    and twelve of probe.py's thirteen subcommands never need any of it. Measured on daniel-box:
    0.41s to import, 0.03s to build the context, 0.02s median to render one role and 0.22s for
    the slowest (home-assistant) — against the 30s `PROBE_TIMEOUT_S` the notifier allows.
    """
    global _RENDER_CONTEXT
    if _RENDER_CONTEXT is None:
        import sys as _sys

        # A directly-invoked script gets only its own directory on sys.path, and pyproject's
        # `pythonpath` is a pytest setting — so the cron and the notifier need this insert.
        # `scripts/` rather than `scripts/validate/`: the package-qualified name is the same
        # one pytest imports, so there is one module object however it is reached.
        _sys.path.insert(0, str(REPO / "scripts"))
        from validate import k8s_manifests as validator

        base = {
            **validator.BASE_CONTEXT,
            **validator.load_yaml(validator.ALL_VARS),
            **validator.load_yaml(validator.HOST_VARS),
            "playbook_dir": str(validator.ANSIBLE),
        }
        _RENDER_CONTEXT = (
            validator,
            validator.resolve_vars(base, base),
            validator.k8s_entries(),
        )
    return _RENDER_CONTEXT


def _role_kind_targets(role, default_namespace, kinds):
    """[(namespace, kind, name)] for every doc of `role`'s manifests whose kind is in `kinds`.

    Shared by role_workload_targets (Deployment/DaemonSet/StatefulSet) and
    role_cronjob_targets (CronJob) — same render, same "not a k8s role this can resolve"
    contract, different kind set.

    None when `role` is not a k8s role this can resolve — then the caller falls back to
    probing the tag name itself, which is what lets `--docker` pick up a Pi service.

    The rendered manifests are the only source that cannot drift from what a deploy applies, so
    this reuses validate.k8s_manifests' own render machinery rather than a second renderer or a
    hand-written role->workload table. `namespace` comes from each object's own metadata,
    falling back to `default_namespace` for the majority that omit it — the same rule
    `kubectl apply -f` follows.

    Raises on a render failure rather than returning None. Fail closed: a role whose templates
    stopped rendering must not degrade to the guess-the-name path, where a miss reads as a skip.
    """
    # Checked before the render context is built, so a block tag (`config`, `deploy`, `cron`)
    # costs a directory stat rather than 0.44s of ansible + kubernetes_validate imports, and a
    # broken renderer environment cannot fail a tag that was never a role.
    role_dir = K8S_ROLES / role
    if not role_dir.is_dir():
        return None

    validator, base, entries = _render_context()
    if role in validator.SKIP_ROLES or role not in entries:
        return None

    templates = sorted(
        p
        for p in (role_dir / "templates").glob("*.j2")
        if validator.is_manifest_template(p)
    )
    ctx = {
        **base,
        **validator.role_defaults(role, base),
        "container_item": entries[role],
    }
    targets = set()
    for tpl in templates:
        err, docs = validator.check_template(role, tpl, ctx)
        if err:
            raise RuntimeError(f"{role}/{tpl.name} failed to render: {err}")
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") not in kinds:
                continue
            meta = doc.get("metadata") or {}
            targets.add(
                (
                    meta.get("namespace") or default_namespace,
                    doc["kind"],
                    meta.get("name"),
                )
            )
    return sorted(targets)


def role_workload_targets(role, default_namespace):
    """[(namespace, kind, name)] every workload `role`'s rendered manifests declare.

    None when `role` is not a k8s role this can resolve — then the caller falls back to
    probing the tag name itself, which is what lets `--docker` pick up a Pi service.

    A deploy tag is a role name, NOT a workload name, and for four roles today it names no
    workload at all: claude-otel deploys grafana/loki/prometheus/tempo/otel-collector/
    kube-state-metrics, scrutiny deploys scrutiny-{web,influxdb,collector}, cloudflare-ddns
    deploys cloudflare-ddns-{direct,proxied}, and dri-device-plugin's DaemonSet lives in
    kube-system rather than the default namespace. All four made `probe.py health <tag>` report
    "no Deployment or DaemonSet", which the deploy notifier skipped — so the gate silently did
    not run. Seven more roles (crowdsec, freshrss, karakeep, loki-homelab, n8n, pihole,
    prowlarr) deploy a workload named after the tag PLUS siblings that went unchecked.
    """
    return _role_kind_targets(role, default_namespace, WORKLOAD_KINDS)


def role_cronjob_targets(role, default_namespace):
    """[(namespace, name)] for every CronJob `role`'s rendered manifests declare.

    Consulted by run_health only as a fallback, when role_workload_targets found nothing —
    a role that deploys both a Deployment and a CronJob is gated on the Deployment, the same
    way manifests_rollout gates it, not on this. Only two roles are CronJob-only today
    (configarr, pi-peer-backup — see the census in
    ansible/tests/diagnostics/test_cronjob_only_role_gate.py), both already including
    `k8s/cronjob-gate` at deploy time; this is the read-only, after-the-fact check for the
    same claim `probe.py health` already makes for a Deployment.
    """
    targets = _role_kind_targets(role, default_namespace, {"CronJob"})
    if targets is None:
        return None
    return [(namespace, name) for namespace, _kind, name in targets]


def format_role_health(role, checked, now):
    """(text, exit code) for a role whose manifests declare at least one workload.

    `checked` is [(namespace, kind, name, workload doc or None, pods doc or None)]. A None
    workload doc is the safety-critical case this function exists for: the role's manifests
    declare that object and the cluster does not have it, which is a failed deploy. Its message
    deliberately carries none of the notifier's NOT_APPLICABLE_MARKERS — asserted directly in
    test_deploy_detach_notify.py, because a rewording that happened to contain one would put a
    failed deploy back on the skip path with every test still green.

    Only the first line reaches the Discord verdict (the notifier reads `splitlines()[0]`), so
    it carries the whole result and the per-workload detail follows it.
    """
    missing, unhealthy, lines = [], [], []
    for namespace, kind, name, workload, pods in checked:
        if workload is None:
            missing.append(f"{kind} {namespace}/{name}")
            lines.append(
                f"  {namespace}/{name}: MISSING — the role's manifests declare this {kind} "
                "and the cluster does not have it (deploy failed?)"
            )
            continue
        text, code = format_k8s_health(workload, pods, f"{namespace}/{name}", now)
        lines.append(f"  {text}")
        if code:
            unhealthy.append(f"{namespace}/{name}")

    failed = missing + unhealthy
    if failed:
        head = (
            f"{role}: {len(failed)} of {len(checked)} workloads FAILED the gate — "
            + ", ".join(failed)
        )
        return "\n".join([head, *lines]), 1
    noun = "workload" if len(checked) == 1 else "workloads"
    head = f"{role}: all {len(checked)} {noun} healthy"
    return "\n".join([head, *lines]), 0


def _fetch_workload(name, namespace):
    """(workload doc or None, pods doc or None) for one name, tried across WORKLOAD_KINDS.

    Asking for the wrong kind just returns non-zero, so the fallback costs one extra call only
    for the DaemonSets and for a name that matches nothing.
    """
    for kind in WORKLOAD_KINDS.values():
        workload = core.json_or_none(k8s_deploy_argv(name, namespace, kind=kind))
        if workload:
            pods = core.json_or_none(
                k8s_pods_argv(name, namespace, pod_selector(workload))
            )
            return workload, pods
    return None, None


def _deploy_applied_at(service):
    """This service's `applied_at` from its release_stamp.yml record, or None if unreadable.

    `roles/k8s/manifests/tasks/release_stamp.yml` writes one record per service after every
    real apply (see `probe_lib/releases.py`, which reads the whole directory; this reads one
    record by name). None covers a service that has never been deployed since the stamp
    shipped, and a truncated or missing file — format_cronjob_health treats a missing deploy
    timestamp as "nothing to compare against" rather than a failure of its own.
    """
    from diagnostics.probe_lib.releases import RELEASE_DIR

    try:
        return json.loads((RELEASE_DIR / f"{service}.json").read_text()).get(
            "applied_at"
        )
    except OSError, ValueError:
        return None


def _fetch_cronjob(name, namespace):
    """(CronJob doc or None, its latest owned Job or None, that Job's pods doc or None)."""
    cronjob = core.json_or_none(k8s_cronjob_argv(name, namespace))
    latest_job = latest_owned_job(core.json_or_none(k8s_jobs_argv(namespace)), name)
    pods = None
    if latest_job:
        job_name = (latest_job.get("metadata") or {}).get("name")
        pods = core.json_or_none(k8s_job_pods_argv(job_name, namespace))
    return cronjob, latest_job, pods


def format_role_cronjob_health(role, checked, now):
    """(text, exit code) for a role whose manifests declare at least one CronJob.

    `checked` is [(namespace, name, cronjob doc or None, latest Job or None, pods doc or
    None, deploy_applied_at)]. Mirrors format_role_health's shape and its NOT_APPLICABLE_
    MARKERS constraint: a CronJob the manifests declare and the cluster lacks reads as a
    failure here too, via format_cronjob_health's own "no CronJob in this namespace" message.
    """
    unhealthy, lines = [], []
    for namespace, name, cronjob, latest_job, pods, deploy_applied_at in checked:
        text, code = format_cronjob_health(
            f"{namespace}/{name}", cronjob, latest_job, pods, deploy_applied_at, now
        )
        lines.append(f"  {text}")
        if code:
            unhealthy.append(f"{namespace}/{name}")

    if unhealthy:
        head = (
            f"{role}: {len(unhealthy)} of {len(checked)} CronJobs FAILED the gate — "
            + ", ".join(unhealthy)
        )
        return "\n".join([head, *lines]), 1
    noun = "CronJob" if len(checked) == 1 else "CronJobs"
    head = f"{role}: all {len(checked)} {noun} healthy"
    return "\n".join([head, *lines]), 0


def run_health(container, docker=False):
    """k8s workload health by default, the Pi's Docker container with --docker.

    k8s first because that is where ~50 of the ~55 services live since the 2026-08-14 Docker
    retirement. Before that this command ran `docker inspect` unconditionally and had been
    dead on both cluster nodes ever since — it died with `FileNotFoundError: 'docker'`,
    because neither node has the binary at all.
    """
    if docker:
        # daniel-pi is the only Docker host left, and probe.py runs on daniel-box, so this is
        # necessarily remote. The ssh is internal to the script, so it is covered by probe.py's
        # own allow-list entry and never reaches the Bash classifier.
        argv = ["ssh", PI_HOST] + inspect_argv(container)
        out = subprocess.run(argv, capture_output=True, text=True)
        try:
            data = json.loads(out.stdout) if out.returncode == 0 else []
        except json.JSONDecodeError:
            data = []
        text, code = format_health(data, container, declared=declared_on_pi(container))
        print(text)
        return code

    ns = core.k8s_namespace()
    try:
        targets = role_workload_targets(container, ns)
    except Exception as exc:
        print(
            f"{container}: could not resolve which workloads this role deploys ({exc}) — "
            "reporting a failure rather than a gate that did not run"
        )
        return 1

    if targets is None:
        # Not a k8s role, so the tag is the only name available. A miss here means "this tag
        # is not a k8s workload name", which is exactly what lets --docker take over.
        workload, pods = _fetch_workload(container, ns)
        text, code = format_k8s_health(
            workload, pods, container, datetime.now(timezone.utc)
        )
        print(text)
        return code

    if not targets:
        # No Deployment/DaemonSet/StatefulSet — the CronJob-only path, the read-only analog
        # of manifests_rollout for a role that instead includes k8s/cronjob-gate. A role
        # deploying both is gated on its rollout above, not here: role_cronjob_targets is
        # only ever consulted when role_workload_targets found nothing to gate on.
        try:
            cronjob_targets = role_cronjob_targets(container, ns)
        except Exception as exc:
            print(
                f"{container}: could not resolve which CronJobs this role deploys ({exc}) — "
                "reporting a failure rather than a gate that did not run"
            )
            return 1
        if cronjob_targets:
            now = datetime.now(timezone.utc)
            deploy_applied_at = _deploy_applied_at(container)
            checked = [
                (namespace, name, *_fetch_cronjob(name, namespace), deploy_applied_at)
                for namespace, name in cronjob_targets
            ]
            text, code = format_role_cronjob_health(container, checked, now)
            print(text)
            return code
        # Genuinely nothing to gate — a Job-only role (media-volume, netpol-baseline) whose
        # one-shot setup Jobs are already gated to completion at deploy time inline, or a role
        # with neither. This message is load-bearing text: deploy_detach_notify.py's
        # NOT_APPLICABLE_MARKERS matches it by substring, so a reword here must keep it intact
        # (test_deploy_detach_notify.py asserts the exact string is in this file's source).
        print(
            f"{container}: the role declares no rollout-checkable workload "
            "(no Deployment, DaemonSet or StatefulSet in its manifests)"
        )
        return 1

    now = datetime.now(timezone.utc)
    checked = [
        (namespace, kind, name, *_fetch_workload(name, namespace))
        for namespace, kind, name in targets
    ]
    text, code = format_role_health(container, checked, now)
    print(text)
    return code
