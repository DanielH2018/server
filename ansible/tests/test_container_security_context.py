"""Every workload container drops its capabilities — asserted at the CONTAINER, not the pod.

A pod-level `securityContext` and a container-level one are different objects governing
different things: `runAsUser`/`fsGroup` are pod-level, while `capabilities`,
`allowPrivilegeEscalation` and `readOnlyRootFilesystem` only exist per container. A container
with no `securityContext` keeps the runtime's default capability set (CHOWN, DAC_OVERRIDE,
SETUID, SETGID, NET_RAW, ...) however locked-down the pod block looks.

That distinction is why this went unnoticed. A grep for `securityContext` across the fleet
matched the pod block and reported the templates clean, and a 2026-08-15 review recorded
securityContext as verified across all 48 deployment templates on that basis. Five templates
had no container block at all — loki-homelab plus four of claude-otel's — and one role
(claude-otel) was internally inconsistent, since its kube-state-metrics manifest did carry one.

Nothing else enforces this: the cluster has no PodSecurity admission labels on any namespace,
so an absent container securityContext is simply honoured.

Rendering goes through validate_k8s_manifests' own machinery rather than a second stub set, so
this cannot drift from what that validator considers a renderable manifest.
"""

from __future__ import annotations

from _helpers import K8S_ROLES
from _k8s_render import rendered_docs

_POD_KINDS = {"Deployment", "DaemonSet", "StatefulSet", "Job", "CronJob"}

# Containers that run `privileged: true`, each for a hardware reason k8s cannot express any
# other way. Asking a privileged container to drop capabilities is meaningless — privileged
# restores them — so these skip the two checks below and are allowlisted instead. That makes
# the allowlist the real guard: a NEW privileged container fails until it is added here with a
# justification, which is the decision worth forcing.
_PRIVILEGED = {
    # Raw USB access to the UPS; k8s has no equivalent of compose's `devices:`. Contained by a
    # single-node pin and a loopback-only hostPort. See roles/k8s/nut/CLAUDE.md.
    ("nut", "nut"),
    # Registers a gRPC socket in the kubelet's device-plugin dir and hands it device file
    # descriptors. It is privileged so the nine media workloads consuming `devic.es/dri` do
    # not have to be.
    ("dri-device-plugin", "generic-device-plugin"),
    # SMART reads: the Docker cap set plus a CharDevice hostPath is not enough under k8s, as
    # the device cgroup still refuses the open. Resolved as a deliberate trade 2026-08-10.
    ("scrutiny", "collector"),
}


# Roles this guard's corpus does NOT contain, each with the reason it is out.
#
# THE BLIND SPOT THIS PINS (2026-08-23): rendered_docs() filters on
# validate_k8s_manifests.SKIP_ROLES — a list maintained for a DIFFERENT purpose, namely what the
# manifest validator can render standalone. Coverage of this security guard was therefore a side
# effect of someone else's list. volume-claim is on it, and seed-pod.yaml.j2 runs `runAsUser: 0`
# with no container securityContext at all, so every assertion in this file passed while that pod
# was never examined. Pinning the exempt set turns a role joining it into a failure here instead
# of a silent contraction; anything added below needs a justification and, if it renders a pod
# spec, its own test.
_UNCOVERED_ROLES = {
    # Renders no manifests of its own — it is the shared apply/rollout machinery.
    "manifests",
    # Per-deploy state, not a service manifest set. seed-pod.yaml.j2 IS a pod spec and is
    # deliberately not covered here; test_seed_pod_security_context.py owns it.
    "volume-claim",
    # Builds images in-cluster; its Job carries reasoned Unconfined seccomp/AppArmor for
    # rootless BuildKit (build-job.yaml.j2). That reason covers only the two profiles, where
    # exemption here also waives uid, privileged, capabilities.add, hostPath and host
    # namespaces — so per the contract above it has its own test:
    # test_image_builder_security_context.py owns those (2026-08-23b review M17).
    "image-builder",
    # No manifest templates — each resolves a fact or drives kubectl/the Longhorn API directly.
    "rollout-drain",
    "cronjob-gate",
    "volume-snapshot",
    "longhorn-api",
    "volume-revert",
    # Dockerfiles and app config only — no Kubernetes objects at all.
    "n8n-images",
}

