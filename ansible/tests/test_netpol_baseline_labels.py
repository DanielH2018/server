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

# Slice 3: the observability namespace. Named per WORKLOAD, not per role — all six render
# from the single claude-otel role, which is why Task 1 made the guard workload-granular.
SLICE_3_WORKLOADS = {
    ("claude-otel", "grafana"),
    ("claude-otel", "prometheus"),
    ("claude-otel", "loki"),
    ("claude-otel", "tempo"),
    ("claude-otel", "kube-state-metrics"),
    ("claude-otel", "otel-collector"),
}

SLICE_3_ROLES = {role for role, _name in SLICE_3_WORKLOADS}

# Slice 4: core infra (docs/networkpolicy-default-deny.md). Named per WORKLOAD, not per role: two
# roles here each render a second pod-producing doc the role-granular habit would miss — pihole
# (pihole-2, its HA pair, same app label, different instance) and crowdsec (crowdsec-node-agent,
# the DaemonSet). crowdsec-node-agent is labelled, not exempted: its only inbound caller is
# prometheus scraping it (verified live — count(up{job="crowdsec-node-agents"} == 1) == 2), and the
# baseline already admits prometheus on every port, so the baseline alone suffices. Its own calls
# out to the LAPI are outbound, which kube-router does not enforce and an ingress policy can't break.
SLICE_4_WORKLOADS = {
    ("traefik", "traefik"),
    ("authelia", "authelia"),
    ("crowdsec", "crowdsec"),
    ("crowdsec", "crowdsec-node-agent"),
    ("pihole", "pihole"),
    ("pihole", "pihole-2"),
    ("mosquitto", "mosquitto"),
    ("nut", "nut"),
    ("jellyfin", "jellyfin"),
}

SLICE_4_ROLES = {role for role, _name in SLICE_4_WORKLOADS}

# Slice 4.5 stage A: the roles that render more than one workload. A role is not the unit of
# fencing (test_every_pod_producing_doc_in_a_fenced_role_is_labelled below enforces that), so
# these roles are labelled WHOLE — labelling scrutiny-collector alone would fail that test on
# scrutiny-web and scrutiny-influxdb. Sub-workloads whose role name differs from the workload
# name are exactly where the slice-4 review caught an omission, so they are all named here.
SLICE_45A_WORKLOADS = {
    ("karakeep", "karakeep"),
    ("karakeep", "karakeep-chrome"),
    ("karakeep", "karakeep-meilisearch"),
    ("karakeep", "karakeep-time-tagger"),
    ("scrutiny", "scrutiny-web"),
    ("scrutiny", "scrutiny-influxdb"),
    ("scrutiny", "scrutiny-collector"),
    ("freshrss", "freshrss"),
    ("freshrss", "freshrss-feed-cache"),
    ("loki-homelab", "loki-homelab"),
    ("loki-homelab", "promtail"),
    ("cloudflare-ddns", "cloudflare-ddns-direct"),
    ("cloudflare-ddns", "cloudflare-ddns-proxied"),
    ("node-exporter", "node-exporter"),
    ("n8n", "n8n-runners"),
}

SLICE_45A_ROLES = {role for role, _name in SLICE_45A_WORKLOADS}

# Workloads inside a fenced role that are fenced by their OWN NetworkPolicy rather than by the
# baseline label. Each entry must name the policy that covers it: an unexplained exemption is
# indistinguishable from a workload someone forgot to label.
#
# flaresolverr: roles/k8s/prowlarr/templates/networkpolicy.yaml.j2 admits app=prowlarr only, on one
# port. That is TIGHTER than the baseline, which would also admit traefik, prometheus and the node
# CIDRs — so labelling it would widen the fence around a headless browser that renders
# attacker-supplied pages.
#
# n8n: roles/k8s/n8n/templates/networkpolicy.yaml.j2 (live name `n8n-broker`) fences it already.
# Slice 4.5 labels n8n-runners, which drags the whole n8n role into the fenced set — but labelling
# n8n itself would ADD the baseline's traefik + prometheus + node-CIDR allow-list on top of a
# tighter bespoke policy, widening it. Same reasoning as flaresolverr, and the same reasoning that
# keeps headlamp and registry out of slice 4.5 entirely.
BESPOKE_POLICY_WORKLOADS = {
    ("prowlarr", "flaresolverr"),
    ("n8n", "n8n"),
}

