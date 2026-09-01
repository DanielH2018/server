"""`probe.py health <svc>`: the post-deploy gate.

It exits 0 only when the workload is fully rolled out AND nothing restarted in the last 180s.
Both halves matter: readiness flips a Deployment to Available before a bad liveness probe starts
killing it, so a rollout check alone reports green on a crashlooping pod. An unreadable restart
time counts as recent, so the gate fails closed.
"""

from datetime import datetime, timezone

import probe_health


def _inspect(state, restarts=0):
    return [{"State": state, "RestartCount": restarts}]


def test_inspect_argv():
    assert probe_health.inspect_argv("jellyfin") == ["docker", "inspect", "jellyfin"]


def test_health_running_and_healthy_exits_zero():
    data = _inspect(
        {
            "Status": "running",
            "Health": {
                "Status": "healthy",
                "FailingStreak": 0,
                "Log": [{"Output": "ok\n"}],
            },
        }
    )
    text, code = probe_health.format_health(data, "jellyfin")
    assert code == 0
    assert "healthy" in text and "running" in text


def test_health_unhealthy_exits_one_and_shows_streak_and_last_log():
    data = _inspect(
        {
            "Status": "running",
            "Health": {
                "Status": "unhealthy",
                "FailingStreak": 3,
                "Log": [{"Output": "connection refused\n"}],
            },
        }
    )
    text, code = probe_health.format_health(data, "qbittorrent")
    assert code == 1
    assert "unhealthy" in text and "3" in text and "connection refused" in text


def test_health_no_healthcheck_running_exits_zero():
    text, code = probe_health.format_health(_inspect({"Status": "running"}), "valheim")
    assert code == 0
    assert "no healthcheck" in text


def test_health_exited_exits_one():
    text, code = probe_health.format_health(_inspect({"Status": "exited"}), "valheim")
    assert code == 1
    assert "exited" in text


def test_health_absent_and_undeclared_is_clean():
    """An undeclared name is a block tag or a typo — the one absence the notifier may skip."""
    text, code = probe_health.format_health([], "nope", declared=False)
    assert code == 1
    assert "not a declared service on any host" in text


def test_health_absent_but_declared_is_flagged():
    """The reject half. A service daniel-pi's inventory declares, with no container on the
    host, is a deploy that did not create it — and until 2026-09-01 it shared the undeclared
    case's "not found (not created" message, which the notifier skipped."""
    text, code = probe_health.format_health([], "wg-easy", declared=True)
    assert code == 1
    assert "MISSING" in text
    assert "not a declared service on any host" not in text


#
# `health` ran `docker inspect` unconditionally until 2026-08-16 and had been dead on both
# cluster nodes since the 2026-08-14 Docker retirement — neither has the binary, so it raised
# FileNotFoundError. Every case below is a way the k8s replacement could report healthy when it
# is not, which is the only direction that matters for a post-deploy gate.

_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


def _deploy(generation=1, observed=1, replicas=1, updated=1, ready=1, available=1):
    return {
        "metadata": {"generation": generation},
        "spec": {"replicas": replicas},
        "status": {
            "observedGeneration": observed,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
        },
    }


def _pods(*containers):
    """containers: (name, restart_count, finished_at_or_None)."""
    return {
        "items": [
            {
                "metadata": {"name": "svc-abc"},
                "status": {
                    "containerStatuses": [
                        {
                            "name": name,
                            "restartCount": count,
                            "lastState": (
                                {"terminated": {"finishedAt": finished}}
                                if finished
                                else {}
                            ),
                        }
                        for name, count, finished in containers
                    ]
                },
            }
        ]
    }


def test_k8s_health_rolled_out_and_quiet_exits_zero():
    text, code = probe_health.format_k8s_health(
        _deploy(), _pods(("app", 0, None)), "freshrss", _NOW
    )
    assert code == 0
    assert "1/1 ready" in text


def test_k8s_health_missing_deployment_exits_one():
    text, code = probe_health.format_k8s_health(None, None, "nope", _NOW)
    assert code == 1
    assert "no Deployment" in text


def test_k8s_health_stale_generation_exits_one():
    """The controller has not observed the spec change yet, so the OLD pod is what is ready."""
    text, code = probe_health.format_k8s_health(
        _deploy(generation=5, observed=4), _pods(("app", 0, None)), "freshrss", _NOW
    )
    assert code == 1
    assert "not observed yet" in text


