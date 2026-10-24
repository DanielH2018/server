"""`probe.py health` judges a Flux-owned CronJob role against the revision Flux applies (#5955).

A Flux-owned role has no release record, so its CronJob gate has no `applied_at`. The gate
Job whose pod template's `homelab/role-digest8` equals Kustomization `<tag>`'s
`homelab/content-digest[:8]` is the run of the revision Flux applies; any other Job is not.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_cronjob_flux_revision.py
"""

import json
import subprocess
from pathlib import Path

from _probe_health_fixtures import NOW, current_checkout, pods

from diagnostics.probe_lib import health, health_cronjob
from diagnostics.probe_lib.health_kubectl import (
    k8s_cronjob_args,
    k8s_job_pods_args,
    k8s_jobs_args,
)
from lib import kubectl

CONTENT = "2ca4de5ca3037532ff2e1c787780516d6c6d5a4d0e8a4953d677c9ad1c171a2f"
CURRENT = CONTENT[:8]
EARLIER = "7fabd86c"
NS = "longhorn-system"
# The renderer's two stamps, `bundle.py:CONTENT_DIGEST` and `cronjob_gate.py:ROLE_DIGEST8`,
# spelled out so a typo in the probe's own copies fails here.
CONTENT_DIGEST = "homelab/content-digest"
ROLE_DIGEST8 = "homelab/role-digest8"
# The read `homelab-readonly` makes of Kustomization `longhorn-reconcile`.
KUSTOMIZATION_ARGS = (
    "-n",
    "flux-system",
    "get",
    "kustomizations.kustomize.toolkit.fluxcd.io",
    "longhorn-reconcile",
    "-o",
    "json",
)


def _gate(cronjob, digest8, created, name_digest, succeeded=1):
    """A gate Job as `<cronjob>-verify` applies it: ownerless, Flux-labelled, digest-stamped."""
    return {
        "metadata": {
            "name": f"{cronjob}-deploy-gate-{name_digest}",
            "namespace": NS,
            "creationTimestamp": created,
            "labels": {"kustomize.toolkit.fluxcd.io/name": f"{cronjob}-verify"},
        },
        "spec": {"template": {"metadata": {"annotations": {ROLE_DIGEST8: digest8}}}},
        "status": {"succeeded": succeeded, "failed": 0},
    }


def _firing(cronjob, name, created):
    return {
        "metadata": {
            "name": name,
            "namespace": NS,
            "creationTimestamp": created,
            "ownerReferences": [
                {"kind": "CronJob", "name": cronjob, "controller": True}
            ],
        },
        "status": {"succeeded": 1, "failed": 0},
    }


def _kustomization(content=CONTENT):
    annotations = {} if content is None else {CONTENT_DIGEST: content}
    return {"metadata": {"name": "longhorn-reconcile", "annotations": annotations}}


def _tools(jobs, kustomization):
    """A `lib.kubectl.Tools` serving prod's nodes, the hourly CronJob, `jobs` and its pods."""
    nodes = {
        "items": [{"metadata": {"name": n}} for n in ("daniel-box", "daniel-server")]
    }
    cronjob = {
        "metadata": {"name": "longhorn-reconcile", "namespace": NS},
        "spec": {"schedule": "17 * * * *"},
    }
    served = {
        ("get", "nodes", "-o", "json"): nodes,
        tuple(k8s_cronjob_args("longhorn-reconcile", NS)): cronjob,
        tuple(k8s_jobs_args(NS)): {"items": jobs},
    }
    if kustomization is not None:
        served[KUSTOMIZATION_ARGS] = kustomization
    for job in jobs:
        served[tuple(k8s_job_pods_args(job["metadata"]["name"], NS))] = pods(
            ("longhorn-reconcile", 0, None)
        )

    def run(argv, timeout):
        # kubectl_argv puts `<binary> --kubeconfig <path>` ahead of the arguments.
        doc = served.get(tuple(argv[3:]))
        if doc is None:
            return subprocess.CompletedProcess(
                argv, 1, "", 'Error from server (NotFound): not found "x"'
            )
        return subprocess.CompletedProcess(argv, 0, json.dumps(doc), "")

    return kubectl.Tools(
        run=run,
        find_tool=lambda name: "/usr/local/bin/kubectl",
        find_kubeconfig=lambda: Path("/tmp/kubeconfig"),
    )


def _verdict(jobs, kustomization):
    """`format_role_cronjob_health` over what `_fetch_cronjob` reads for a Flux-owned role."""
    fetched = health._fetch_cronjob(
        "longhorn-reconcile",
        "longhorn-reconcile",
        NS,
        "prod",
        _tools(jobs, kustomization),
    )
    checked = [(NS, "longhorn-reconcile", *fetched)]
    return health.format_role_cronjob_health("longhorn-reconcile", checked, NOW)


def test_flux_revision_picks_the_gate_job_carrying_the_applied_digest():
    jobs = {
        "items": [
            _gate("longhorn-reconcile", EARLIER, "2026-08-16T08:00:00Z", "aaaaaaaa"),
            _gate("longhorn-reconcile", CURRENT, "2026-08-16T11:31:00Z", "bbbbbbbb"),
        ]
    }
    revision = health_cronjob.flux_revision(
        _kustomization(), jobs, "longhorn-reconcile"
    )
    assert revision == health_cronjob.FluxRevision(
        CURRENT, "longhorn-reconcile-deploy-gate-bbbbbbbb"
    )


