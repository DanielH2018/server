"""LAN DNS survives a Pi-hole deploy only while four properties hold.

Each of them fails green — the deploy succeeds and DNS goes down anyway. Under Flux the order
of the two rolls is Kustomization order (`pihole-deferred` depends on `pihole`), which
ansible/roles/k8s/pihole/tests/test_pihole_gravity.py checks, and the sibling guard holds
`pihole` while pihole-2 serves nothing (test_pihole_precheck.py):

  * one instance instead of two: the Service has a single backend and its Recreate gap is
    a LAN-wide DNS outage;
  * both restarted at once: the shared manifests role fires restarts back to back and defers
    waiting to the end-of-batch drain, so a reintroduced `manifests_rollout` would take both
    down within a second of each other and the redundancy would be decorative;
  * both APPLIED at once: `kubectl apply -f <dir>/` applies every file in a directory in one
    request, so with both Deployments rendered into one file a `pihole/pihole` image-pin bump
    changes both pod templates in the same second. The controller Recreate-cycles both instances
    before any restart task runs, LAN DNS goes down, and both new pods fail their image pull
    against the resolver they just replaced. The play reports failed=0. Sequencing the restarts
    cannot help, so the apply is sequenced too;
  * split across nodes: every VIP is announced from daniel-box only (marked PERMANENT in
    setup/k3s metallb-pool.yaml.j2), and with externalTrafficPolicy: Local a pod on the other
    node receives nothing, so half the capacity would silently serve no traffic.
"""

from lib.k8s_roles import resolved_manifest_files

from _k8s_render import rendered_docs
from _helpers import REPO as _REPO
from _helpers import load_tasks, render_expr, task_named


_MANIFESTS_TASKS = _REPO / "ansible/roles/k8s/manifests/tasks"

INSTANCES = {"pihole", "pihole-2"}
CLAIM_BY_INSTANCE = {"pihole": "pihole-etc", "pihole-2": "pihole-etc-2"}


def _pihole_deployments() -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc
        for role, _tpl, doc in rendered_docs()
        if role == "pihole" and doc.get("kind") == "Deployment"
    }


def test_two_instances_are_rendered():
    assert set(_pihole_deployments()) == INSTANCES


def test_both_instances_are_selected_by_the_dns_service():
    """The DNS Service fronts both pods because both carry `app: pihole`, its only selector.

    The web Service additionally pins to `instance: pihole` — see
    test_only_the_web_service_is_pinned_to_one_instance — so it deliberately does NOT select
    pihole-2; this test covers DNS only.
    """
    dns_selector = next(
        doc["spec"]["selector"]
        for role, _tpl, doc in rendered_docs()
        if role == "pihole"
        and doc.get("kind") == "Service"
        and doc["metadata"]["name"] == "pihole-dns"
    )
    for name, dep in _pihole_deployments().items():
        labels = dep["spec"]["template"]["metadata"]["labels"]
        assert all(labels.get(k) == v for k, v in dns_selector.items()), (
            f"{name} is not selected by pihole-dns — it would take no DNS traffic"
        )


def test_instances_do_not_share_a_volume():
    """Assert the claim each instance mounts, not just that the two differ — swapping the
    claim names (moving pihole onto pihole-etc-2) would also pass a uniqueness-only check
    and put the live instance on a blank volume."""
    claim_by_instance = {}
    for name, dep in _pihole_deployments().items():
        for vol in dep["spec"]["template"]["spec"].get("volumes", []):
            if "persistentVolumeClaim" in vol:
                claim_by_instance[name] = vol["persistentVolumeClaim"]["claimName"]
    assert claim_by_instance == CLAIM_BY_INSTANCE, (
        f"expected {CLAIM_BY_INSTANCE}, got {claim_by_instance} — a swap would put the live "
        f"instance on a blank volume"
    )


def test_both_instances_pin_to_the_announcing_node():
    nodes = {
        name: dep["spec"]["template"]["spec"]
        .get("nodeSelector", {})
        .get("kubernetes.io/hostname")
        for name, dep in _pihole_deployments().items()
    }
    assert set(nodes.values()) == {"daniel-box"}, (
        f"every VIP is announced from daniel-box only and the Service is "
        f"externalTrafficPolicy: Local, so a pod elsewhere receives nothing. Got {nodes}"
    )


def test_pod_template_carries_a_per_instance_label():
    """The pod-only `instance` label is what lets a Service address one instance's pod.

    `kubectl exec deploy/<name>` resolves through spec.selector, which is `app: pihole` on both
    Deployments and can't carry a per-instance value (selector is immutable). The `instance` label
    is what lets a Service or a probe address a specific instance's pod instead of whichever one
    the shared selector happens to pick.
    """
    for name, dep in _pihole_deployments().items():
        assert dep["spec"]["template"]["metadata"]["labels"].get("instance") == name, (
            f"{name}'s pod template is missing its own instance label"
        )
        assert "instance" not in dep["spec"]["selector"]["matchLabels"], (
            "spec.selector is immutable in apps/v1 — adding `instance` there would make "
            "kubectl apply fail on the live Deployment"
        )