def test_k8s_health_incomplete_rollout_exits_one():
    text, code = probe_health.format_k8s_health(
        _deploy(replicas=2, updated=1, ready=1, available=1),
        _pods(("app", 0, None)),
        "freshrss",
        _NOW,
    )
    assert code == 1
    assert "rollout incomplete" in text


def test_k8s_health_recent_restart_exits_one_despite_being_ready():
    """The kube-state-metrics failure of 2026-08-07: a bad liveness probe passes READINESS,
    flips the Deployment to Available, and only then starts getting killed. Every
    readiness-derived field reads healthy while the pod crashloops."""
    just_now = "2026-08-16T11:59:30Z"
    text, code = probe_health.format_k8s_health(
        _deploy(), _pods(("app", 3, just_now)), "kube-state-metrics", _NOW
    )
    assert code == 1
    assert "RECENT RESTART" in text and "30s ago" in text


def test_k8s_health_old_restart_does_not_fail():
    """A pod that restarted last week and has been up since is healthy — restartCount alone
    would fail it forever."""
    last_week = "2026-08-09T12:00:00Z"
    text, code = probe_health.format_k8s_health(
        _deploy(), _pods(("app", 3, last_week)), "freshrss", _NOW
    )
    assert code == 0
    assert "restarts=3" in text


def test_k8s_health_unparseable_restart_timestamp_does_not_fail_open():
    """An unreadable finishedAt must count as RECENT, not as 'long ago'.

    Treating unknown as old is the one direction a gate must never fail. Reachable whenever
    kubectl's timestamp format shifts — fractional seconds, for instance, parse as None.
    """
    assert probe_health._seconds_since("not-a-timestamp", _NOW) is None
    assert probe_health._seconds_since(None, _NOW) is None

    text, code = probe_health.format_k8s_health(
        _deploy(), _pods(("app", 1, "2026-08-16T11:59:30.123456Z")), "freshrss", _NOW
    )
    assert code == 1
    assert "unreadable time" in text


def test_k8s_health_restart_with_no_laststate_fails_closed():
    """restartCount > 0 with no terminated state is still an unexplained restart."""
    text, code = probe_health.format_k8s_health(
        _deploy(), _pods(("app", 2, None)), "freshrss", _NOW
    )
    assert code == 1
    assert "unreadable time" in text


def test_k8s_health_checks_every_container_in_the_pod():
    """A sidecar crashlooping while the main container is fine still fails the gate."""
    just_now = "2026-08-16T11:59:00Z"
    text, code = probe_health.format_k8s_health(
        _deploy(), _pods(("app", 0, None), ("sidecar", 9, just_now)), "n8n", _NOW
    )
    assert code == 1
    assert "sidecar" in text


def _daemonset(generation=1, observed=1, desired=2, updated=2, ready=2, available=2):
    return {
        "kind": "DaemonSet",
        "metadata": {"generation": generation},
        "status": {
            "observedGeneration": observed,
            "desiredNumberScheduled": desired,
            "updatedNumberScheduled": updated,
            "numberReady": ready,
            "numberAvailable": available,
        },
    }


def test_k8s_health_reads_a_daemonset():
    """Six workloads here are DaemonSets — promtail, node-exporter, the crowdsec node agent.
    They carry the same four numbers under different status field names."""
    text, code = probe_health.format_k8s_health(
        _daemonset(), _pods(("app", 0, None)), "promtail", _NOW
    )
    assert code == 0
    assert "2/2 ready" in text


def test_k8s_health_daemonset_missing_a_node_exits_one():
    """Scheduled on 2 nodes, ready on 1 — a Deployment's readyReplicas would read 0 here, so
    the field mapping has to be per-kind rather than a shared default."""
    text, code = probe_health.format_k8s_health(
        _daemonset(ready=1, available=1), _pods(("app", 0, None)), "promtail", _NOW
    )
    assert code == 1
    assert "rollout incomplete" in text


def test_k8s_health_argv_can_ask_for_a_daemonset():
    assert "daemonset" in probe_health.k8s_deploy_argv(
        "promtail", "homelab", kind="daemonset"
    )