# The real container count is ~103. A floor of 40 cannot distinguish a broken collector from
# half the fleet dropping out, which is the failure this file exists to prevent. Kept a little
# below the live count so adding a workload never breaks the build, but close enough to notice
# a contraction.
_MIN_CONTAINERS = 90


def _pod_specs(doc: dict):
    spec = doc.get("spec", {})
    if doc["kind"] == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    return spec.get("template", {}).get("spec", {})


def _containers():
    """(role, template, container name, securityContext) for every container in the fleet."""
    for role, tpl, doc in rendered_docs():
        if doc.get("kind") not in _POD_KINDS:
            continue
        pod = _pod_specs(doc)
        for key in ("initContainers", "containers"):
            for container in pod.get(key) or []:
                yield (
                    role,
                    tpl,
                    container.get("name", "<unnamed>"),
                    container.get("securityContext") or {},
                )


def test_privileged_containers_are_allowlisted():
    privileged = {
        (role, name)
        for role, _tpl, name, sc in _containers()
        if sc.get("privileged") is True
    }
    assert privileged == _PRIVILEGED, (
        "the set of privileged containers changed — added: "
        f"{sorted(privileged - _PRIVILEGED)}, removed: {sorted(_PRIVILEGED - privileged)}. "
        "A privileged container holds every capability regardless of what it drops, so each "
        "one needs a justification recorded in _PRIVILEGED."
    )


def test_every_container_drops_all_capabilities():
    offenders = []
    seen = 0

    for role, tpl, name, sc in _containers():
        if (role, name) in _PRIVILEGED:
            continue
        seen += 1
        dropped = (sc.get("capabilities") or {}).get("drop") or []
        if "ALL" not in [str(d).upper() for d in dropped]:
            offenders.append(f"{role}/{tpl}:{name}")

    assert seen > 40, (
        f"only inspected {seen} containers — the collector stopped matching"
    )
    assert seen >= _MIN_CONTAINERS, (
        f"only inspected {seen} containers, expected at least {_MIN_CONTAINERS} — coverage "
        "shrank. A floor far below the real count cannot tell 'the collector broke' from "
        "'half the fleet stopped being rendered'."
    )
    assert not offenders, (
        "these containers keep the runtime's default capability set, because a pod-level "
        "securityContext does not grant one: " + ", ".join(sorted(offenders))
    )


def test_the_corpus_covers_every_role_except_a_named_set():
    """Coverage is asserted, not assumed — see _UNCOVERED_ROLES."""
    all_roles = {p.parent.parent.name for p in K8S_ROLES.glob("*/tasks/main.yml")}
    covered = {role for role, _, _ in rendered_docs()}
    unexpected = (all_roles - covered) - _UNCOVERED_ROLES
    assert not unexpected, (
        "these roles are not in the security corpus and are not declared uncovered, so their "
        "containers are unchecked: " + ", ".join(sorted(unexpected))
    )
    stale = _UNCOVERED_ROLES - all_roles
    assert not stale, "_UNCOVERED_ROLES names roles that no longer exist: " + ", ".join(
        sorted(stale)
    )


# Containers that do NOT assert `runAsNonRoot: true`, grouped by the mechanism that stops them.
#
# The 2026-08-31 review reported this as a karakeep defect against "~10 other roles". Both halves
# were wrong: 38 of 50 roles carried no assertion, and the reviewer had inferred root from image
# names — karakeep-chrome was reported root and measured uid 1000, node-exporter was assumed to
# need root for host access and measured 65534. A census then measured every container live, by
# cgroup or container id rather than a `ps` name grep, and the sets below are its result.
#
# The allowlist is the guard, exactly as _PRIVILEGED above is: the 42 assertions that now exist
# are individually near-worthless restatements of measured facts, and collectively they are what
# makes this list small enough to read. A new container fails until it either asserts or is added
# here with a mechanism.

# LSIO images: PID 1 is `s6-svscan` as uid 0, which chowns /config and hands the app to uid 1000
# through `s6-supervise`. Traced three-hop per container, so the root half is measured, not
# inferred from the image name. `runAsNonRoot` refuses the pod at admission and the chown never
# runs — jellyfin's template records the failure it caused: `sed: couldn't open temporary file
# /config/sedUo9qXt: Permission denied`, which reads like a read-only mount and is not one.
_LSIO_CHOWN_THEN_DROP = {
    ("bazarr", "bazarr"),
    ("code-server", "code-server"),
    ("freshrss", "freshrss"),
    ("healthchecks", "healthchecks"),
    ("home-assistant", "home-assistant"),
    ("jellyfin", "jellyfin"),
    ("prowlarr", "prowlarr"),
    ("qbittorrent", "qbittorrent"),
    ("radarr", "radarr"),
    ("sonarr", "sonarr"),
    ("speedtest", "speedtest"),
    ("tdarr", "tdarr"),
}

