"""Guards for the live prune.

`kubectl apply -f <dir>/` only adds/updates; it never removes a live object whose entry was
dropped from a role's `manifests_files`. `manifest-prune-check.sh` pages about that after the
fact. This is the in-place fix: `k8s/manifests` passes `--prune -l homelab/role=<service>
--prune-allowlist=<kinds> -n <namespace>` to the apply for a role that arms `manifests_prune`
(see the DECIDED comment in `ansible/roles/k8s/manifests/defaults/main.yml`).

An armed role renders through `ansible/templates/role-labelled.yaml.j2`, whose
`homelab_role_label` filter stamps the label onto every document (#3388). The selector filters
the apply as well as the prune, so a document without the label would silently stop being
applied.

Two things make a wrong prune catastrophic rather than merely wrong: pruning a Secret or a
PersistentVolumeClaim. Neither is recoverable the way a Deployment or a Service is (a re-apply
re-creates those; a re-apply of a lost Secret loses its rotated value, and a lost PVC loses
data). `test_the_allowlist_never_names_a_secret_or_a_claim` is the guard against either ever
landing in the fixed allowlist.

Run: uv run pytest ansible/tests/deploy/test_manifests_prune.py
"""

import sys

import pytest
from _role_census import role_dirs
from _helpers import (
    REPO,
    ROLES,
    jinja_env,
    load_tasks,
    task_named,
    walk_tasks,
)
from _k8s_render import render_role_template, rendered_texts
from lib import yaml_fast
from lib.k8s_roles import declared_manifest_files, manifest_template
from role_label import ROLE_LABEL, homelab_role_label

sys.path.insert(0, str(REPO / "ansible/roles/setup/k3s/files"))

from manifest_declares import declared_in

_MANIFESTS_TASKS = ROLES / "k8s" / "manifests" / "tasks" / "main.yml"
_MANIFESTS_DEFAULTS = ROLES / "k8s" / "manifests" / "defaults" / "main.yml"

# Kinds that must never be pruned by this mechanism — see the module docstring.
_FORBIDDEN_PRUNE_KINDS = ("Secret", "PersistentVolumeClaim")
# The armed roles, each added on purpose, so the census below fails on an unmeant arming.
_ARMED_ROLES = frozenset(
    {
        "registry",
        "bazarr",
        "littlelink",
        "texbrain",
        "navidrome",
        "qbittorrent",
        "terraria",
        "code-server",
        "n8n",
        "valheim",
        "scrutiny",
        "tdarr",
        "home-assistant",
        "game-stats",
        "uptime-kuma",
        "freshrss",
        "sonarr",
        "radarr",
        "zigbee2mqtt",
        "prowlarr",
        "karakeep",
        "livesync",
        "speedtest",
        "bento-pdf",
        "ical-proxy",
        "peanut",
        "janitorr",
        "deploy-ui",
        "artifacts",
        "docs",
        "nut-exporter",
        "pihole-exporter",
        "node-exporter",
        "jellyfin",
        "mosquitto",
        "cloudflare-ddns",
        "crowdsec",
        "configarr",
        "pi-peer-backup",
        "pihole",
        "loki-homelab",
        "wg-easy",
        "homepage",
        "gpu-exporter",
        "headlamp",
        "autofix-bridge",
        "monitor-bridge",
        "homelab-mcp",
        "media-volume",
        "netpol-baseline",
    }
)
# The three render tasks whose source the armed switch replaces with the labelling wrapper.
_RENDER_TASKS = (
    "Render manifests",
    "Render secret manifests",
    "Render volume claims",
)


def _defaults() -> dict:
    return yaml_fast.safe_load(_MANIFESTS_DEFAULTS.read_text()) or {}


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
    "manifests_prune_allowlist": ["apps/v1/Deployment", "core/v1/Service"],
}


def test_prune_flags_are_absent_when_not_armed() -> None:
    """The default (manifests_prune unset) must never prune."""
    rendered = _render(_BASE_CONTEXT)
    assert "--prune" not in rendered


