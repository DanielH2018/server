"""Guards the opt-in label the netpol-baseline policy selects on.

`k8s/roles/netpol-baseline` fences exactly the pods carrying `netpol-baseline: enforced` in
their POD TEMPLATE. Nothing else notices when that label goes missing: the probe job dials
only littlelink, so dropping the label from any of the other five leaves them accepting
connections from every pod in the cluster while the probe still prints four green lines.

Asserted against the RENDERED manifests, not a text scan, because the failure that matters is
the label sitting in `spec.selector.matchLabels` only — a file-level grep for the string passes
on exactly that broken shape, and a NetworkPolicy does not look at a Deployment's selector.

Run: uv run pytest tests/k8s/edge/test_netpol_baseline_labels.py
"""

from _k8s_render import pod_template, rendered_docs
from _helpers import K8S_ROLES
from lib import yaml_fast

SLICE_1_ROLES = {
    "bento-pdf",
    "littlelink",
    "speedtest",
    "ical-proxy",
    "code-server",
}

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

BORN_FENCED_ROLES = {
    "airflow",
    # Serves each host's ~/.claude/artifacts read-only; makes no outbound connection at all.
    "artifacts",
    "docs",
    "texbrain",
    # Serves one static page it builds from Riot's API. Traefik is its only caller, and its calls
    # out are egress, which the baseline does not fence.
    "tft-assistant",
    "navidrome",
    "nut-exporter",
    "pihole-exporter",
    "karma",
    "gpu-exporter",
    "dri-device-plugin",
    "blackbox-exporter",
    "lidarr",
    "slskd",
    "vaultwarden",
    # Mirrors the vault from livesync's CouchDB, which is egress; nothing calls it yet.
    "vault-mcp",
}

LABEL = ("netpol-baseline", "enforced")

# Named per workload, not per role: every one renders from the single observability role.
SLICE_3_WORKLOADS = {
    ("observability", "grafana"),
    ("observability", "prometheus"),
    ("observability", "loki"),
    ("observability", "tempo"),
    ("observability", "kube-state-metrics"),
    ("observability", "otel-collector"),
    # The M1 alert plane's Alertmanager (ADR-0026).
    ("observability", "alertmanager"),
}

SLICE_3_ROLES = {role for role, _name in SLICE_3_WORKLOADS}

# Named per WORKLOAD, not per role: pihole-2 and crowdsec-node-agent are second pod-producing docs a
# role-granular habit would miss.
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

# Sub-workloads whose role name differs from the workload name are where an omission hides.
SLICE_45A_WORKLOADS = {
    ("karakeep", "karakeep"),
    ("karakeep", "karakeep-meilisearch"),
    ("karakeep", "karakeep-time-tagger"),
    ("karakeep", "karakeep-archive-pruner"),
    ("scrutiny", "scrutiny-web"),
    ("scrutiny", "scrutiny-influxdb"),
    ("scrutiny", "scrutiny-collector"),
    ("freshrss", "freshrss"),
    ("freshrss", "freshrss-feed-cache"),
    ("loki-homelab", "loki-homelab"),
    ("loki-homelab", "alloy"),
    ("cloudflare-ddns", "cloudflare-ddns-direct"),
    ("cloudflare-ddns", "cloudflare-ddns-proxied"),
    ("node-exporter", "node-exporter"),
    ("n8n", "n8n-runners"),
}

SLICE_45A_ROLES = {role for role, _name in SLICE_45A_WORKLOADS}

# Slice 4.5 stage B: the standalone apps, named per workload like stage A's.
SLICE_45B_WORKLOADS = {
    ("homepage", "homepage"),
    ("homelab-mcp", "homelab-mcp"),
    ("livesync", "livesync"),
    ("peanut", "peanut"),
    ("zigbee2mqtt", "zigbee2mqtt"),
    ("game-stats", "terraria-stats"),
    ("game-stats", "valheim-stats"),
    ("home-assistant", "home-assistant"),
}

SLICE_45B_ROLES = {role for role, _name in SLICE_45B_WORKLOADS}

# Fenced by a policy that leaves their game or VPN port OPEN (LoadBalancer, externalTrafficPolicy:
# Local);
# labelled so the baseline fences every OTHER port.
SLICE_45C_WORKLOADS = {
    ("terraria", "terraria"),
    ("valheim", "valheim"),
    ("wg-easy", "wg-easy"),
}

SLICE_45C_ROLES = {role for role, _name in SLICE_45C_WORKLOADS}

# Exempt workloads carry their own tighter policy; labelling one would ADD the baseline's allow-list
# on top and widen it.

# Deliberately unlabelled and unfenced: a netpol probe stands in for a compromised pod.
UNFENCED_BY_DESIGN_WORKLOADS: frozenset[tuple[str, str]] = frozenset()


