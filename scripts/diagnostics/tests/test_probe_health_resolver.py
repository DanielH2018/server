"""`probe.py health <tag>`: which workloads a deploy tag resolves to, and their pod queries.

A deploy tag names a ROLE, not a workload. The mapping is rendered from the role's own
manifests rather than listed, so these tests are mostly a derived corpus over the whole k8s
tree — which is why the two full-tree renders here are `functools.cache`d and why every reader
of them lives in this one module (`--dist loadscope` keeps a module on one xdist worker).

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_resolver.py
"""

import collections
import functools

from diagnostics.probe_lib import health, health_kubectl

#
# The derived corpus. The role -> workload mapping is rendered from the manifests rather than
# listed, so a role that adds a workload is covered without anyone editing this file. What IS
# pinned is the two ways the mapping can silently SHRINK: a role dropping out of the
# multi-workload set, and a role joining the resolves-to-nothing set.
#

# Roles whose manifests declare no Deployment, DaemonSet or StatefulSet, each with what they
# declare instead. Membership here means `probe.py health <role>` legitimately has nothing to
# check, which the notifier skips — so a role arriving here by accident is a gate that stopped
# running, exactly the PR #685 failure. Verified against the rendered manifests 2026-09-01.
_ROLES_WITH_NO_WORKLOAD = {
    "configarr": "a CronJob and its Secret; the sync runs to completion, nothing stays up",
    "longhorn-ui": "an IngressRoute, Middleware and TLSOption onto longhorn-system's own UI",
    "media-volume": "a StorageClass, PV, PVC and a one-shot Job — storage, not a workload",
    "n8n-images": "only Dockerfiles — it delegates to image-builder and owns no manifest",
    "netpol-baseline": "NetworkPolicies plus the Job that probes them",
    "pi-peer-backup": "a CronJob, its PVC and its Secret",
}

# Roles where the tag is NOT the name of every workload to check. Pinned as a LOWER bound: the
# resolver must still return at least these names. Extra workloads are fine and need no edit
# here; a name disappearing is a workload that stopped being gated.
_MULTI_WORKLOAD_ROLES = {
    "claude-otel": {
        "grafana",
        "kube-state-metrics",
        "loki",
        "otel-collector",
        "prometheus",
        "tempo",
    },
    "cloudflare-ddns": {"cloudflare-ddns-direct", "cloudflare-ddns-proxied"},
    "crowdsec": {"crowdsec", "crowdsec-node-agent"},
    "freshrss": {"freshrss", "freshrss-feed-cache"},
    "karakeep": {
        "karakeep",
        "karakeep-chrome",
        "karakeep-meilisearch",
        "karakeep-time-tagger",
    },
    "loki-homelab": {"alloy", "loki-homelab"},
    "n8n": {"n8n", "n8n-runners"},
    "pihole": {"pihole", "pihole-2"},
    "prowlarr": {"flaresolverr", "prowlarr"},
    "scrutiny": {"scrutiny-collector", "scrutiny-influxdb", "scrutiny-web"},
}

# Workloads that do not live in the default namespace. `probe.py health` asked the default
# namespace for every name until 2026-09-01, so dri-device-plugin's DaemonSet — the only thing
# its role deploys — was unreachable and the gate skipped it.
_NON_DEFAULT_NAMESPACES = {
    "claude-otel": "observability",
    "dri-device-plugin": "kube-system",
}

_DEFAULT_NS = "homelab"


@functools.cache
def _resolved():
    """{role: [(namespace, kind, name)]} for every k8s role the resolver handles.

    Cached: this renders every role in the tree (~3s), six tests read it, and until 2026-09-01
    each paid for its own render. The tree does not change under a test run.
    """
    from validate import k8s_manifests as validator

    roles = sorted(d.name for d in validator.K8S_ROLES.iterdir() if d.is_dir())
    out = {}
    for role in roles:
        targets = health.role_workload_targets(role, _DEFAULT_NS)
        if targets is not None:
            out[role] = targets
    return out