# The same shape without s6: the entrypoint starts as root and drops the app itself.
_ENTRYPOINT_DROPS = {
    # The LAPI plugin broker forks the notifier and drops it to uid 65534 — observed live in the
    # same container.
    ("crowdsec", "crowdsec"),
    # CouchDB chowns /opt/couchdb/data, then `setpriv`s to uid 5984.
    ("livesync", "couchdb"),
    # `start.sh` runs as 0 and hands off to `pihole-FTL` at 1000. SETFCAP is the binding
    # constraint — FTL applies file capabilities to its own binary — not the privileged ports,
    # which NET_BIND_SERVICE already covers.
    ("pihole", "pihole"),
    # Entrypoint chowns the data dir then gosu-drops to 1000.
    ("scrutiny", "influxdb"),
    # s6-svscan at uid 0, like the LSIO set above, except this one does NOT drop: `node
    # dist/index.js` and next-server both measure uid 0 too. Filed as non-root by image default
    # on 2026-08-31 from a census summary, corrected the same day by measuring it.
    ("karakeep", "karakeep"),
    # Starts as root to read its config and drops to the unbound user — SETUID/SETGID are granted
    # for exactly that, and the template says so at the container. The census measured the settled
    # process at uid 101 and missed the entrypoint, which is the same steady-state-vs-PID-1 error
    # that mis-filed karakeep. Asserting here would refuse the container at admission.
    ("pihole", "unbound"),
    # su-exec drop. Mounts only an emptyDir and a read-only ConfigMap, so whether it tolerates
    # starting unprivileged is a deploy test rather than a template question.
    ("homepage", "homepage"),
}

# Root for host or network access that k8s cannot express any other way.
_HOST_OR_NETWORK_ROOT = {
    # DAC_READ_SEARCH over a wholesale /var/log hostPath; syslog and auth.log are syslog:adm 640.
    ("loki-homelab", "promtail"),
    ("crowdsec", "crowdsec-agent"),
    # NET_ADMIN for wg-quick and iptables, plus two pod sysctls.
    ("wg-easy", "wg-easy"),
    ("qbittorrent", "wireguard"),
}

# Deliberately root, declared as such, to fix up ownership before the workload starts.
_ROOT_BY_DESIGN = {
    ("code-server", "seed-workspace-claim"),
    ("wg-easy", "config-chown"),
}

# Runs as root over root-owned data. These are the ones with real work behind them: each needs an
# fsGroup the role currently avoids, or a one-time chown, before it can assert.
_ROOT_OWNED_DATA = {
    # No USER in the image, dumb-init at uid 0, /app/data root-owned. Its template avoids fsGroup
    # deliberately — it would recursively chown the PVC on every mount — so this needs a chown Job.
    ("uptime-kuma", "uptime-kuma"),
    # Root writing a SQLite DB + WAL in a root-owned PVC.
    ("scrutiny", "web"),
    # Data is uid-0-owned by explicit design; see the role's CLAUDE.md.
    ("valheim", "valheim"),
    # Stock nginx runs its master as root to bind :80 and fork `user nginx;` workers. texbrain
    # proves nginx-unprivileged runs 101 end to end on the same job, so an image swap plus a port
    # change would move this one.
    ("freshrss", "nginx"),
    # Measured uid 0 (`tini -- /bin/sh -c /bin/meilisearch`), writing a PVC it owns as root.
    ("karakeep", "meilisearch"),
    # Measured uid 0. Its uv cache is mounted at /root/.cache/uv, a hardcoded root HOME, so a
    # non-root uid cannot write it — the mount path is the blocker, not the data's ownership.
    ("karakeep", "time-tagger"),
}