def test_prune_flags_are_absent_when_armed_with_no_kinds() -> None:
    """manifests_prune: true with an empty allowlist is still inert, not `--prune --all`.

    An empty --prune-allowlist would leave kubectl's own default allowlist in effect, which
    covers kinds a role never declared to this mechanism (Pod, ReplicationController) — the
    empty case must disable pruning outright, not silently widen it.
    """
    rendered = _render(
        {**_BASE_CONTEXT, "manifests_prune": True, "manifests_prune_allowlist": []}
    )
    assert "--prune" not in rendered


def test_prune_flags_render_when_armed() -> None:
    rendered = _render({**_BASE_CONTEXT, "manifests_prune": True})
    assert "--prune " in rendered or rendered.rstrip().endswith("--prune")
    assert "-l homelab/role=widget" in rendered
    assert "--prune-allowlist=apps/v1/Deployment" in rendered
    assert "--prune-allowlist=core/v1/Service" in rendered
    assert "-n homelab" in rendered


def test_prune_allowlist_uses_one_flag_per_kind_not_a_comma_joined_value() -> None:
    """kubectl apply --prune-allowlist takes exactly one <group/version/kind> per flag.

    Kinds comma-joined into ONE flag value
    (`--prune-allowlist=apps/v1/Deployment,core/v1/Service,...`) are parsed by kubectl as a
    single GroupVersionKind and rejected outright: `error: invalid GroupVersionKind format:
    apps/v1/Deployment,core/v1/Service,...`, failing every deploy of an armed role. Rendered
    with the real allowlist, so a regression back to `join(',')` fails this test instead of
    only failing a real deploy.
    """
    allowlist = _defaults()["manifests_prune_allowlist"]
    rendered = _render(
        {
            **_BASE_CONTEXT,
            "manifests_prune": True,
            "manifests_prune_allowlist": allowlist,
        }
    )
    assert ",".join(allowlist[:2]) not in rendered, (
        "the kinds are comma-joined into a single --prune-allowlist value again — kubectl "
        "rejects that as an invalid GroupVersionKind. See #1092's registry deploy failure."
    )
    assert rendered.count("--prune-allowlist=") == len(allowlist)


def test_prune_selector_is_keyed_on_the_calling_role_not_a_constant() -> None:
    """A hardcoded role name in the selector would scope every armed role to one label."""
    context = {**_BASE_CONTEXT, "manifests_prune": True}
    widget = _render({**context, "manifests_service": "widget"})
    gadget = _render({**context, "manifests_service": "gadget"})
    assert "-l homelab/role=widget" in widget
    assert "-l homelab/role=gadget" in gadget
    assert "-l homelab/role=widget" not in gadget
    assert "-l homelab/role=gadget" not in widget


def _forbidden(kinds: list[str]) -> list[str]:
    return [k for k in kinds if k.rsplit("/", 1)[-1] in _FORBIDDEN_PRUNE_KINDS]


def test_the_allowlist_never_names_a_secret_or_a_claim() -> None:
    allowlist = _defaults()["manifests_prune_allowlist"]
    assert allowlist, "manifests_prune_allowlist is empty, so no armed role prunes"
    assert not _forbidden(allowlist), (
        f"manifests_prune_allowlist names {_forbidden(allowlist)}. Pruning a Secret loses a "
        "rotated value with no re-apply to recover it from; pruning a PersistentVolumeClaim "
        "loses data. Neither kind may ever be pruned this way."
    )


def test_the_forbidden_kind_check_actually_fires() -> None:
    """Control: prove the matcher above rejects a Secret/PVC entry rather than passing vacuously."""
    assert _forbidden(["apps/v1/Deployment", "core/v1/Secret"]) == ["core/v1/Secret"]
    assert _forbidden(["core/v1/PersistentVolumeClaim"]) == [
        "core/v1/PersistentVolumeClaim"
    ]