def _pihole_services() -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc
        for role, _tpl, doc in rendered_docs()
        if role == "pihole" and doc.get("kind") == "Service"
    }


def test_only_the_web_service_is_pinned_to_one_instance():
    """Ruling: the web UI pins to instance 1, DNS stays load-balanced across both.

    Pi-hole v6 keeps sessions in FTL memory, so the web UI cannot be balanced; each DNS query is
    stateless and redundancy is the point.
    """
    services = _pihole_services()
    assert services["pihole"]["spec"]["selector"].get("instance") == "pihole", (
        "the web Service must pin to instance 1 or admin sessions 401 at random between "
        "two independent FTLs"
    )
    assert "instance" not in services["pihole-dns"]["spec"]["selector"], (
        "the DNS Service must stay selecting both instances — pinning it defeats the "
        "redundancy this plan exists to add"
    )


# ── the APPLY is sequenced, not only the restart ─────────────────────────────────────────────

MANIFEST_ROOT = "/etc/rancher/k3s/manifests"


def _deployment_source() -> dict[str, str]:
    """Deployment name -> the template it is rendered from."""
    return {
        doc["metadata"]["name"]: tpl
        for _role, tpl, doc in rendered_docs()
        if _role == "pihole" and doc.get("kind") == "Deployment"
    }


def test_each_instance_is_rendered_from_its_own_template():
    """One file per instance is what lets the two be applied separately.

    Asserting the mapping rather than just "two distinct files" is deliberate: a change that
    rendered both Deployments back into `deployment.yaml` — the shared-apply shape —
    would satisfy a count-only check on documents while failing this one, and so would swapping
    which file carries which instance (`manifests_files` names `deployment.yaml`, so the swap
    would put instance 2 in the shared apply and instance 1 in the sequenced one)."""
    assert _deployment_source() == {
        "pihole": "deployment.yaml.j2",
        "pihole-2": "deployment-2.yaml.j2",
    }


def test_the_shared_apply_carries_instance_one_only():
    """`manifests_files` is the directory the shared role applies in one request. Naming
    `deployment-2.yaml` there would put both Deployments back in that request and re-open the
    simultaneous-apply outage with every other guard in this file still green."""
    files, _secret = resolved_manifest_files("pihole")
    assert "deployment.yaml" in files, files
    assert "deployment-2.yaml" not in files, (
        "instance 2 must not be staged in the directory the shared role applies — "
        "`kubectl apply -f <dir>/` would roll both instances in one request again"
    )


def test_instance_twos_bytes_reach_the_release_digest():
    """The point of rendering through the shared role: the deferred file is stat'd
    into `manifests_release_files`, so `manifests_digest` covers it and
    `probe.py releases --stale-only` can clear or catch a change to it. A render alone would not
    have done that — the digest loops a list, not a directory."""
    checksum = task_named(
        load_tasks(_MANIFESTS_TASKS / "release_digest.yml"),
        "Checksum the rendered deferred manifests",
    )
    assert checksum["loop"].strip() == "{{ manifests_deferred_files | default([]) }}"
    assert "manifests_deferred_dir" in checksum["ansible.builtin.stat"]["path"]

    files = task_named(
        load_tasks(_MANIFESTS_TASKS / "release_digest.yml"),
        "Digest the rendered manifests",
    )["ansible.builtin.set_fact"]["manifests_release_files"]
    stat_result = {
        "results": [{"item": "deployment-2.yaml", "stat": {"checksum": "beef"}}]
    }
    rendered = render_expr(
        files,
        manifests_release_stat={
            "results": [{"item": "deployment.yaml", "stat": {"checksum": "cafe"}}]
        },
        manifests_release_deferred_stat=stat_result,
        manifests_deferred_dir_name="pihole-instance-2",
    )
    # Keyed by directory, so instance 2's `deployment-2.yaml` can never collide with a file of
    # the same name in the shared directory — a collision that would read as a match.
    assert rendered == {
        "deployment.yaml": "cafe",
        "pihole-instance-2/deployment-2.yaml": "beef",
    }, rendered
    # The reject half: a role that defers nothing digests exactly what it did before.
    assert render_expr(
        files,
        manifests_release_stat={
            "results": [{"item": "deployment.yaml", "stat": {"checksum": "cafe"}}]
        },
        manifests_release_deferred_stat={"results": []},
        manifests_deferred_dir_name="",
    ) == {"deployment.yaml": "cafe"}