# Emptied 2026-08-31, one deploy after it was written, by measuring every member instead of
# trusting the census summary that produced it. Seven were real and are now pinned and asserted in
# their templates. Three were never non-root at all — karakeep's app, meilisearch and time-tagger
# measure uid 0 — and moved to _ENTRYPOINT_DROPS and _ROOT_OWNED_DATA below.
#
# Kept as an empty named set rather than deleted, because the category is the thing worth
# remembering: a container that lands non-root only from its image's USER needs `runAsUser` pinned
# alongside the assertion, since `runAsNonRoot` alone makes the kubelet refuse a container whose
# image NAMES its user rather than numbering it. A future one belongs here until it is measured.
_NON_ROOT_BY_IMAGE_DEFAULT: set[tuple[str, str]] = set()

# The operator declined pinning a uid on these, in both templates, in the same words: doing it
# "would be a behaviour change smuggled in under a platform move". Root buys them nothing; that is
# not the question. Listed so a later census does not re-open a settled decision.
_UID_PIN_DECLINED = {
    ("littlelink", "littlelink"),
    ("peanut", "peanut"),
}

# Short-lived init and probe containers with no uid declared and no persistent process to measure.
# Unclassified rather than cleared: the census could not observe them, and guessing from an image
# name is the error that produced the finding this list came from.
_UNMEASURED_SHORT_LIVED = {
    ("crowdsec", "config-install"),
    # Added by the LAPI startup gate that landed in #675, one PR before this guard. Neither PR
    # could see the other: PR CI is scoped to changed files, so both were green and master was
    # not. It is a `nc` retry loop from the crowdsec image, whose default user is root — the same
    # image whose node-agent is allowlisted above under _HOST_OR_NETWORK_ROOT.
    ("crowdsec", "wait-for-lapi"),
    ("headlamp", "probe"),
    ("homepage", "seed-config"),
    ("karakeep", "wait-for-deps"),
    ("karakeep", "wait-for-karakeep"),
    ("livesync", "seed-config"),
    ("n8n", "probe"),
    ("n8n", "wait-for-broker"),
    ("netpol-baseline", "probe"),
    ("peanut", "seed-config"),
    ("prowlarr", "probe"),
    ("registry", "crane"),
    ("registry", "probe"),
    ("registry", "pulled"),
}

_ROOT_ALLOWED = (
    _LSIO_CHOWN_THEN_DROP
    | _ENTRYPOINT_DROPS
    | _HOST_OR_NETWORK_ROOT
    | _ROOT_BY_DESIGN
    | _ROOT_OWNED_DATA
    | _NON_ROOT_BY_IMAGE_DEFAULT
    | _UID_PIN_DECLINED
    | _UNMEASURED_SHORT_LIVED
)


def _containers_with_pod():
    """As _containers(), plus the pod-level securityContext each container inherits from."""
    for role, tpl, doc in rendered_docs():
        if doc.get("kind") not in _POD_KINDS:
            continue
        pod = _pod_specs(doc)
        pod_sc = pod.get("securityContext") or {}
        for key in ("initContainers", "containers"):
            for container in pod.get(key) or []:
                yield (
                    role,
                    tpl,
                    container.get("name", "<unnamed>"),
                    container.get("securityContext") or {},
                    pod_sc,
                )


def _asserts_non_root(container_sc: dict, pod_sc: dict) -> bool:
    """runAsNonRoot in effect — the container's own value, else the pod's."""
    if "runAsNonRoot" in container_sc:
        return container_sc["runAsNonRoot"] is True
    return pod_sc.get("runAsNonRoot") is True


def _root_offenders(rows, allowed):
    """Containers that neither assert non-root nor are excused. Pure, so it can be driven RED.

    Split out from the test below for exactly that reason: a guard over the live fleet is only
    ever observed passing, which is indistinguishable from a guard that fires on nothing. The
    synthetic pair beneath the real test is what proves this one can fail.
    """
    return [
        f"{role}/{tpl}:{name}"
        for role, tpl, name, sc, pod_sc in rows
        if (role, name) not in _PRIVILEGED
        and (role, name) not in allowed
        and not _asserts_non_root(sc, pod_sc)
    ]


def test_a_container_with_no_assertion_and_no_entry_is_flagged():
    """The rejecting half."""
    rows = [("newrole", "deployment.yaml.j2", "app", {}, {})]
    assert _root_offenders(rows, frozenset()) == ["newrole/deployment.yaml.j2:app"]