def _include_vars_by_role() -> dict[str, list[dict]]:
    """Every `vars:` block of every k8s role's tasks/main.yml, keyed by role."""
    found: dict[str, list[dict]] = {}
    for role in role_dirs():
        main = role / "tasks" / "main.yml"
        if not main.is_file():
            continue
        for task in walk_tasks(load_tasks(main)):
            vars_ = task.get("vars")
            if isinstance(vars_, dict):
                found.setdefault(role.name, []).append(vars_)
    return found


def test_no_role_widens_the_allowlist_or_keeps_a_kinds_list() -> None:
    """The allowlist is one list for every armed role, so no caller passes its own."""
    offenders = sorted(
        role
        for role, blocks in _include_vars_by_role().items()
        for vars_ in blocks
        if {"manifests_prune_allowlist", "manifests_prune_kinds"} & vars_.keys()
    )
    assert not offenders, (
        f"{offenders} pass their own prune kinds. The fixed manifests_prune_allowlist in "
        "k8s/manifests' defaults is the only list, so the Secret/claim guard covers every role."
    )


def test_the_armed_roles_are_the_named_pilots() -> None:
    armed = {
        role
        for role, blocks in _include_vars_by_role().items()
        if any(vars_.get("manifests_prune") is True for vars_ in blocks)
    }
    assert armed == _ARMED_ROLES, (
        f"armed roles changed: added {sorted(armed - _ARMED_ROLES)}, dropped "
        f"{sorted(_ARMED_ROLES - armed)}. Arming a role means clearing its unlabelled orphans "
        "first; update _ARMED_ROLES once that is done."
    )


@pytest.mark.parametrize("name", _RENDER_TASKS)
def test_an_armed_role_renders_through_the_labelling_wrapper(name: str) -> None:
    task = task_named(load_tasks(_MANIFESTS_TASKS), name)
    src = str(task["ansible.builtin.template"]["src"])
    inner = "/repo/roles/k8s/widget/templates/deployment.yaml.j2"
    context = {"playbook_dir": "/repo", "manifests_label_src": inner}
    armed = jinja_env().from_string(src).render(**context, manifests_prune=True)
    unarmed = jinja_env().from_string(src).render(**context, manifests_prune=False)
    assert armed.strip() == "/repo/templates/role-labelled.yaml.j2"
    assert unarmed.strip() == inner
    assert "manifests_label_src" in task["vars"]


def _render_src(name: str, **context) -> str:
    task = task_named(load_tasks(_MANIFESTS_TASKS), name)
    src = str(task["ansible.builtin.template"]["src"])
    return jinja_env().from_string(src).render(**context).strip()


def test_a_deferred_render_never_carries_the_callers_label() -> None:
    """pihole's instance-2 Deployment must stay invisible to `-l homelab/role=pihole`.

    The deferred directory is applied by the caller, outside the shared apply, so the caller's
    prune never declares its objects. A deferred document labelled with the caller's service
    would be a prune candidate and be deleted on every deploy of an armed role. The deferred
    render keeps the raw template, so its objects carry no `homelab/role` label at all. The
    control is the caller's own render, which the same armed context does route through the
    wrapper.
    """
    context = {
        "playbook_dir": "/repo",
        "manifests_service": "pihole",
        "item": "deployment-2.yaml",
        "manifests_label_src": "/repo/roles/k8s/pihole/templates/deployment.yaml.j2",
        "manifests_shared_defaults": {},
        "manifests_prune": True,
    }
    wrapper = "/repo/templates/role-labelled.yaml.j2"
    assert _render_src("Render manifests", **context) == wrapper
    assert _render_src("Render deferred manifests", **context) == (
        "/repo/roles/k8s/pihole/templates/deployment-2.yaml.j2"
    )


