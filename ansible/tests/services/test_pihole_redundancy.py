"""LAN DNS survives a Pi-hole deploy only while four properties hold.

Each of them fails green — the deploy succeeds and DNS goes down anyway:

  * one instance instead of two: the Service has a single backend and its Recreate gap is
    a LAN-wide DNS outage, which is the state this replaced;
  * both restarted at once: the shared manifests role fires restarts back to back and defers
    waiting to the end-of-batch drain, so a reintroduced `manifests_rollout` would take both
    down within a second of each other and the redundancy would be decorative;
  * both APPLIED at once, which is how it actually broke on 2026-09-28 (#2884): `kubectl apply -f
    <dir>/` applies every file in a directory in one request, both Deployments rendered into one
    file, and a `pihole/pihole` image-pin bump changed both pod templates in the same second. The
    controller Recreate-cycled both instances before any restart task ran, LAN DNS was down 52s,
    and both new pods failed their image pull against the resolver they had just replaced. The
    play reported failed=0. Sequencing the restarts cannot help, so the apply is sequenced too;
  * split across nodes: every VIP is announced from daniel-box only (marked PERMANENT in
    setup/k3s metallb-pool.yaml.j2), and with externalTrafficPolicy: Local a pod on the other
    node receives nothing, so half the capacity would silently serve no traffic.
"""

from lib import yaml_fast

from _k8s_render import rendered_docs
from _helpers import REPO as _REPO
from _helpers import load_tasks, render_expr, task_named
from _helpers import walk_tasks as _flatten_tasks


_TASKS = _REPO / "ansible/roles/k8s/pihole/tasks/main.yml"
_ROLL_ONE = _REPO / "ansible/roles/k8s/pihole/tasks/roll_one.yml"
_APPLY_INSTANCE_2 = _REPO / "ansible/roles/k8s/pihole/tasks/apply_instance_2.yml"
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
    """The pod-only `instance` label is what lets a task address one instance's pod.

    `kubectl exec deploy/<name>` resolves through spec.selector, which is `app: pihole` on both
    Deployments and can't carry a per-instance value (selector is immutable). The `instance` label
    is what lets tasks/main.yml and roll_one.yml address a specific instance's pod instead of
    whichever one the shared selector happens to pick.
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


def _roll_one_tasks() -> list[dict]:
    return list(_flatten_tasks(yaml_fast.safe_load(_ROLL_ONE.read_text())))


def test_the_shared_role_does_not_restart_pihole():
    """`manifests_rollout: ''` is what stops both instances restarting together."""
    for task in load_tasks(_TASKS):
        if task.get("ansible.builtin.include_role", {}).get("name") == "k8s/manifests":
            vars_ = task.get("vars", {})
            assert vars_.get("manifests_rollout") == "", (
                "pihole must set manifests_rollout: '' — otherwise the shared role restarts "
                "both instances back to back and defers waiting to the end-of-batch drain"
            )
            assert not vars_.get("manifests_extra_rollouts"), (
                "extra rollouts restart in a batch too; use the sequenced roll_one.yml instead"
            )
            return
    raise AssertionError("pihole no longer includes k8s/manifests")


def test_the_rollout_is_sequenced_per_instance():
    included = [
        task
        for task in load_tasks(_TASKS)
        if str(task.get("ansible.builtin.include_tasks", "")).endswith("roll_one.yml")
    ]
    assert included, "no per-instance roll_one.yml include — restarts are not sequenced"
    looped = included[0].get("loop")
    assert looped is not None, "the roll_one.yml include has no loop"
    assert set(looped) == INSTANCES, (
        f"roll_one.yml must cover both instances, got {looped}"
    )


def test_roll_one_restarts_then_waits_for_the_same_instance():
    """Deleting the `rollout status` wait from roll_one.yml — the one line that makes the
    restarts sequential rather than concurrent — must fail this test even though every other
    guard in this file still passes."""
    tasks = _roll_one_tasks()

    def _cmd(task: dict) -> str:
        return str(task.get("ansible.builtin.command", {}).get("cmd", ""))

    restart_idx = next(
        (i for i, t in enumerate(tasks) if "rollout restart" in _cmd(t)), None
    )
    status_idx = next(
        (i for i, t in enumerate(tasks) if "rollout status" in _cmd(t)), None
    )
    assert restart_idx is not None, "roll_one.yml is missing the rollout restart"
    assert status_idx is not None, "roll_one.yml is missing the rollout status wait"
    assert restart_idx < status_idx, "the wait must come after the restart"
    for idx in (restart_idx, status_idx):
        assert "pihole_instance" in _cmd(tasks[idx]), (
            "the restart and wait must target pihole_instance, not a hardcoded name"
        )


def test_roll_one_checks_sibling_readiness_before_restarting():
    """Restarting an instance with no ready sibling is a LAN-wide DNS outage (both
    Deployments use Recreate on a single-writer volume). Matching on `pihole_sibling_instance`
    specifically (not just any `instance=`) rules out a spurious pass where the check names
    the very instance about to be restarted instead of its sibling — that would check nothing.
    Requiring it to precede the restart also rules out a check that runs too late to matter."""
    tasks = _roll_one_tasks()

    def _cmd(task: dict) -> str:
        return str(task.get("ansible.builtin.command", {}).get("cmd", ""))

    ready_idx = next(
        (
            i
            for i, t in enumerate(tasks)
            if "pihole_sibling_instance" in _cmd(t) and "condition=Ready" in _cmd(t)
        ),
        None,
    )
    # The wait is only a gate while its timeout still fails the play: `command` fails on
    # rc != 0 unless something swallows it, and an unbounded wait never returns rc != 0.
    if ready_idx is not None:
        assert "--timeout=" in _cmd(tasks[ready_idx]) and not tasks[ready_idx].get(
            "ignore_errors"
        ), "the sibling-readiness wait must time out loudly, or it gates nothing"
    restart_idx = next(
        (i for i, t in enumerate(tasks) if "rollout restart" in _cmd(t)), None
    )
    assert ready_idx is not None, (
        "roll_one.yml must verify pihole_sibling_instance is ready before restarting — "
        "otherwise the first deploy (or a week-stale sibling) is a full DNS outage"
    )
    assert restart_idx is not None, "roll_one.yml is missing the rollout restart"
    assert ready_idx < restart_idx, (
        "the sibling-readiness check must run before the restart, not after — checking "
        "afterwards can no longer prevent the outage it exists to catch"
    )


def test_roll_one_skips_an_instance_this_run_just_created():
    assert any(
        "created" in str(t.get("when", ""))
        and "manifests_apply" in str(t.get("when", ""))
        for t in yaml_fast.safe_load(_ROLL_ONE.read_text())
    ), (
        "roll_one.yml must skip the restart+wait for a Deployment this run just created — "
        "restarting it races the initial rollout"
    )


# ── #2884: the APPLY is sequenced, not only the restart ──────────────────────────────────────

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
    rendered both Deployments back into `deployment.yaml` — the state that caused the outage —
    would satisfy a count-only check on documents while failing this one, and so would swapping
    which file carries which instance (`manifests_files` names `deployment.yaml`, so the swap
    would put instance 2 in the shared apply and instance 1 in the sequenced one)."""
    assert _deployment_source() == {
        "pihole": "deployment.yaml.j2",
        "pihole-2": "deployment-2.yaml.j2",
    }