def _pod_template_labels(doc: dict) -> dict:
    """The labels a Deployment or CronJob actually stamps onto its pods."""
    return (pod_template(doc).get("metadata") or {}).get("labels", {})


def _labelled_roles() -> set[str]:
    key, value = LABEL
    return {
        role
        for role, _tpl, doc in rendered_docs()
        if _pod_template_labels(doc).get(key) == value
    }


def test_exactly_the_fenced_roles_carry_the_baseline_label() -> None:
    # fact: k8s/roles/pihole-exporter/CLAUDE.md#NetworkPolicy
    expected = (
        SLICE_1_ROLES
        | SLICE_2_ROLES
        | SLICE_3_ROLES
        | SLICE_4_ROLES
        | SLICE_45A_ROLES
        | SLICE_45B_ROLES
        | SLICE_45C_ROLES
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
    """A role is not a unit of fencing.

    observability renders six workloads; five could go unlabelled while the role still looked fenced.
    """
    fenced_roles = (
        SLICE_1_ROLES
        | SLICE_2_ROLES
        | SLICE_3_ROLES
        | SLICE_4_ROLES
        | SLICE_45A_ROLES
        | SLICE_45B_ROLES
        | SLICE_45C_ROLES
    )
    unlabelled = {
        (role, doc.get("metadata", {}).get("name", "?"))
        for role, _tpl, doc in rendered_docs()
        if role in fenced_roles
        and doc.get("kind") in POD_KINDS
        and _pod_template_labels(doc).get(LABEL[0]) != LABEL[1]
    }
    exempt = _exempt_workloads() | UNFENCED_BY_DESIGN_WORKLOADS
    unexplained = sorted(f"{role}/{name}" for role, name in unlabelled - exempt)
    assert not unexplained, (
        "pod-producing docs inside a fenced role are missing the baseline label:\n"
        f"  {unexplained}\n"
        "Every workload in a fenced role must carry it — the role is not the unit. Pick one: "
        "label it; if it is already fenced by its own NetworkPolicy, give it the "
        "netpol-baseline-exempt label and list it in netpol_baseline_exempt_workloads; if it is a probe "
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


def test_exactly_the_slice_45b_workloads_carry_the_baseline_label() -> None:
    labelled = {
        (role, name)
        for (role, name) in _labelled_workloads()
        if role in SLICE_45B_ROLES
    }
    assert labelled == SLICE_45B_WORKLOADS, (
        "slice 4.5 stage B's labelled workloads no longer match SLICE_45B_WORKLOADS.\n"
        f"  labelled: {sorted(labelled)}\n"
        f"  expected: {sorted(SLICE_45B_WORKLOADS)}"
    )


def test_exactly_the_slice_45c_workloads_carry_the_baseline_label() -> None:
    labelled = {
        (role, name)
        for (role, name) in _labelled_workloads()
        if role in SLICE_45C_ROLES
    }
    assert labelled == SLICE_45C_WORKLOADS, (
        "slice 4.5 stage C's labelled workloads no longer match SLICE_45C_WORKLOADS.\n"
        f"  labelled: {sorted(labelled)}\n"
        f"  expected: {sorted(SLICE_45C_WORKLOADS)}"
    )


def test_exactly_the_slice_3_workloads_carry_the_baseline_label() -> None:
    labelled_observability = {
        name for (role, name) in _labelled_workloads() if role == "observability"
    }
    expected = {name for _, name in SLICE_3_WORKLOADS}
    assert labelled_observability == expected, (
        "observability's labelled workloads no longer match SLICE_3_WORKLOADS.\n"
        f"  labelled: {sorted(labelled_observability)}\n"
        f"  expected: {sorted(expected)}"
    )


# The baseline fences every pod that does not carry `netpol-baseline-exempt`.
EXEMPT_LABEL = ("netpol-baseline-exempt", "true")

_DEFAULTS = yaml_fast.safe_load(
    (K8S_ROLES / "netpol-baseline" / "defaults" / "main.yml").read_text()
)
EXEMPT_NAMES = set(_DEFAULTS["netpol_baseline_exempt_workloads"])


def _exempt_workloads() -> set[tuple[str, str]]:
    key, value = EXEMPT_LABEL
    return {
        (role, doc.get("metadata", {}).get("name", "?"))
        for role, _tpl, doc in rendered_docs()
        if doc.get("kind") in POD_KINDS
        and str(_pod_template_labels(doc).get(key, "")).lower() == value
    }


def test_the_exempt_default_names_a_known_member() -> None:
    """Non-vacuity: an empty or renamed default would make the exact-set check below pass on
    two empty sets."""
    assert "karakeep-chrome" in EXEMPT_NAMES


def test_exactly_the_default_exempt_workloads_carry_the_exemption() -> None:
    """Under namespace scope this label is the ONLY way out of the fence.

    A missing entry is a workload about to be widened. An extra entry is a workload fenced by
    nothing at all — the more dangerous direction, and the one a grep for the label cannot tell
    apart from the safe one.
    """
    exempt = {name for _role, name in _exempt_workloads()}
    assert exempt == EXEMPT_NAMES, (
        "the workloads carrying netpol-baseline-exempt no longer match "
        "netpol_baseline_exempt_workloads in k8s/roles/netpol-baseline/defaults/main.yml.\n"
        f"  exempt:   {sorted(exempt)}\n"
        f"  expected: {sorted(EXEMPT_NAMES)}\n"
        "An extra entry is unfenced by everything; a missing one gets the baseline's allow-list "
        "added on top of its own tighter policy."
    )


def _namespaced(doc: dict) -> str:
    return doc.get("metadata", {}).get("namespace", "")


def unselected(workloads: list[tuple[str, dict]], policies: list[dict]) -> set[str]:
    """Names of the (name, pod-producing doc) workloads no policy's `matchLabels` selects.

    Only `matchLabels` selectors count. The baseline selects by `matchExpressions` on the
    exemption label, so it can never be the policy that covers an exempt workload.
    """
    missing = set()
    for name, doc in workloads:
        labels = _pod_template_labels(doc)
        if not any(
            _namespaced(policy) == _namespaced(doc)
            and (wanted := policy["spec"]["podSelector"].get("matchLabels"))
            and wanted.items() <= labels.items()
            for policy in policies
        ):
            missing.add(name)
    return missing


def test_every_exempt_workload_is_selected_by_its_own_policy() -> None:
    """An exempt workload no policy selects is open to every pod in the namespace."""
    # fact: k8s/roles/netpol-baseline/CLAUDE.md#netpol-baseline — default-deny NetworkPolicies for the cluster
    workloads = [
        (doc["metadata"]["name"], doc)
        for _role, _tpl, doc in rendered_docs()
        if doc.get("kind") in POD_KINDS and doc["metadata"]["name"] in EXEMPT_NAMES
    ]
    policies = [
        doc for _r, _t, doc in rendered_docs() if doc.get("kind") == "NetworkPolicy"
    ]
    assert {name for name, _doc in workloads} == EXEMPT_NAMES
    assert unselected(workloads, policies) == set()


def test_an_exempt_workload_only_the_baseline_selects_is_flagged() -> None:
    """The rejecting half: a namespace-wide matchExpressions selector covers nothing here."""
    pod = {
        "kind": "Deployment",
        "metadata": {"name": "chrome", "namespace": "homelab"},
        "spec": {"template": {"metadata": {"labels": {"app": "chrome"}}}},
    }
    baseline = {
        "metadata": {"namespace": "homelab"},
        "spec": {
            "podSelector": {
                "matchExpressions": [{"key": "x", "operator": "DoesNotExist"}]
            }
        },
    }
    own = {
        "metadata": {"namespace": "homelab"},
        "spec": {"podSelector": {"matchLabels": {"app": "chrome"}}},
    }
    elsewhere = {
        "metadata": {"namespace": "observability"},
        "spec": {"podSelector": {"matchLabels": {"app": "chrome"}}},
    }
    assert unselected([("chrome", pod)], [baseline, elsewhere]) == {"chrome"}
    assert unselected([("chrome", pod)], [baseline, own]) == set()


def test_no_workload_is_both_labelled_and_exempt() -> None:
    """The two labels are opposites, and nothing enforces that from the manifests alone.

    `netpol-baseline: enforced` selects a pod under label scope; `netpol-baseline-exempt`
    de-selects it under namespace scope. A pod carrying both is fenced today and unfenced the
    moment slice 5 lands, which is the hardest version of this to notice.
    """
    both = sorted(
        f"{role}/{name}" for role, name in _labelled_workloads() & _exempt_workloads()
    )
    assert not both, (
        f"workloads carry BOTH the baseline label and the exemption: {both}\n"
        "Pick one: the baseline fences it, or its own policy does."
    )


def test_only_traefik_and_homepage_reach_the_longhorn_ui() -> None:
    """Traefik serves the route; homepage's Longhorn widget reads `/v1/nodes` through it."""
    (policy,) = [
        doc
        for role, _tpl, doc in rendered_docs()
        if role == "longhorn"
        and doc.get("kind") == "NetworkPolicy"
        and doc["spec"]["podSelector"].get("matchLabels") == {"app": "longhorn-ui"}
    ]
    (rule,) = policy["spec"]["ingress"]
    peers = {
        (
            peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            peer["podSelector"]["matchLabels"]["app"],
        )
        for peer in rule["from"]
    }
    assert peers == {("homelab", "traefik"), ("homelab", "homepage")}
    assert rule["ports"] == [{"protocol": "TCP", "port": 8000}]
