"""Guards for the opt-in live prune added by #1076.

`kubectl apply -f <dir>/` only adds/updates; it never removes a live object whose entry was
dropped from a role's `manifests_files`. `manifest-prune-check.sh` pages about that after the
fact. This is the in-place fix: `k8s/manifests` can pass `--prune -l homelab/role=<service>
--prune-allowlist=<kinds> -n <namespace>` to the apply, gated on `manifests_prune` (default
`false`, opt-in per role — see the DECIDED comment in
`ansible/roles/k8s/manifests/defaults/main.yml`).

Two things make a wrong prune catastrophic rather than merely wrong: pruning a Secret or a
PersistentVolumeClaim. Neither is recoverable the way a Deployment or a Service is (a re-apply
re-creates those; a re-apply of a lost Secret loses its rotated value, and a lost PVC loses
data). `test_no_role_prunes_secret_or_pvc_kinds` is the guard against either ever landing in a
role's `manifests_prune_kinds`, independent of whether that role got the label rollout right.

Run: uv run pytest ansible/tests/deploy/test_manifests_prune.py
"""

import re

from _helpers import K8S_ROLES, ROLES, jinja_env, load_tasks, task_named, walk_tasks
from lib import yaml_fast

_MANIFESTS_TASKS = ROLES / "k8s" / "manifests" / "tasks" / "main.yml"
_MANIFESTS_DEFAULTS = ROLES / "k8s" / "manifests" / "defaults" / "main.yml"

# Kinds that must never be pruned by this mechanism — see the module docstring.
_FORBIDDEN_PRUNE_KINDS = ("Secret", "PersistentVolumeClaim")


def _apply_cmd() -> str:
    task = task_named(load_tasks(_MANIFESTS_TASKS), "Apply manifests")
    return str(task["ansible.builtin.command"]["cmd"])


def _render(context: dict) -> str:
    return jinja_env().from_string(_apply_cmd()).render(**context)


_BASE_CONTEXT = {
    "manifests_dest_dir": "/etc/rancher/k3s/manifests/widget",
    "k8s_dry_run": False,
    "manifests_service": "widget",
    "k8s_namespace": "homelab",
}


def test_prune_flags_are_absent_when_not_armed() -> None:
    """The default (manifests_prune unset) — 63 of 64 k8s roles today — must never prune."""
    rendered = _render(_BASE_CONTEXT)
    assert "--prune" not in rendered


def test_prune_flags_are_absent_when_armed_with_no_kinds() -> None:
    """manifests_prune: true with an empty kinds list is still inert, not `--prune --all`.

    An empty --prune-allowlist would leave kubectl's own default allowlist in effect, which
    covers kinds a role never declared to this mechanism (Pod, ReplicationController) — the
    empty-kinds case must disable pruning outright, not silently widen it.
    """
    rendered = _render(
        {**_BASE_CONTEXT, "manifests_prune": True, "manifests_prune_kinds": []}
    )
    assert "--prune" not in rendered


def test_prune_flags_render_when_armed_with_kinds() -> None:
    rendered = _render(
        {
            **_BASE_CONTEXT,
            "manifests_prune": True,
            "manifests_prune_kinds": ["apps/v1/Deployment", "core/v1/Service"],
        }
    )
    assert "--prune " in rendered or rendered.rstrip().endswith("--prune")
    assert "-l homelab/role=widget" in rendered
    assert "--prune-allowlist=apps/v1/Deployment" in rendered
    assert "--prune-allowlist=core/v1/Service" in rendered
    assert "-n homelab" in rendered


def test_prune_allowlist_uses_one_flag_per_kind_not_a_comma_joined_value() -> None:
    """kubectl apply --prune-allowlist takes exactly one <group/version/kind> per flag.

    #1092 shipped it with the kinds comma-joined into ONE flag value
    (`--prune-allowlist=apps/v1/Deployment,core/v1/Service,...`), and kubectl parses the whole
    string as a single GroupVersionKind and rejects it outright: `error: invalid
    GroupVersionKind format: apps/v1/Deployment,core/v1/Service,...`. Every deploy of the one
    armed role (registry) failed at this task. Pinned against the exact comma-joined shape that
    used to read green here, so a regression back to `join(',')` fails this test instead of
    only failing a real deploy.
    """
    rendered = _render(
        {
            **_BASE_CONTEXT,
            "manifests_prune": True,
            "manifests_prune_kinds": [
                "apps/v1/Deployment",
                "core/v1/Service",
                "networking.k8s.io/v1/NetworkPolicy",
            ],
        }
    )
    assert "apps/v1/Deployment,core/v1/Service" not in rendered, (
        "the kinds are comma-joined into a single --prune-allowlist value again — kubectl "
        "rejects that as an invalid GroupVersionKind. See #1092's registry deploy failure."
    )
    assert rendered.count("--prune-allowlist=") == 3
    assert "--prune-allowlist=apps/v1/Deployment" in rendered
    assert "--prune-allowlist=core/v1/Service" in rendered
    assert "--prune-allowlist=networking.k8s.io/v1/NetworkPolicy" in rendered


