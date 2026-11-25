"""Every workload container drops its capabilities — asserted at the CONTAINER, not the pod.

A pod-level `securityContext` and a container-level one are different objects governing
different things: `runAsUser`/`fsGroup` are pod-level, while `capabilities`,
`allowPrivilegeEscalation` and `readOnlyRootFilesystem` only exist per container. A container
with no `securityContext` keeps the runtime's default capability set (CHOWN, DAC_OVERRIDE,
SETUID, SETGID, NET_RAW, ...) however locked-down the pod block looks.

A grep for `securityContext` across the fleet matches the pod block and reports a template
clean, so this check reads the container.

Nothing else enforces this: the cluster has no PodSecurity admission labels on any namespace,
so an absent container securityContext is simply honoured.

Rendering goes through validate.render_lib.k8s' own machinery rather than a second stub set, so
this cannot drift from what that validator considers a renderable manifest.
"""

from _helpers import K8S_ROLES
from _k8s_render import pod_spec, rendered_docs

_POD_KINDS = {"Deployment", "DaemonSet", "StatefulSet", "Job", "CronJob"}

# A privileged container is asked to drop no capabilities.
_PRIVILEGED = {
    # Raw USB access to the UPS; contained by a single-node pin and a loopback-only hostPort.
    ("nut", "nut"),
    ("dri-device-plugin", "generic-device-plugin"),
    ("scrutiny", "collector"),
}


# Roles this guard's corpus does not contain.
_UNCOVERED_ROLES = {
    # Builds images in-cluster; its Job carries reasoned Unconfined seccomp/AppArmor for
    # rootless BuildKit (build-job.yaml.j2). That reason covers only the two profiles, where
    # exemption here also waives uid, privileged, capabilities.add, hostPath and host
    # namespaces — so per the contract above it has its own test:
    # test_image_builder_security_context.py owns those.
    "image-builder",
    # No manifest templates — each resolves a fact or drives kubectl/the Longhorn API directly.
    "volume-snapshot",
    # Writes a row in the app's database: renders no manifest and starts no container.
    "arr-notification",
    # Only upstream's vendored CRD file; starts no container.
    "traefik-crds",
    # Workloads are upstream's vendored longhorn.yaml, which the fleet guards do not read.
    "longhorn",
}

# A floor of 40 cannot tell a broken collector from half the fleet dropping out.
_MIN_CONTAINERS = 90


def _containers():
    """(role, template, container name, securityContext) for every container in the fleet."""
    for role, tpl, doc in rendered_docs():
        if doc.get("kind") not in _POD_KINDS:
            continue
        pod = pod_spec(doc)
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
    all_roles = {p.parent.parent.name for p in K8S_ROLES.glob("*/defaults/main.yml")}
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


# Each member was measured live by cgroup or container id.

_LSIO_CHOWN_THEN_DROP = {
    ("bazarr", "bazarr"),
    ("code-server", "code-server"),
    ("freshrss", "freshrss"),
    ("home-assistant", "home-assistant"),
    ("jellyfin", "jellyfin"),
    ("lidarr", "lidarr"),
    ("prowlarr", "prowlarr"),
    ("qbittorrent", "qbittorrent"),
    ("radarr", "radarr"),
    ("sonarr", "sonarr"),
    ("speedtest", "speedtest"),
    ("tdarr", "tdarr"),
}

# The same shape without s6: the entrypoint starts as root and drops the app itself.
_ENTRYPOINT_DROPS = {
    # The LAPI plugin broker forks the notifier and drops it to uid 65534.
    ("crowdsec", "crowdsec"),
    # CouchDB chowns /opt/couchdb/data, then `setpriv`s to uid 5984.
    ("livesync", "couchdb"),
    # `start.sh` runs as 0 and hands off to `pihole-FTL` at 1000.
    ("pihole", "pihole"),
    # Entrypoint chowns the data dir then gosu-drops to 1000.
    ("scrutiny", "influxdb"),
    # s6-svscan at uid 0 like the LSIO set, but it does not drop.
    ("karakeep", "karakeep"),
    ("pihole", "unbound"),
    # Whether it tolerates starting unprivileged is a deploy test, not a template question.
    ("homepage", "homepage"),
}

# Root for host or network access that k8s cannot express any other way.
_HOST_OR_NETWORK_ROOT = {
    # DAC_READ_SEARCH over a wholesale /var/log hostPath; syslog and auth.log are syslog:adm 640.
    ("loki-homelab", "alloy"),
    ("crowdsec", "crowdsec-agent"),
    # NET_ADMIN for wg-quick and iptables, plus two pod sysctls.
    ("wg-easy", "wg-easy"),
    # NET_ADMIN + NET_RAW to insert the FORWARD drops for peer traffic to the cluster CIDRs.
    ("wg-easy", "peer-cluster-drop"),
    ("qbittorrent", "wireguard"),
    ("slskd", "wireguard"),
}

# Deliberately root, declared as such, to fix up ownership before the workload starts.
_ROOT_BY_DESIGN = {
    ("code-server", "seed-workspace-claim"),
    ("wg-easy", "config-chown"),
    ("traefik", "crowdsec-data-install"),
    ("authelia", "crowdsec-data-install"),
    # Copies the root-only hub tree into /etc/crowdsec world-readable for the non-root sidecar.
    ("traefik", "crowdsec-hub-install"),
    ("authelia", "crowdsec-hub-install"),
    # The server itself runs as uid 0, so root here is the owner writing its own files.
    ("valheim", "mods"),
}

# Each needs an fsGroup the role avoids, or a one-time chown, before it can assert.
_ROOT_OWNED_DATA = {
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
    # cscli writes allowlist and bouncer rows in the engine's root-owned SQLite file.
    ("crowdsec", "crowdsec-jobs"),
    ("crowdsec", "lapi-register"),
}

# A container that lands non-root only from its image's USER needs `runAsUser` pinned.
_NON_ROOT_BY_IMAGE_DEFAULT: set[tuple[str, str]] = set()

# The operator declined pinning a uid on these; root buys them nothing.
_UID_PIN_DECLINED = {
    ("littlelink", "littlelink"),
    ("peanut", "peanut"),
}

_UNMEASURED_SHORT_LIVED = {
    ("crowdsec", "config-install"),
    # A `nc` retry loop from the crowdsec image, whose default user is root.
    ("crowdsec", "wait-for-lapi"),
    ("homepage", "seed-config"),
    ("livesync", "seed-config"),
    ("n8n", "wait-for-broker"),
    ("peanut", "seed-config"),
    # A `curl` retry loop gating the web container on scrutiny-influxdb.
    ("scrutiny", "wait-for-influxdb"),
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
        pod = pod_spec(doc)
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
    """Containers that assert runAsNonRoot but pin no uid.

    Pure, for the same reason _root_offenders is: a guard over the live fleet is only ever observed
    passing.

    `runAsNonRoot` alone does not pin a uid — see the comment above `_NON_ROOT_BY_IMAGE_DEFAULT` —
    so a container that asserts it while relying on the image's own USER still passes admission
    under a different uid after a tag bump.
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