def test_an_asserting_container_and_an_allowlisted_one_are_clean():
    """The accepting half, both ways a container can legitimately pass."""
    rows = [
        # asserts at the container level
        ("a", "t.j2", "x", {"runAsNonRoot": True}, {}),
        # inherits the assertion from its pod
        ("b", "t.j2", "y", {}, {"runAsNonRoot": True}),
        # excused by name
        ("c", "t.j2", "z", {}, {}),
    ]
    assert _root_offenders(rows, frozenset({("c", "z")})) == []


def test_runasnonroot_false_does_not_count_as_an_assertion():
    """`runAsNonRoot: false` is a declaration that it MAY run as root, not an assertion."""
    rows = [("a", "t.j2", "x", {"runAsNonRoot": False}, {"runAsNonRoot": True})]
    assert _root_offenders(rows, frozenset()) == ["a/t.j2:x"]


def test_every_container_asserts_non_root_or_is_allowlisted():
    offenders = _root_offenders(_containers_with_pod(), _ROOT_ALLOWED)
    assert not offenders, (
        "these containers neither assert runAsNonRoot nor appear in _ROOT_ALLOWED, so nothing "
        "records whether they run as root deliberately: " + ", ".join(sorted(offenders))
    )


def _missing_uid_pin(rows):
    """Containers that assert runAsNonRoot but pin no uid. Pure, for the same reason
    _root_offenders is: a guard over the live fleet is only ever observed passing.

    `runAsNonRoot` alone does not pin a uid — see the comment above
    `_NON_ROOT_BY_IMAGE_DEFAULT` — so a container that asserts it while relying on the
    image's own USER still passes admission under a different uid after a tag bump.
    """
    return [
        f"{role}/{tpl}:{name}"
        for role, tpl, name, sc, pod_sc in rows
        if _asserts_non_root(sc, pod_sc)
        and sc.get("runAsUser", pod_sc.get("runAsUser")) is None
    ]


def test_an_assertion_with_no_uid_pin_is_flagged():
    """The rejecting half."""
    rows = [("a", "t.j2", "x", {"runAsNonRoot": True}, {})]
    assert _missing_uid_pin(rows) == ["a/t.j2:x"]


def test_an_assertion_with_a_pinned_uid_is_clean():
    """The accepting half, both ways a uid can be pinned: at the container or via the pod."""
    rows = [
        ("a", "t.j2", "x", {"runAsNonRoot": True, "runAsUser": 1000}, {}),
        ("b", "t.j2", "y", {"runAsNonRoot": True}, {"runAsUser": 1000}),
    ]
    assert _missing_uid_pin(rows) == []


def test_every_asserting_container_pins_a_uid():
    offenders = _missing_uid_pin(_containers_with_pod())
    assert not offenders, (
        "these containers assert runAsNonRoot but pin no runAsUser, so a tag bump that moves "
        "the image's named user to a different uid still passes admission silently: "
        + ", ".join(sorted(offenders))
    )


def test_the_root_allowlist_has_no_stale_entries():
    """An entry that stops matching a real container is a reason nobody can check any more.

    This is the half that makes the list above shrink rather than only grow: assert a container
    non-root and its entry must come out, or this fails.
    """
    live = {(role, name) for role, _tpl, name, _sc, _pod_sc in _containers_with_pod()}
    stale = _ROOT_ALLOWED - live
    assert not stale, (
        "_ROOT_ALLOWED names containers that no longer exist under those names: "
        + ", ".join(f"{r}/{n}" for r, n in sorted(stale))
    )
    asserting = {
        (role, name)
        for role, _tpl, name, sc, pod_sc in _containers_with_pod()
        if _asserts_non_root(sc, pod_sc)
    }
    contradictory = _ROOT_ALLOWED & asserting
    assert not contradictory, (
        "these containers assert runAsNonRoot AND are allowlisted as root — remove them from "
        "_ROOT_ALLOWED: " + ", ".join(f"{r}/{n}" for r, n in sorted(contradictory))
    )


def test_no_container_allows_privilege_escalation():
    offenders = [
        f"{role}/{tpl}:{name}"
        for role, tpl, name, sc in _containers()
        if (role, name) not in _PRIVILEGED
        and sc.get("allowPrivilegeEscalation") is not False
    ]
    assert not offenders, (
        "these containers can gain privileges through a setuid binary: "
        + ", ".join(sorted(offenders))
    )