# Pod-producing docs that are deliberately unlabelled and deliberately NOT fenced: a netpol probe
# stands in for a compromised pod with no allow-list entry, so labelling one would make it prove
# nothing. This is a different category from BESPOKE_POLICY_WORKLOADS, which is "fenced by its own
# policy" — these are fenced by nothing, on purpose.
UNFENCED_BY_DESIGN_WORKLOADS = {
    ("prowlarr", "flaresolverr-netpol-probe"),
    ("sonarr", "sonarr-isolation-probe"),
    ("n8n", "n8n-netpol-probe"),
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


def test_exactly_the_fenced_roles_carry_the_baseline_label() -> None:
    expected = (
        SLICE_1_ROLES
        | SLICE_2_ROLES
        | SLICE_3_ROLES
        | SLICE_4_ROLES
        | SLICE_45A_ROLES
        | BORN_FENCED_ROLES
    )
    labelled = _labelled_roles()
    missing = sorted(expected - labelled)
    extra = sorted(labelled - expected)
    assert not missing and not extra, (
        "netpol-baseline: enforced no longer matches slice 1 + slice 2 + slice 3 + slice 4.\n"
        f"  missing (silently unfenced, and the probe would not notice): {missing}\n"
        f"  unexpected (fenced without a probe proving its callers still work): {extra}\n"
        "Update SLICE_1_ROLES/SLICE_2_ROLES/SLICE_3_WORKLOADS/SLICE_4_WORKLOADS together with "
        "docs/networkpolicy-default-deny.md when the rollout moves to the next slice."
    )


POD_KINDS = {"Deployment", "DaemonSet", "StatefulSet", "CronJob", "Job"}


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
    fenced_roles = (
        SLICE_1_ROLES | SLICE_2_ROLES | SLICE_3_ROLES | SLICE_4_ROLES | SLICE_45A_ROLES
    )
    unlabelled = {
        (role, doc.get("metadata", {}).get("name", "?"))
        for role, _tpl, doc in rendered_docs()
        if role in fenced_roles
        and doc.get("kind") in POD_KINDS
        and _pod_template_labels(doc).get(LABEL[0]) != LABEL[1]
    }
    exempt = BESPOKE_POLICY_WORKLOADS | UNFENCED_BY_DESIGN_WORKLOADS
    unexplained = sorted(f"{role}/{name}" for role, name in unlabelled - exempt)
    assert not unexplained, (
        "pod-producing docs inside a fenced role are missing the baseline label:\n"
        f"  {unexplained}\n"
        "Every workload in a fenced role must carry it — the role is not the unit. Pick one: "
        "label it; if it is already fenced by its own NetworkPolicy, add (role, name) to "
        "BESPOKE_POLICY_WORKLOADS above with a comment naming that policy file; if it is a probe "
        "that must stay unfenced to test the fence, add it to UNFENCED_BY_DESIGN_WORKLOADS."
    )


def test_exactly_the_slice_45a_workloads_carry_the_baseline_label() -> None:
    """Without this, SLICE_45A_WORKLOADS would be decorative — only the roles it derives are
    otherwise read, so a workload could be renamed or dropped and every other assertion here
    would still pass."""
    labelled = {
        (role, name)
        for (role, name) in _labelled_workloads()
        if role in SLICE_45A_ROLES
    }
    assert labelled == SLICE_45A_WORKLOADS, (
        "slice 4.5 stage A's labelled workloads no longer match SLICE_45A_WORKLOADS.\n"
        f"  labelled: {sorted(labelled)}\n"
        f"  expected: {sorted(SLICE_45A_WORKLOADS)}"
    )


def test_exactly_the_slice_3_workloads_carry_the_baseline_label() -> None:
    labelled_claude_otel = {
        name for (role, name) in _labelled_workloads() if role == "claude-otel"
    }
    expected = {name for _, name in SLICE_3_WORKLOADS}
    assert labelled_claude_otel == expected, (
        "claude-otel's labelled workloads no longer match SLICE_3_WORKLOADS.\n"
        f"  labelled: {sorted(labelled_claude_otel)}\n"
        f"  expected: {sorted(expected)}"
    )