def test_prune_selector_is_keyed_on_the_calling_role_not_a_constant() -> None:
    """A hardcoded role name in the selector would scope every armed role to one label."""
    context = {
        **_BASE_CONTEXT,
        "manifests_prune": True,
        "manifests_prune_kinds": ["core/v1/Service"],
    }
    widget = _render({**context, "manifests_service": "widget"})
    gadget = _render({**context, "manifests_service": "gadget"})
    assert "-l homelab/role=widget" in widget
    assert "-l homelab/role=gadget" in gadget
    assert "-l homelab/role=widget" not in gadget
    assert "-l homelab/role=gadget" not in widget


def _prune_kinds_by_role() -> dict[str, list[str]]:
    """manifests_prune_kinds as declared by every k8s role's tasks/main.yml, keyed by role."""
    declared: dict[str, list[str]] = {}
    for role in sorted(K8S_ROLES.iterdir()):
        main = role / "tasks" / "main.yml"
        if not main.is_file():
            continue
        for task in walk_tasks(load_tasks(main)):
            vars_ = task.get("vars")
            if isinstance(vars_, dict) and "manifests_prune_kinds" in vars_:
                declared[role.name] = list(vars_["manifests_prune_kinds"] or [])
    return declared


def test_no_role_prunes_secret_or_pvc_kinds() -> None:
    offenders = {
        role: [k for k in kinds if any(bad in k for bad in _FORBIDDEN_PRUNE_KINDS)]
        for role, kinds in _prune_kinds_by_role().items()
    }
    offenders = {role: bad for role, bad in offenders.items() if bad}
    assert not offenders, (
        f"these roles list a forbidden kind in manifests_prune_kinds: {offenders}. Pruning a "
        "Secret loses a rotated value with no re-apply to recover it from; pruning a "
        "PersistentVolumeClaim loses data. Neither kind may ever be pruned this way."
    )


def test_the_forbidden_kind_check_actually_fires() -> None:
    """Control: prove the matcher above rejects a Secret/PVC entry rather than passing vacuously."""
    poisoned = {"widget": ["apps/v1/Deployment", "core/v1/Secret"]}
    offenders = {
        role: [k for k in kinds if any(bad in k for bad in _FORBIDDEN_PRUNE_KINDS)]
        for role, kinds in poisoned.items()
    }
    assert offenders == {"widget": ["core/v1/Secret"]}


def test_registry_pilot_is_armed_and_labeled() -> None:
    """registry is the one role proving the mechanism end-to-end (see DECIDED comment).

    Every kind it lists in manifests_prune_kinds must have the matching label rendered in its
    own template, and pvc.yaml — deliberately excluded, see the module docstring — must stay
    out of the kinds list.
    """
    tasks = load_tasks(ROLES / "k8s" / "registry" / "tasks" / "main.yml")
    deploy = task_named(tasks, "Deploy the image registry")
    prune_vars = deploy["vars"]
    assert prune_vars["manifests_prune"] is True
    kinds = prune_vars["manifests_prune_kinds"]
    assert kinds, "registry no longer arms manifests_prune_kinds"
    assert not any("PersistentVolumeClaim" in k for k in kinds)

    templates = ROLES / "k8s" / "registry" / "templates"
    kind_to_template = {
        "Deployment": "deployment.yaml.j2",
        "Service": "service.yaml.j2",
        "NetworkPolicy": "networkpolicy.yaml.j2",
    }
    for kind in kind_to_template:
        assert any(kind in k for k in kinds), f"registry's kinds list is missing {kind}"

    for kind, template_name in kind_to_template.items():
        if not any(kind in k for k in kinds):
            continue
        text = (templates / template_name).read_text()
        assert re.search(r"homelab/role:\s*registry\b", text), (
            f"{template_name} is in manifests_prune_kinds but does not render the "
            "homelab/role label — its live object would be invisible to the --prune selector "
            "and would never be pruned, which is safe but means the pilot proves nothing for "
            "that kind."
        )

    # pvc.yaml deliberately carries no label and names no kind above — assert the omission is
    # still deliberate rather than merely forgotten, by checking the DECIDED comment is there.
    defaults_text = (ROLES / "k8s" / "manifests" / "defaults" / "main.yml").read_text()
    assert "DECIDED" in defaults_text and "manifests_prune" in defaults_text


def test_manifests_prune_defaults_off() -> None:
    all_vars = yaml_fast.safe_load(_MANIFESTS_DEFAULTS.read_text()) or {}
    assert all_vars["manifests_prune"] is False, (
        "manifests_prune must default false — every role that does not explicitly arm it must "
        "keep behaving exactly as before."
    )
    assert all_vars["manifests_prune_kinds"] == []