def test_flux_revision_ignores_another_cronjobs_gate_with_the_same_digest():
    """configarr's two CronJobs share the role digest, so the digest alone names no CronJob."""
    jobs = {"items": [_gate("other", CURRENT, "2026-08-16T11:31:00Z", "bbbbbbbb")]}
    revision = health_cronjob.flux_revision(
        _kustomization(), jobs, "longhorn-reconcile"
    )
    assert revision == health_cronjob.FluxRevision(CURRENT, None)


def test_flux_revision_has_no_digest_without_the_annotation():
    jobs = {
        "items": [_gate("longhorn-reconcile", CURRENT, "2026-08-16T11:31:00Z", "b" * 8)]
    }
    assert health_cronjob.flux_revision(
        _kustomization(None), jobs, "longhorn-reconcile"
    ) == health_cronjob.FluxRevision(None, None)
    assert health_cronjob.flux_revision(
        None, jobs, "longhorn-reconcile"
    ) == health_cronjob.FluxRevision(None, None)


def test_health_fails_when_the_newest_gate_job_predates_the_applied_revision():
    """The Verify-by of #5955: the gate and a later firing both ran an earlier revision."""
    jobs = [
        _gate("longhorn-reconcile", EARLIER, "2026-08-16T08:00:00Z", "aaaaaaaa"),
        _firing(
            "longhorn-reconcile", "longhorn-reconcile-29123457", "2026-08-16T11:17:00Z"
        ),
    ]
    text, code = _verdict(jobs, _kustomization())
    assert code == 1, text
    assert (
        f"no gate Job carries the revision Flux applies (homelab/role-digest8 {CURRENT})"
        in text
    )


def test_health_passes_on_the_gate_job_of_the_applied_revision():
    jobs = [
        _firing(
            "longhorn-reconcile", "longhorn-reconcile-29123457", "2026-08-16T11:17:00Z"
        ),
        _gate("longhorn-reconcile", CURRENT, "2026-08-16T11:31:00Z", "bbbbbbbb"),
    ]
    text, code = _verdict(jobs, _kustomization())
    assert code == 0, text
    assert (
        "longhorn-reconcile-deploy-gate-bbbbbbbb succeeded since the last deploy"
        in text
    )


def test_health_passes_on_a_firing_after_the_applied_revisions_gate():
    jobs = [
        _gate("longhorn-reconcile", CURRENT, "2026-08-16T11:00:00Z", "bbbbbbbb"),
        _firing(
            "longhorn-reconcile", "longhorn-reconcile-29123457", "2026-08-16T11:17:00Z"
        ),
    ]
    text, code = _verdict(jobs, _kustomization())
    assert code == 0, text
    assert "longhorn-reconcile-29123457 succeeded since the last deploy" in text


def test_health_fails_the_applied_revisions_gate_when_it_failed():
    jobs = [
        _gate(
            "longhorn-reconcile",
            CURRENT,
            "2026-08-16T11:31:00Z",
            "bbbbbbbb",
            succeeded=0,
        )
    ]
    jobs[0]["status"]["failed"] = 1
    text, code = _verdict(jobs, _kustomization())
    assert code == 1, text
    assert "longhorn-reconcile-deploy-gate-bbbbbbbb FAILED" in text


def test_health_fails_closed_when_the_kustomization_is_missing():
    jobs = [_gate("longhorn-reconcile", CURRENT, "2026-08-16T11:31:00Z", "bbbbbbbb")]
    text, code = _verdict(jobs, None)
    assert code == 1, text
    assert "carries no homelab/content-digest annotation" in text


def test_health_gates_configarr_on_its_own_gate_of_two_with_one_digest():
    """The Verify-by of #5950: configarr's two CronJobs share the role digest."""
    content = "fd6466a0dc5fd1d74b774343052e3ce65443ea61ecbfa295533ace060b39b472"
    gates = [
        _gate("configarr", content[:8], "2026-08-16T11:31:00Z", "a0bce752"),
        _gate("configarr-health", content[:8], "2026-08-16T11:32:00Z", "72dde612"),
    ]
    kustomization = {"metadata": {"annotations": {CONTENT_DIGEST: content}}}
    revision = health_cronjob.flux_revision(
        kustomization, {"items": gates}, "configarr"
    )
    assert revision.gate == "configarr-deploy-gate-a0bce752"
    text, code = health_cronjob.format_cronjob_health(
        "homelab/configarr",
        {"spec": {"schedule": "30 4 * * *"}},
        gates[0],
        pods(("configarr", 0, None)),
        revision,
        NOW,
    )
    assert code == 0, text
    assert "configarr-deploy-gate-a0bce752 succeeded since the last deploy" in text


def test_run_health_reads_the_revision_for_a_flux_owned_cronjob_role(capsys):
    """The production path: the inventory's `owner: flux` routes longhorn-reconcile here."""
    jobs = [_gate("longhorn-reconcile", EARLIER, "2026-08-16T08:00:00Z", "aaaaaaaa")]
    code = health.run_health(
        "longhorn-reconcile",
        served="prod",
        tools=_tools(jobs, _kustomization()),
        on_origin=lambda role: True,
        stale_reason=current_checkout,
    )
    assert code == 1
    assert "no gate Job carries the revision Flux applies" in capsys.readouterr().out
