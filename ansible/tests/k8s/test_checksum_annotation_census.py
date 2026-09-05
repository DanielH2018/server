"""Census: every k8s role that stages ConfigMap content OUTSIDE the manifests role's own
render/apply cycle either rolls its pod on a content change (a `checksum/` annotation) or is
named in DEBT with the reason it does not need one.

A role's pod can depend on content the manifests role's own change detection cannot see in
two different ways, and only one of them needs an annotation:

  (a) Embedded. A ConfigMap/Secret template under `templates/*.j2` — one of `manifests_files`
      or `manifests_secret_files`, applied through the standard render/apply cycle — builds
      its `data:` from `lookup('file'|'template', ...)`. This needs no annotation: the
      lookup's content is IN the rendered manifest, so a content change changes
      `manifests_render`'s bytes and the central rollout-restart
      (`roles/k8s/manifests/tasks/main.yml`, guarded on `manifests_render is changed`) fires
      on its own. See "A config edit won't restart the pod (k3s)" in the repo CLAUDE.md.
      Not this census's subject at all — fourteen roles do this today (artifacts, authelia,
      configarr, crowdsec, freshrss, home-assistant, homepage, image-builder, janitorr,
      karakeep, livesync, loki-homelab, traefik, zigbee2mqtt) and none of them needs anything
      from this file.

  (b) Staged outside the cycle. A role's `tasks/*.yml` runs `kubectl create configmap
      --from-file` (`from_file_configmap_roles()` below) and applies the result itself with
      its own `kubectl apply --server-side` task
      (`test_script_configmaps_apply_server_side.py` covers THAT half) — entirely outside
      `manifests_files`. `manifests_render is changed` never sees this ConfigMap, so nothing
      restarts the pod unless the role adds its own trigger via
      `ansible/templates/checksum-annotation.yml.j2`. THIS is the census's subject.

Run: uv run pytest ansible/tests/k8s/test_checksum_annotation_census.py
"""

import sys
from pathlib import Path

import pytest
from _helpers import REPO

sys.path.insert(0, str(REPO / "scripts"))

from lib import yaml_fast

from validate.k8s_manifests import (
    SHARED_TPL,
    make_env,
    register_ansible_filters,
)

from _k8s_render import rendered_docs

K8S_ROLES = REPO / "ansible" / "roles" / "k8s"
WORKLOAD_KINDS = {"Deployment", "DaemonSet", "StatefulSet"}


def from_file_configmap_roles(roles_dir: Path = K8S_ROLES) -> set[str]:
    """Every role whose tasks stage a ConfigMap with `kubectl create configmap --from-file`.

    Derived from the tree rather than listed, so a role that starts doing this joins the
    census the day it appears — the same shape `test_script_configmaps_apply_server_side.py`
    uses for its own (narrower — one exact task name) version of this predicate.
    """
    found = set()
    for role_dir in sorted(roles_dir.iterdir()):
        tasks_dir = role_dir / "tasks"
        if not tasks_dir.is_dir():
            continue
        for task_file in tasks_dir.glob("*.yml"):
            if "create configmap" in task_file.read_text():
                found.add(role_dir.name)
                break
    return found


# Roles the census finds today that need no checksum/ annotation despite staging a ConfigMap
# outside manifests_files — each entry names the mechanism that covers it instead. A role
# landing here without a real mechanism is exactly the false-clear this census exists to
# prevent, so a reason is required, not optional.
DEBT: dict[str, str] = {
    "claude-otel": (
        "grafana.yaml.j2's dashboard ConfigMaps are staged this way, but Grafana's own file "
        "provisioner polls the mount every 30s (`updateIntervalSeconds: 30` in "
        "templates/grafana.yaml.j2) and reloads dashboards without a pod restart — a "
        "checksum/ annotation would force an unnecessary Grafana restart on every dashboard "
        "edit instead of doing nothing."
    ),
}