def test_k8s_health_argv_targets_the_named_namespace():
    assert probe_health.k8s_deploy_argv("freshrss", "homelab")[:4] == [
        "k3s",
        "kubectl",
        "-n",
        "homelab",
    ]
    assert "app=freshrss" in probe_health.k8s_pods_argv("freshrss", "homelab")


#
# A deploy tag names a ROLE, not a workload. Everything below covers the resolution step added
# 2026-09-01: for eleven roles the tag is not the name of the thing to health-check, and for
# four of them it names no workload at all — so `probe.py health <tag>` reported "no Deployment
# or DaemonSet" and `deploy_detach_notify.py` skipped it. PR #685 landed VERDICT: settled with
# claude-otel's gate never having run.
#


def _target(namespace, kind, name, workload, pods=None):
    return (namespace, kind, name, workload, pods)


def test_role_health_all_present_is_clean():
    text, code = probe_health.format_role_health(
        "claude-otel",
        [
            _target("observability", "Deployment", "grafana", _deploy(), _pods()),
            _target("observability", "Deployment", "loki", _deploy(), _pods()),
        ],
        _NOW,
    )
    assert code == 0
    assert "all 2 workloads healthy" in text
    assert "observability/grafana" in text


def test_role_health_absent_workload_is_flagged():
    """The safety-critical half. The role's manifests declare grafana; the cluster does not
    have it. That is a failed deploy, and it must NOT read as a skip."""
    text, code = probe_health.format_role_health(
        "claude-otel",
        [
            _target("observability", "Deployment", "grafana", None),
            _target("observability", "Deployment", "loki", _deploy(), _pods()),
        ],
        _NOW,
    )
    assert code == 1
    assert "MISSING" in text
    assert text.splitlines()[0].startswith(
        "claude-otel: 1 of 2 workloads FAILED the gate"
    )


def test_role_health_unhealthy_sibling_is_flagged():
    """A workload the tag is NOT named after still fails the role. karakeep-time-tagger
    crashlooping behind a healthy `karakeep` was invisible before the resolution step."""
    text, code = probe_health.format_role_health(
        "karakeep",
        [
            _target("homelab", "Deployment", "karakeep", _deploy(), _pods()),
            _target(
                "homelab",
                "Deployment",
                "karakeep-time-tagger",
                _deploy(ready=0, available=0),
                _pods(),
            ),
        ],
        _NOW,
    )
    assert code == 1
    assert "karakeep-time-tagger" in text.splitlines()[0]


def test_role_health_verdict_rides_the_first_line():
    """deploy_detach_notify reads `splitlines()[0]`, so a failure buried in the per-workload
    detail would be reported as the summary line's verdict instead."""
    text, _ = probe_health.format_role_health(
        "scrutiny",
        [
            _target("homelab", "Deployment", "scrutiny-web", None),
            _target("homelab", "Deployment", "scrutiny-influxdb", _deploy(), _pods()),
        ],
        _NOW,
    )
    assert "FAILED" in text.splitlines()[0]


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
    "loki-homelab": {"loki-homelab", "promtail"},
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


def _resolved():
    """{role: [(namespace, kind, name)]} for every k8s role the resolver handles."""
    import validate_k8s_manifests as validator

    roles = sorted(d.name for d in validator.K8S_ROLES.iterdir() if d.is_dir())
    out = {}
    for role in roles:
        targets = probe_health.role_workload_targets(role, _DEFAULT_NS)
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
    """The reject half of the pin above: these roles are listed BECAUSE the tag alone is not
    enough. One that became a plain single-workload role should leave the list rather than sit
    here asserting nothing."""
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
    """`config` is a block tag; glances and autoheal run only on daniel-pi. None sends
    run_health down the guess-the-name path, which is what lets --docker reach the Pi.

    Not wg-easy: it is a role on BOTH trees, and the resolver prefers the k8s one, matching
    run_health's own k8s-first ordering."""
    assert probe_health.role_workload_targets("config", _DEFAULT_NS) is None
    assert probe_health.role_workload_targets("glances", _DEFAULT_NS) is None
    assert probe_health.role_workload_targets("autoheal", _DEFAULT_NS) is None