def _empty_files_guard_holds(**context) -> bool:
    task = task_named(
        load_tasks(_MANIFESTS_TASKS), "Check that a role arming the prune"
    )
    assert task["when"] == "manifests_prune | bool"
    env = jinja_env()
    return all(
        env.from_string("{{ " + cond + " }}").render(**context)
        for cond in task["ansible.builtin.assert"]["that"]
    )


def test_an_armed_role_naming_no_manifest_is_flagged() -> None:
    assert not _empty_files_guard_holds(manifests_files=[])


def test_an_armed_role_naming_a_manifest_is_clean() -> None:
    assert _empty_files_guard_holds(manifests_files=["deployment.yaml"])


def test_an_empty_render_is_flagged() -> None:
    with pytest.raises(ValueError, match="rendered no document"):
        homelab_role_label("# nothing rendered\n---\n", "widget")


def test_a_rendered_object_is_clean() -> None:
    out = homelab_role_label(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: w\n", "widget"
    )
    assert yaml_fast.safe_load(out)["metadata"]["labels"] == {ROLE_LABEL: "widget"}


def test_another_roles_label_is_flagged() -> None:
    held = f"kind: Service\nmetadata:\n  name: w\n  labels:\n    {ROLE_LABEL}: gadget\n"
    with pytest.raises(ValueError, match="already carries"):
        homelab_role_label(held, "widget")


def _env_value_line(value: str) -> str:
    text = homelab_role_label(
        f"kind: ConfigMap\nmetadata:\n  name: w\ndata:\n  v: {value}\n", "widget"
    )
    return next(line for line in text.splitlines() if line.startswith("  v:"))


@pytest.mark.parametrize("value", ['"y"', '"N"', '"1e3"', '"0o17"', '"0b1"', '"on"'])
def test_a_string_kubectl_reads_as_a_number_or_bool_is_written_quoted(
    value: str,
) -> None:
    """kubectl's go-yaml v2 resolves these plain scalars to bools and numbers; PyYAML does not."""
    assert _env_value_line(value) == f"  v: {value}"


@pytest.mark.parametrize("value", ["0o444", "y", "1e3"])
def test_a_plain_scalar_kubectl_reads_as_a_number_or_bool_stays_plain(
    value: str,
) -> None:
    """scrutiny's `defaultMode: 0o444` is a string to PyYAML and 292 to go-yaml.

    Quoted, the API server receives the string "0o444" and refuses the Deployment.
    """
    assert _env_value_line(value) == f"  v: {value}"


def test_an_ordinary_string_and_the_keys_stay_plain() -> None:
    assert _env_value_line("plain-text") == "  v: plain-text"
    text = homelab_role_label("kind: Service\nmetadata:\n  name: w\n", "widget")
    assert "kind: Service" in text.splitlines()


def _without_role_label(doc: dict) -> dict:
    labels = dict(doc["metadata"]["labels"])
    labels.pop(ROLE_LABEL)
    metadata = {**doc["metadata"], "labels": labels}
    if not labels:
        metadata.pop("labels")
    return {**doc, "metadata": metadata}


def test_every_armed_render_is_labelled_and_otherwise_unchanged() -> None:
    """The armed roles' real renders, passed through the filter the wrapper calls.

    Every document gains the label and nothing else: a Deployment's selector and pod-template
    labels, a quoted string and a multi-line value all read back as rendered, and the host's
    stdlib reader declares the same objects from the new bytes. bazarr's claim
    is the named member: the selector filters the apply, so an unlabelled claim would stop
    being applied.
    """
    seen = set()
    for role, name, text in rendered_texts():
        if role not in _ARMED_ROLES:
            continue
        before = [d for d in yaml_fast.safe_load_all(text) if d is not None]
        # A macro-only template (pihole's pihole-deployment.yaml.j2) renders empty and is
        # never staged; the deploy renders only the files the role names.
        if not before and name.removesuffix(".j2") not in declared_manifest_files(role):
            continue
        seen.add((role, name))
        labelled = homelab_role_label(text, role)
        after = list(yaml_fast.safe_load_all(labelled))
        # manifest-prune-check.sh reads the staged bytes by position, without PyYAML.
        assert declared_in(labelled) == declared_in(text), f"{role}/{name}"
        assert len(after) == len(before), f"{role}/{name} gained or lost a document"
        for old, new in zip(before, after, strict=True):
            assert new["metadata"]["labels"][ROLE_LABEL] == role, f"{role}/{name}"
            assert _without_role_label(new) == old, (
                f"{role}/{name} changed beyond the label"
            )
    assert {
        ("bazarr", "claim-default.yaml.j2"),
        ("littlelink", "service-default.yaml.j2"),
    } <= seen