# Ratchet: DEBT growing is a deliberate, reviewed choice made in the same diff as the new
# entry and its reason, not a side effect of some other change. Raise this alongside DEBT.
MAX_DEBT = 1

CENSUS = sorted(from_file_configmap_roles())

# Non-vacuity: a predicate that stopped matching (the "create configmap" grep text moved, a
# role renamed) must fail loudly here rather than silently emptying CENSUS and passing the
# parametrized check below on zero cases.
KNOWN_FLOOR = {
    "autofix-bridge",
    "claude-otel",
    "monitor-bridge",
    "terraria-stats",
    "valheim-stats",
}


def test_the_derivation_finds_the_known_roles():
    assert KNOWN_FLOOR <= set(CENSUS), CENSUS


def test_debt_names_only_roles_the_census_actually_finds():
    stale = set(DEBT) - set(CENSUS)
    assert not stale, (
        f"DEBT excuses {stale}, but the census no longer finds {stale} staging a ConfigMap "
        "outside manifests_files — drop the now-stale entry."
    )


def test_debt_does_not_grow_silently():
    assert len(DEBT) <= MAX_DEBT, (
        f"DEBT holds {len(DEBT)} role(s) ({sorted(DEBT)}), above the pinned ceiling of "
        f"{MAX_DEBT}. Raise MAX_DEBT in the same diff that adds the new entry and its reason."
    )


def has_checksum_annotation(doc: dict) -> bool:
    """True if a rendered Deployment/DaemonSet/StatefulSet doc carries a checksum/ annotation
    on its pod template."""
    annotations = (
        ((doc.get("spec") or {}).get("template") or {}).get("metadata") or {}
    ).get("annotations") or {}
    return any(key.startswith("checksum/") for key in annotations)


def test_has_checksum_annotation_is_flagged():
    doc = {
        "kind": "Deployment",
        "spec": {
            "template": {
                "metadata": {"annotations": {"checksum/stats-script": "abc123"}}
            }
        },
    }
    assert has_checksum_annotation(doc)


def test_missing_checksum_annotation_is_clean():
    doc = {
        "kind": "Deployment",
        "spec": {
            "template": {"metadata": {"annotations": {"some-other/annotation": "x"}}}
        },
    }
    assert not has_checksum_annotation(doc)


def test_checksum_annotation_macro_produces_a_detected_annotation():
    """End-to-end proof that the shared macro's output is what `has_checksum_annotation`
    looks for, so a future edit to either side (the macro's key format, the detector's
    prefix) is caught by the other rather than by the real roles going quietly uncovered.
    """
    env = make_env([SHARED_TPL])
    register_ansible_filters(env)
    tpl = env.from_string(
        "{% from 'checksum-annotation.yml.j2' import checksum_annotation with context %}\n"
        "kind: Deployment\n"
        "spec:\n"
        "  template:\n"
        "    metadata:\n"
        "      annotations:\n"
        "        {{ checksum_annotation('demo', value='deadbeef') }}\n"
    )
    doc = yaml_fast.safe_load(tpl.render())
    assert has_checksum_annotation(doc)


def _roles_with_checksum_annotation() -> set[str]:
    found = set()
    for role, _tpl, doc in rendered_docs():
        if doc.get("kind") in WORKLOAD_KINDS and has_checksum_annotation(doc):
            found.add(role)
    return found


@pytest.mark.parametrize("role", CENSUS)
def test_role_carries_checksum_annotation_or_is_excused(role):
    if role in DEBT:
        pytest.skip(f"{role}: excused — {DEBT[role]}")
    assert role in _roles_with_checksum_annotation(), (
        f"{role} stages a ConfigMap via `kubectl create configmap --from-file`, outside "
        "manifests_files/manifests_secret_files, so nothing restarts its pod on a content "
        "change unless it adds its own checksum/<name> annotation "
        "(ansible/templates/checksum-annotation.yml.j2). Add one, or add "
        f"{role!r} to DEBT above with the reason it does not need one."
    )