def test_resolver_covers_the_whole_k8s_tree():
    """A resolver that returned None for everything would make every assertion below vacuous
    while leaving the gate on the guess-the-name path it is replacing."""
    resolved = _resolved()
    assert len(resolved) > 40, resolved.keys()
    assert "claude-otel" in resolved and "jellyfin" in resolved


def test_roles_with_no_workload_have_not_grown():
    resolved = _resolved()
    empty = {role for role, targets in resolved.items() if not targets}
    assert empty == set(_ROLES_WITH_NO_WORKLOAD), (
        "a role resolving to no workload is a role probe.py health cannot gate. Add it to "
        "_ROLES_WITH_NO_WORKLOAD with what it declares instead, or give it a workload."
    )


def test_multi_workload_roles_still_resolve_their_siblings():
    resolved = _resolved()
    for role, expected in _MULTI_WORKLOAD_ROLES.items():
        names = {name for _, _, name in resolved[role]}
        assert expected <= names, f"{role} lost {sorted(expected - names)}"


def test_multi_workload_roles_are_not_named_after_their_tag():
    """The reject half of the pin above: a multi-workload role is not named after its tag.

    These roles are listed BECAUSE the tag alone is not enough. One that became a plain
    single-workload role should leave the list rather than sit here asserting nothing.
    """
    resolved = _resolved()
    for role in _MULTI_WORKLOAD_ROLES:
        names = {name for _, _, name in resolved[role]}
        assert names != {role}, f"{role} now resolves to just its own name"


def test_workloads_outside_the_default_namespace_keep_their_own():
    resolved = _resolved()
    for role, namespace in _NON_DEFAULT_NAMESPACES.items():
        namespaces = {ns for ns, _, _ in resolved[role]}
        assert namespaces == {namespace}, f"{role}: {namespaces}"


def test_workloads_without_an_explicit_namespace_take_the_default():
    resolved = _resolved()
    assert {ns for ns, _, _ in resolved["jellyfin"]} == {_DEFAULT_NS}


def test_resolver_returns_none_for_a_tag_that_is_not_a_k8s_role():
    """`config` is a block tag; glances and autoheal run only on daniel-pi.

    None sends run_health down the guess-the-name path, which is what lets --docker reach the Pi.

    Not wg-easy: it is a role on BOTH trees, and the resolver prefers the k8s one, matching
    run_health's own k8s-first ordering.
    """
    assert health.role_workload_targets("config", _DEFAULT_NS) is None
    assert health.role_workload_targets("glances", _DEFAULT_NS) is None
    assert health.role_workload_targets("autoheal", _DEFAULT_NS) is None


def test_resolver_respects_the_validators_skip_roles():
    """volume-claim and image-builder render only with vars a CALLING role passes, so rendering
    them standalone produces stub-filled manifests. Widening past that boundary would invent
    workload names and report them MISSING."""
    from validate import k8s_manifests as validator

    for role in sorted(validator.SKIP_ROLES):
        assert health.role_workload_targets(role, _DEFAULT_NS) is None, role


def test_statefulset_is_resolvable_and_lookupable():
    """No role deploys one today.

    Resolving a kind the kubectl lookup cannot ask for would make the first StatefulSet added read
    as MISSING, so the two sets have to agree.
    """
    assert "StatefulSet" in health_kubectl.WORKLOAD_KINDS
    assert health_kubectl.WORKLOAD_KINDS["StatefulSet"] == "statefulset"
    assert "statefulset" in health_kubectl.k8s_deploy_argv(
        "postgres", "homelab", kind="statefulset"
    )


#
# The pod query behind the restart half of the gate. A selector matching NO pods yields
# restarts=0 and an empty recent-restart list — byte-identical to a genuinely quiet workload —
# so a wrong selector makes that half silently inert rather than failing.
#


def _workload(name, selector, template=None):
    spec = {"selector": {"matchLabels": selector}}
    if template is not None:
        spec["template"] = {"metadata": {"labels": template}}
    return {"metadata": {"name": name}, "spec": spec}


def test_pod_selector_matches_a_workloads_own_labels():
    assert (
        health_kubectl.pod_selector(_workload("grafana", {"app": "grafana"}))
        == "app=grafana"
    )