def test_the_shared_apply_carries_instance_one_only():
    """`manifests_files` is the directory the shared role applies in one request. Naming
    `deployment-2.yaml` there would put both Deployments back in that request and re-open #2884
    with every other guard in this file still green."""
    for task in load_tasks(_TASKS):
        if task.get("ansible.builtin.include_role", {}).get("name") == "k8s/manifests":
            files = task.get("vars", {}).get("manifests_files", [])
            assert "deployment.yaml" in files, files
            assert "deployment-2.yaml" not in files, (
                "instance 2 must not be staged in the directory the shared role applies — "
                "`kubectl apply -f <dir>/` would roll both instances in one request again"
            )
            return
    raise AssertionError("pihole no longer includes k8s/manifests")


def test_instance_two_is_staged_outside_the_pruned_directory():
    """Its own directory, not a subdirectory of the one the shared prune owns.

    The shared role deletes every file in `<root>/pihole/` that `manifests_files` does not name
    and applies that directory in one request, so staging instance 2 there would be #2884 again
    plus a permanently `changed` prune item. Since #2899 the render is the shared role's, through
    `manifests_deferred_files`, so this reads the directory the shared role derives from pihole's
    `manifests_deferred_dir_name` rather than a fact pihole sets itself — and the apply has to
    read the same fact, or it would apply a directory the deploy never writes.
    """
    declared = next(
        task["vars"]
        for task in load_tasks(_TASKS)
        if task.get("ansible.builtin.include_role", {}).get("name") == "k8s/manifests"
    )
    assert declared["manifests_deferred_files"] == ["deployment-2.yaml"], declared
    dir_name = declared["manifests_deferred_dir_name"]

    select = task_named(
        load_tasks(_MANIFESTS_TASKS / "main.yml"),
        "Select the deferred render directory",
    )
    staged = render_expr(
        select["ansible.builtin.set_fact"]["manifests_deferred_dir"],
        k8s_dry_run=False,
        manifests_dest_dir=f"{MANIFEST_ROOT}/pihole",
        manifests_deferred_dir_name=dir_name,
    ).strip()
    assert staged.startswith(MANIFEST_ROOT + "/"), staged
    assert staged != f"{MANIFEST_ROOT}/pihole", staged
    assert not staged.startswith(f"{MANIFEST_ROOT}/pihole/"), staged

    apply_cmd = str(
        task_named(load_tasks(_APPLY_INSTANCE_2), "Apply pihole-2's Deployment")[
            "ansible.builtin.command"
        ]["cmd"]
    )
    assert "manifests_deferred_dir" in apply_cmd, apply_cmd


