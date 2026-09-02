"""`probe.py health <svc>` — the post-deploy gate, plus the argv builders it shares.

Split out of probe.py, which had grown to 1349 lines across thirteen subcommands.
`readonly-rbac` and `vip-placement` have since moved out too, to probe_readonly_rbac.py and
probe_vip_placement.py — the three subcommands shared a file but never shared logic, so this
one keeps only `health` and the argv builders/format functions `run_health` uses.

The gate exits 0 only when the workload is fully rolled out AND nothing restarted recently.
Both halves are load-bearing, and the reasoning for each sits with the function it governs.

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
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# `core.<name>` for anything the tests monkeypatch — binding those into this module's
# globals with a `from probe_core import ...` would take a snapshot the patch never reaches.
import probe_core as core
from probe_core import PI_HOST

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    """First non-empty token of `docker inspect`'s IP list (host can reach any
    of a container's bridge IPs). None if the container has no address."""
    for tok in inspect_output.split():
        if tok:
            return tok
    return None


def inspect_argv(container):
    return ["docker", "inspect", container]


def k8s_service_ip_argv(service, namespace):
    """kubectl argv for a Service's ClusterIP — the k8s analog of inspect_ip_argv, for
    apps (arr) that must be reached directly rather than through k8s_endpoint."""
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
    """kubectl argv for a workload's pods. `selector` overrides the `app=<service>` guess —
    pass `pod_selector(workload)` whenever the workload document is in hand."""
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


def pod_selector(workload):
    """The `-l` expression matching a workload's OWN pods, read from its spec.selector.

    Not `app=<name>`. pihole-2's Deployment selects `app: pihole`, so `app=pihole-2` matched
    no pods at all while `app=pihole` matched both piholes' — and a pod query that matches
    nothing yields `restarts=0` with an empty recent-restart list, which is byte-identical to a
    genuinely quiet workload. That would leave the restart half of this gate silently inert,
    the half that caught a crashlooping kube-state-metrics on 2026-08-07.

    64 of the 65 rendered workloads do use `app=<name>`, and a rule that is right 64 times and
    quietly wrong the 65th is the wrong rule for a gate. Falls back to the guess only for a
    workload carrying no selector at all, which the k8s API rejects — querying every pod in the
    namespace instead would be worse than the guess.

    Both pihole Deployments select `app: pihole`, so each now reads the union of the two's
    pods and a restart in either fails both. That over-reports rather than under-reports, which
    is the direction a gate may err in; the overlapping selectors themselves are a pihole role
    question, not this function's.
    """
    labels = ((workload or {}).get("spec") or {}).get("selector") or {}
    matched = labels.get("matchLabels") or {}
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


def resolve_service_ip(name):
    """A workload's k8s Service ClusterIP — the k8s replacement for `docker inspect`ing a
    container's bridge IP.

    A ClusterIP is stable across pod restarts and redeploys, so this does not reintroduce
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
    """A Docker container's bridge IP. daniel-pi is the only host that still has Docker —
    on either cluster node this raises FileNotFoundError, so use resolve_service_ip."""
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
        # Inserting the validate/ directory itself (rather than scripts/) keeps the module
        # under the same name pytest imports it as, so there is one module object, not two.
        _sys.path.insert(0, str(REPO / "scripts" / "validate"))
        import validate_k8s_manifests as validator

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

    The rendered manifests are the only source that cannot drift from what a deploy applies, so
    this reuses validate_k8s_manifests' own render machinery rather than a second renderer or a
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
            if not isinstance(doc, dict) or doc.get("kind") not in WORKLOAD_KINDS:
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