def test_pod_selector_is_flagged_when_it_would_differ_from_the_name():
    """A pod selector that would differ from the workload name must be flagged.

    pihole-2's Deployment selects `app: pihole`. `app=pihole-2` matched no pods at all, and
    `app=pihole` matched BOTH piholes' — confirmed live 2026-09-01.
    """
    selector = health_kubectl.pod_selector(_workload("pihole-2", {"app": "pihole"}))
    assert selector == "app=pihole"
    assert selector != "app=pihole-2"


def test_pod_selector_separates_two_instances_sharing_one_selector():
    """The accept half of the pihole fix (issue #802).

    `spec.selector` is immutable, so both pihole Deployments select `app: pihole` and neither can
    be given a discriminating selector label. The pod template carries `instance:` instead, and
    reading the template is what makes each instance's query match only its own pods.
    """
    shared = {"app": "pihole"}
    one = health_kubectl.pod_selector(
        _workload("pihole", shared, {"app": "pihole", "instance": "pihole"})
    )
    two = health_kubectl.pod_selector(
        _workload("pihole-2", shared, {"app": "pihole", "instance": "pihole-2"})
    )
    assert one == "app=pihole,instance=pihole"
    assert two == "app=pihole,instance=pihole-2"
    assert one != two


def test_pod_selector_is_flagged_when_it_reads_only_the_shared_selector():
    """The reject half.

    Reading `spec.selector.matchLabels` — what this did until 2026-09-02 — returns the same
    string for both instances, so each one's pod query matched the union of the two. A test that
    only asserted the selector is non-empty would pass just as well with that rule back in place.
    """
    shared = {"app": "pihole"}
    templates = [
        {"app": "pihole", "instance": "pihole"},
        {"app": "pihole", "instance": "pihole-2"},
    ]
    old_rule = {
        ",".join(f"{k}={v}" for k, v in sorted(shared.items())) for _ in templates
    }
    new_rule = {
        health_kubectl.pod_selector(_workload("pihole", shared, template))
        for template in templates
    }
    assert len(old_rule) == 1
    assert len(new_rule) == 2


def test_pod_selector_is_never_wider_than_the_workloads_own_selector():
    """k8s requires the template labels to be a superset of the selector, so the query this
    builds can only ever narrow. Asserting it directly keeps a future edit from widening it."""
    selector = {"app": "pihole"}
    template = {"app": "pihole", "instance": "pihole-2", "netpol-baseline": "enforced"}
    built = dict(
        pair.split("=", 1)
        for pair in health_kubectl.pod_selector(
            _workload("pihole-2", selector, template)
        ).split(",")
    )
    assert selector.items() <= built.items()


def test_pod_selector_falls_back_rather_than_matching_every_pod():
    """A workload with no selector is rejected by the k8s API, so this is unreachable — but an
    empty `-l` would query the whole namespace, which is worse than the old guess."""
    assert health_kubectl.pod_selector({}) == ""
    assert "app=pihole-2" in health_kubectl.k8s_pods_argv("pihole-2", "homelab", "")


def test_pods_argv_prefers_an_explicit_selector():
    argv = health_kubectl.k8s_pods_argv("pihole-2", "homelab", "app=pihole")
    assert "app=pihole" in argv and "app=pihole-2" not in argv


@functools.cache
def _rendered_workloads():
    """[(role, workload doc)] for every Deployment/DaemonSet/StatefulSet in the tree.

    Cached for the same reason as `_resolved`: one full render per worker, not one per test.
    """
    return list(_iter_rendered_workloads())


def _iter_rendered_workloads():
    validator, base, entries = health._render_context()
    for role_dir in sorted(d for d in validator.K8S_ROLES.iterdir() if d.is_dir()):
        role = role_dir.name
        if role in validator.SKIP_ROLES or role not in entries:
            continue
        ctx = {
            **base,
            **validator.role_defaults(role, base),
            "container_item": entries[role],
        }
        for tpl in sorted(
            p
            for p in (role_dir / "templates").glob("*.j2")
            if validator.is_manifest_template(p)
        ):
            err, docs = validator.check_template(role, tpl, ctx)
            assert not err, f"{role}/{tpl.name}: {err}"
            for doc in docs:
                if (
                    isinstance(doc, dict)
                    and doc.get("kind") in health_kubectl.WORKLOAD_KINDS
                ):
                    yield role, doc