def _staged_templates(role: str) -> set[str]:
    """The template names the armed apply stages for `role`: its file lists plus its claims.

    A deferred file and a probe Job's own directory are applied outside the armed
    `kubectl apply -n <namespace>`, so they are left out on purpose.
    """
    names = {
        tpl.name
        for basename in declared_manifest_files(role)
        if (tpl := manifest_template(role, basename)) is not None
    }
    return names | {"claim-default.yaml.j2"}


def _foreign_namespaces(text: str, namespace: str) -> list[tuple[str, str, str]]:
    """(kind, name, namespace) for each document placed in a namespace other than `namespace`.

    Only `metadata.namespace` counts. A cluster-scoped document carries none, and a binding's
    `subjects[].namespace` names where its subject lives, not where the binding goes.
    """
    return [
        (doc["kind"], doc["metadata"]["name"], ns)
        for doc in yaml_fast.safe_load_all(text)
        if isinstance(doc, dict)
        and (ns := (doc.get("metadata") or {}).get("namespace"))
        not in (None, namespace)
    ]


def test_an_armed_role_renders_nothing_outside_the_prune_namespace() -> None:
    """The armed apply passes `-n homelab`, and kubectl refuses a document placed elsewhere.

    The refusal reads `the namespace from the provided object "observability" does not match
    the namespace "homelab"`, and only a server-side dry run or a real deploy raised it: arming
    headlamp passed pytest and prek while its rbac.yaml still carried the Prometheus proxy Role
    in observability (#3575). headlamp is the named member, because it is the armed role whose
    render reaches another namespace at all, through its deferred file.
    """
    namespace = load_yaml_namespace()
    checked, offenders = set(), []
    for role, name, text in rendered_texts():
        if role not in _ARMED_ROLES or name not in _staged_templates(role):
            continue
        checked.add(role)
        offenders += [
            (role, name, *found) for found in _foreign_namespaces(text, namespace)
        ]
    assert "headlamp" in checked, "headlamp's staged render was never examined"
    assert not offenders, (
        f"armed roles render documents outside {namespace!r}: {offenders}. The armed apply "
        f"passes `-n {namespace}`, so kubectl refuses the whole directory. Render them into a "
        "deferred directory the role applies itself (`manifests_deferred_files`, as headlamp's "
        "observability RBAC does), or leave the role unarmed."
    )


def test_the_foreign_namespace_check_finds_headlamps_deferred_rbac() -> None:
    """Control: the real render of headlamp's deferred file is what the check above refuses.

    It also proves the staged set leaves that file out, so the check passes on headlamp because
    the file is deferred rather than because the reader saw no namespace at all.
    """
    name = "rbac-observability.yaml.j2"
    assert name not in _staged_templates("headlamp")
    text = render_role_template("headlamp", name)
    assert {found[2] for found in _foreign_namespaces(text, load_yaml_namespace())} == {
        "observability"
    }


def load_yaml_namespace() -> str:
    return yaml_fast.safe_load(
        (REPO / "ansible/inventory/group_vars/all.yml").read_text()
    )["k8s_namespace"]


def test_manifests_prune_defaults_off() -> None:
    all_vars = _defaults()
    assert all_vars["manifests_prune"] is False, (
        "manifests_prune must default false — every role that does not explicitly arm it must "
        "keep behaving exactly as before."
    )