def test_the_deferred_render_is_what_triggers_instance_twos_apply():
    """A change to `deployment-2.yaml.j2` alone moves none of the shared role's three restart
    facts — it renders into another directory, so `manifests_render` never sees it. Without the
    fourth trigger the apply that carries instance 2 would simply not run, and the deploy would
    report success having shipped nothing (#2884, re-pointed at the shared register by #2899)."""
    when = str(task_named(load_tasks(_TASKS), "Roll the Pi-hole instances")["when"])
    assert "manifests_deferred_render" in when, when
    assert "pihole_k8s_instance_2_render" not in when, (
        "the role no longer renders instance 2 itself; reading its own stale register would "
        "leave a deployment-2.yaml.j2 change undeployed"
    )


def test_a_deferred_only_change_rolls_instance_two_alone():
    """A `deployment-2.yaml.j2` change moves instance 2's manifests only, so the fourth trigger
    must fire the `pihole-2` iteration and skip `pihole` (#2957). The include's `when` is
    evaluated per loop item, so this renders every clause once per instance."""
    task = task_named(load_tasks(_TASKS), "Roll the Pi-hole instances")
    unchanged = {"changed": False}
    context = {
        "k8s_no_mutate": False,
        "manifests_render": unchanged,
        "manifests_secret_render": unchanged,
        "manifests_image_changed": False,
        "manifests_deferred_render": {"changed": True},
    }

    def fires(instance: str, **overrides) -> bool:
        ctx = {**context, **overrides, "pihole_instance": instance}
        return all(
            render_expr("{{ " + clause + " }}", **ctx) for clause in task["when"]
        )

    assert task["loop_control"]["loop_var"] == "pihole_instance"
    assert fires("pihole-2"), "a deferred-only change must still roll instance 2"
    assert not fires("pihole"), "a deferred-only change must not restart instance 1"
    # Control: a change the shared apply carries still rolls both.
    assert fires("pihole", manifests_render={"changed": True})
    assert fires("pihole-2", manifests_render={"changed": True})


def test_instance_twos_bytes_reach_the_release_digest():
    """The point of moving the render into the shared role (#2899): the deferred file is stat'd
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


def _roll_one_index(fragment: str) -> int:
    for i, task in enumerate(_roll_one_tasks()):
        if fragment in str(task.get("name", "")):
            return i
    raise AssertionError(f"no task in roll_one.yml named like {fragment!r}")


def test_instance_two_is_applied_only_after_its_sibling_is_verified_serving():
    """The apply that rolls instance 2 sits between the sibling checks and the rollout wait.

    Ahead of the checks it is the 2026-09-28 outage again — instance 2's pod template changing
    while instance 1 may not be serving. After the wait it would roll instance 2 with nothing
    left to block on, so the play would report success before the second resolver came back.
    The `when` matters as much as the position: without it the include fires on the `pihole`
    iteration too and applies instance 2 first, before instance 1 has rolled at all."""
    apply_idx = _roll_one_index("Apply the second Pi-hole instance's Deployment")
    assert _roll_one_index("Verify the sibling instance is ready") < apply_idx
    assert _roll_one_index("is terminating") < apply_idx
    assert apply_idx < _roll_one_index("Wait for serving Pi-hole instance")

    task = _roll_one_tasks()[apply_idx]
    assert str(task["ansible.builtin.include_tasks"]).endswith("apply_instance_2.yml")
    assert str(task["when"]) == "pihole_instance == 'pihole-2'", task["when"]


def test_the_sibling_check_refuses_a_terminating_pod():
    """A terminating pod keeps phase `Running` and condition `Ready` for its whole grace period,
    so the readiness wait passed against a `pihole-2` pod that was already going away and the play
    restarted `pihole` into a full DNS outage (#2884).

    The accept/reject pair is over the real `failed_when`: an empty read (no pod carries a
    `deletionTimestamp`) must pass, and any timestamp at all must fail. A check that only ran the
    kubectl read without judging its output would satisfy the position assertion above and nothing
    else."""
    task = _roll_one_tasks()[_roll_one_index("is terminating")]
    cmd = str(task["ansible.builtin.command"]["cmd"])
    assert "pihole_sibling_instance" in cmd, cmd
    assert "deletionTimestamp" in cmd, cmd

    when = "{{ " + str(task["failed_when"]) + " }}"
    assert not render_expr(when, pihole_sibling_terminating={"stdout": ""}), (
        "a sibling with no terminating pod must pass"
    )
    assert render_expr(
        when, pihole_sibling_terminating={"stdout": "2026-09-28T17:35:59Z"}
    ), "a terminating sibling must fail the play, not be restarted around"
