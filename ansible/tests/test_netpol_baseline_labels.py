"""Guards the opt-in label the netpol-baseline policy selects on.

`roles/k8s/netpol-baseline` fences exactly the pods carrying `netpol-baseline: enforced` in
their POD TEMPLATE. Nothing else notices when that label goes missing: the probe job dials
only littlelink, so dropping the label from any of the other five leaves them accepting
connections from every pod in the cluster while the probe still prints four green lines.

Asserted against the RENDERED manifests, not a text scan, because the failure that matters is
the label sitting in `spec.selector.matchLabels` only — a file-level grep for the string passes
on exactly that broken shape, and a NetworkPolicy does not look at a Deployment's selector.

Run: uv run pytest ansible/tests/test_netpol_baseline_labels.py
"""

from __future__ import annotations

from _k8s_render import rendered_docs

# Slice 1 of the rollout: the six traefik-only leaf apps (docs/networkpolicy-default-deny.md).
# Adding a role here without labelling it — or labelling one without listing it — fails below.
SLICE_1_ROLES = {
    "bento-pdf",
    "littlelink",
    "speedtest",
    "healthchecks",
    "ical-proxy",
    "code-server",
}

# Slice 2: the media stack plus both push bridges. configarr is a CronJob, not a Deployment —
# its pod template lives two levels deeper, at spec.jobTemplate.spec.template, and
# _pod_template_labels() below has a CronJob-specific branch to reach it.
SLICE_2_ROLES = {
    "sonarr",
    "radarr",
    "prowlarr",
    "qbittorrent",
    "bazarr",
    "tdarr",
    "janitorr",
    "monitor-bridge",
    "autofix-bridge",
    "configarr",
}

# Leaf apps born fenced. A service added AFTER a slice shipped belongs to no slice — it was
# never part of that rollout — but it is the same shape slice 1 covers: Traefik is its only
# caller and it dials nothing itself, so labelling it at creation costs nothing and skips the
# unfenced window a later slice would have to close.
BORN_FENCED_ROLES = {
    # Serves each host's ~/.claude/artifacts read-only; makes no outbound connection at all.
    "artifacts",
}

LABEL = ("netpol-baseline", "enforced")

# Workloads inside a fenced role that are fenced by their OWN NetworkPolicy rather than by the
# baseline label. Each entry must name the policy that covers it: an unexplained exemption is
# indistinguishable from a workload someone forgot to label.
#
# flaresolverr: roles/k8s/prowlarr/templates/networkpolicy.yaml.j2 admits app=prowlarr only, on one
# port. That is TIGHTER than the baseline, which would also admit traefik, prometheus and the node
# CIDRs — so labelling it would widen the fence around a headless browser that renders
# attacker-supplied pages.
BESPOKE_POLICY_WORKLOADS = {
    ("prowlarr", "flaresolverr"),
}


def _pod_template_labels(doc: dict) -> dict:
    """The labels a Deployment or CronJob actually stamps onto its pods.

    A Deployment's pod template is spec.template. A CronJob's is one level deeper, at
    spec.jobTemplate.spec.template — its own spec.template does not exist, so reading that
    path the Deployment way would silently return {} for every CronJob.
    """
    spec = doc.get("spec") or {}
    if doc.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    return (spec.get("template") or {}).get("metadata", {}).get("labels", {})


def _labelled_roles() -> set[str]:
    key, value = LABEL
    return {
        role
        for role, _tpl, doc in rendered_docs()
        if _pod_template_labels(doc).get(key) == value
    }


def test_exactly_the_slice_1_and_slice_2_roles_carry_the_baseline_label() -> None:
    expected = SLICE_1_ROLES | SLICE_2_ROLES | BORN_FENCED_ROLES
    labelled = _labelled_roles()
    missing = sorted(expected - labelled)
    extra = sorted(labelled - expected)
    assert not missing and not extra, (
        "netpol-baseline: enforced no longer matches slice 1 + slice 2.\n"
        f"  missing (silently unfenced, and the probe would not notice): {missing}\n"
        f"  unexpected (fenced without a probe proving its callers still work): {extra}\n"
        "Update SLICE_1_ROLES/SLICE_2_ROLES together with docs/networkpolicy-default-deny.md "
        "when the rollout moves to the next slice."
    )


POD_KINDS = {"Deployment", "DaemonSet", "StatefulSet", "CronJob"}


def _labelled_workloads() -> set[tuple[str, str]]:
    key, value = LABEL
    return {
        (role, doc.get("metadata", {}).get("name", "?"))
        for role, _tpl, doc in rendered_docs()
        if doc.get("kind") in POD_KINDS and _pod_template_labels(doc).get(key) == value
    }


def test_every_pod_producing_doc_in_a_fenced_role_is_labelled() -> None:
    """A role is not a unit of fencing. claude-otel renders six workloads; five
    could go unlabelled while the role still looked fenced."""
    fenced_roles = SLICE_1_ROLES | SLICE_2_ROLES
    unlabelled = {
        (role, doc.get("metadata", {}).get("name", "?"))
        for role, _tpl, doc in rendered_docs()
        if role in fenced_roles
        and doc.get("kind") in POD_KINDS
        and _pod_template_labels(doc).get(LABEL[0]) != LABEL[1]
    }
    unexplained = sorted(
        f"{role}/{name}" for role, name in unlabelled - BESPOKE_POLICY_WORKLOADS
    )
    assert not unexplained, (
        "pod-producing docs inside a fenced role are missing the baseline label:\n"
        f"  {unexplained}\n"
        "Every workload in a fenced role must carry it — the role is not the unit. If this one is "
        "genuinely unfenced, label it. If it is already fenced by its own NetworkPolicy, add "
        "(role, name) to BESPOKE_POLICY_WORKLOADS above with a comment naming that policy file."
    )