def test_resolver_respects_the_validators_skip_roles():
    """seed-volume and image-builder render only with vars a CALLING role passes, so rendering
    them standalone produces stub-filled manifests. Widening past that boundary would invent
    workload names and report them MISSING."""
    import validate_k8s_manifests as validator

    for role in sorted(validator.SKIP_ROLES):
        assert probe_health.role_workload_targets(role, _DEFAULT_NS) is None, role


def test_statefulset_is_resolvable_and_lookupable():
    """No role deploys one today. Resolving a kind the kubectl lookup cannot ask for would make
    the first StatefulSet added read as MISSING, so the two sets have to agree."""
    assert "StatefulSet" in probe_health.WORKLOAD_KINDS
    assert probe_health.WORKLOAD_KINDS["StatefulSet"] == "statefulset"
    assert "statefulset" in probe_health.k8s_deploy_argv(
        "postgres", "homelab", kind="statefulset"
    )


#
# The pod query behind the restart half of the gate. A selector matching NO pods yields
# restarts=0 and an empty recent-restart list — byte-identical to a genuinely quiet workload —
# so a wrong selector makes that half silently inert rather than failing.
#


def _workload(name, selector):
    return {"metadata": {"name": name}, "spec": {"selector": {"matchLabels": selector}}}


def test_pod_selector_matches_a_workloads_own_labels():
    assert (
        probe_health.pod_selector(_workload("grafana", {"app": "grafana"}))
        == "app=grafana"
    )


def test_pod_selector_is_flagged_when_it_would_differ_from_the_name():
    """pihole-2's Deployment selects `app: pihole`. `app=pihole-2` matched no pods at all,
    and `app=pihole` matched BOTH piholes' — confirmed live 2026-09-01."""
    selector = probe_health.pod_selector(_workload("pihole-2", {"app": "pihole"}))
    assert selector == "app=pihole"
    assert selector != "app=pihole-2"


def test_pod_selector_falls_back_rather_than_matching_every_pod():
    """A workload with no selector is rejected by the k8s API, so this is unreachable — but an
    empty `-l` would query the whole namespace, which is worse than the old guess."""
    assert probe_health.pod_selector({}) == ""
    assert "app=pihole-2" in probe_health.k8s_pods_argv("pihole-2", "homelab", "")


def test_pods_argv_prefers_an_explicit_selector():
    argv = probe_health.k8s_pods_argv("pihole-2", "homelab", "app=pihole")
    assert "app=pihole" in argv and "app=pihole-2" not in argv


def _rendered_workloads():
    """(role, workload doc) for every Deployment/DaemonSet/StatefulSet in the tree."""
    validator, base, entries = probe_health._render_context()
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
                    and doc.get("kind") in probe_health.WORKLOAD_KINDS
                ):
                    yield role, doc


def test_every_rendered_workload_yields_a_usable_pod_selector():
    """The tree-wide pin. Nothing else connects a manifest's selector to the query the gate
    runs, and a selector matching nothing reads as a healthy quiet workload. An empty result
    here would send `kubectl get pods -l ''` at the whole namespace."""
    workloads = list(_rendered_workloads())
    assert len(workloads) > 50, len(workloads)
    for role, doc in workloads:
        name = (doc.get("metadata") or {}).get("name")
        assert probe_health.pod_selector(doc), f"{role}/{name} declares no matchLabels"


def test_a_workload_selecting_labels_other_than_its_own_name_still_exists():
    """The reject half. `app=<name>` was the assumption until 2026-09-01, and it is right for
    every workload but one — so a test asserting only that the selector is non-empty would pass
    just as well with the assumption back in place. This names the counter-example."""
    divergent = {
        (role, (doc.get("metadata") or {}).get("name"))
        for role, doc in _rendered_workloads()
        if probe_health.pod_selector(doc)
        != f"app={(doc.get('metadata') or {}).get('name')}"
    }
    assert ("pihole", "pihole-2") in divergent, divergent
    assert probe_health.declared_on_pi("wg-easy") is True
    assert probe_health.declared_on_pi("definitely-not-a-service") is False


def test_declared_on_pi_fails_closed_on_an_unreadable_inventory(monkeypatch, tmp_path):
    """Fail closed: an unreadable inventory must not turn a missing container into a skip."""
    monkeypatch.setattr(probe_health, "PI_HOST_VARS", tmp_path / "gone.yml")
    assert probe_health.declared_on_pi("wg-easy") is True