def test_every_rendered_workload_yields_a_usable_pod_selector():
    """The tree-wide pin.

    Nothing else connects a manifest's selector to the query the gate runs, and a selector matching
    nothing reads as a healthy quiet workload. An empty result here would send `kubectl get pods -l
    ''` at the whole namespace.
    """
    workloads = list(_rendered_workloads())
    assert len(workloads) > 50, len(workloads)
    for role, doc in workloads:
        name = (doc.get("metadata") or {}).get("name")
        assert health_kubectl.pod_selector(doc), (
            f"{role}/{name} declares no matchLabels"
        )


def test_no_two_rendered_workloads_share_a_pod_selector():
    """The tree-wide pin for issue #802.

    Two workloads resolving to the same `-l` expression means each reads the union of both's
    pods, so a restart in either fails the gate for both. The unit tests above pin the pihole
    pair; this one catches the next role that grows a second instance off one pod template.
    """
    by_selector = collections.defaultdict(list)
    for role, doc in _rendered_workloads():
        name = (doc.get("metadata") or {}).get("name")
        by_selector[health_kubectl.pod_selector(doc)].append(f"{role}/{name}")
    shared = {sel: names for sel, names in by_selector.items() if len(names) > 1}
    assert not shared, shared


def test_a_workload_selecting_labels_other_than_its_own_name_still_exists():
    """The reject half.

    `app=<name>` was the assumption until 2026-09-01, and it is right for every workload but one —
    so a test asserting only that the selector is non-empty would pass just as well with the
    assumption back in place. This names the counter-example.
    """
    divergent = {
        (role, (doc.get("metadata") or {}).get("name"))
        for role, doc in _rendered_workloads()
        if health_kubectl.pod_selector(doc)
        != f"app={(doc.get('metadata') or {}).get('name')}"
    }
    assert ("pihole", "pihole-2") in divergent, divergent


#
# CronJob-only roles. configarr and pi-peer-backup declare no Deployment/DaemonSet/
# StatefulSet, only a CronJob -- until this gate existed, `probe.py health` reported "declares
# no rollout-checkable workload" for both, which deploy_detach_notify.py's
# NOT_APPLICABLE_MARKERS turns into a `skipped` verdict rather than a checked one. Neither
# role's post-deploy state was ever actually read outside the deploy-time k8s/cronjob-gate
# run. format_cronjob_health closes that gap.
#

_CRONJOB_ONLY_ROLES = frozenset({"configarr", "pi-peer-backup"})


def test_cronjob_only_census_finds_exactly_the_known_roles():
    """Non-vacuity pinned against a concrete set, not a lower bound -- this repo's own rule for
    a check that finds its own subject by pattern (KNOWN_CONSUMERS in test_probe_boundaries.py
    is the worked example). Equality rather than `>=` so a THIRD role gaining a CronJob is
    caught here too, pointing at
    ansible/tests/deploy/test_cronjob_only_roles_include_the_gate.py for whether it is wired up.
    """
    resolved = _resolved()
    cronjob_only = {
        role
        for role in resolved
        if not resolved[role] and health.role_cronjob_targets(role, _DEFAULT_NS)
    }
    assert cronjob_only == _CRONJOB_ONLY_ROLES


def test_the_no_rollout_checkable_workload_set_still_matches_outside_the_cronjob_roles():
    """The other four roles in `_ROLES_WITH_NO_WORKLOAD` (longhorn-ui, media-volume,
    n8n-images, netpol-baseline) still reach the unchanged skip message run_health prints --
    only configarr and pi-peer-backup were pulled onto the new CronJob path."""
    resolved = _resolved()
    no_workload = {role for role in resolved if not resolved[role]}
    assert (
        no_workload - _CRONJOB_ONLY_ROLES
        == set(_ROLES_WITH_NO_WORKLOAD) - _CRONJOB_ONLY_ROLES
    )
