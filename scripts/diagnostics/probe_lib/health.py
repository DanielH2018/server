"""`probe.py health <svc>` — the post-deploy gate, from a deploy tag to a per-workload verdict.

Split out of probe.py, which had grown to 1349 lines across thirteen subcommands, and split
again at 938 lines into four helper modules this one drives:

  - `health_kubectl.py` — the kubectl argv builders and `pod_selector`
  - `health_rollout.py` — `format_k8s_health`, the Deployment/DaemonSet verdict
  - `health_cronjob.py` — `format_cronjob_health`, the CronJob verdict
  - `health_docker.py`  — the Pi's Docker containers and the ClusterIP lookups

What stays here is the part that needs all four: resolving a deploy tag to the workloads the
role's rendered manifests declare, fetching each one, and rolling the per-workload verdicts up
into the line `deploy_detach_notify.py` reads.

The gate exits 0 only when the workload is fully rolled out AND nothing restarted recently.
Both halves are load-bearing, and the reasoning for each sits with the function it governs. A
role with no Deployment/DaemonSet/StatefulSet but a CronJob (configarr, pi-peer-backup) is
gated the same way on its most recent Job instead — see health_cronjob.format_cronjob_health.

WHAT "NOT FOUND" IS ALLOWED TO MEAN. This paragraph is the canonical statement of the rule, and
the three helper modules point back at it rather than restating it. `deploy_detach_notify.py`
turns some of this command's failures into a `skipped` that does not fail the deploy verdict,
matched by substring against its `NOT_APPLICABLE_MARKERS`. Which failures may carry such a
marker is positional, not a property of any one message:

  - a name GUESSED from the deploy tag, absent from the cluster, may skip: the guess only ever
    meant "maybe this tag names a workload", and a miss lets the `--docker` fallback try the Pi.
  - a name RESOLVED from the role's own rendered manifests, absent from the cluster, must NOT
    skip: the manifests say that object should exist, so its absence is a failed deploy.

Every message across the four modules is written to that rule, and the `test_probe_health*.py`
suites assert each one lands on the intended side of it. PR #685 is the reason: `land.sh`
printed `VERDICT: settled` for a claude-otel deploy whose health gate never ran.
"""

import json
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
from diagnostics.probe_lib.health_cronjob import format_cronjob_health, latest_owned_job
from diagnostics.probe_lib.health_docker import (
    declared_on_pi,
    format_health,
    inspect_argv,
    # Re-exported only: probe.py imports it here and hands it to `plan()` as that function's
    # IP resolver (probe.py:688). Nothing in this module calls it, so ruff --fix deletes the
    # import without the noqa, and probe.py then fails at import.
    resolve_ip,  # noqa: F401
)
from diagnostics.probe_lib.health_kubectl import (
    WORKLOAD_KINDS,
    k8s_cronjob_argv,
    k8s_deploy_argv,
    k8s_job_pods_argv,
    k8s_jobs_argv,
    k8s_pods_argv,
    pod_selector,
)
from diagnostics.probe_lib.health_rollout import format_k8s_health

from lib.repo_paths import K8S_ROLES, REPO

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
    (configarr, pi-peer-backup), both already including `k8s/cronjob-gate` at deploy time;
    this is the read-only, after-the-fact check for the same claim `probe.py health` already
    makes for a Deployment. Two censuses pin that pair from opposite sides, and both assert
    equality rather than a lower bound so a third role cannot join unnoticed:
    scripts/diagnostics/tests/test_probe_health_resolver.py from the health-gate side, and
    ansible/tests/deploy/test_cronjob_only_roles_include_the_gate.py from the deploy side.
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
