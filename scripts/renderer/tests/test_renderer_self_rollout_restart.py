"""`checksum/restart` on self-rolled workloads, and the census that requires it (D5.1e).

pihole and observability restart their own workloads in Ansible tasks, which a flux-owned tag
never runs. The renderer stamps each `manifests_self_rollouts` entry instead. The module
renders both roles as flux-owned through `homelab_render.cluster`, once as committed and once
with an Alertmanager Secret edit and a pihole ConfigMap edit, against a fixture `.source` that
records every secret key. A third render drops `grafana-dashboards.yaml` from observability's
`manifests_exclude_files`, the flip's edit, and holds grafana and Prometheus still across an
edit to a ConfigMap each reloads in place (#5679). The plan is docs/target-architecture/deploy/d5-bulk-move.md,
*Restart annotations for self-rolled workloads (D5.1e)*.

Run: uv run pytest scripts/renderer/tests/test_renderer_self_rollout_restart.py
"""

import shutil
from collections.abc import Iterable
from pathlib import Path

import pytest

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from lib.repo_paths import K8S_ROLES
from renderer import homelab_render
from renderer.renderer_lib.cluster import DEFERRED_SUFFIX, select_tags, tag_context
from renderer.renderer_lib.placeholders import PlaceholderRender, render_placeholders
from renderer.renderer_lib.restart import (
    RESTART_ANNOTATION,
    pod_references,
    pod_template,
    restart_targets,
    restart_values,
    self_rollouts,
)
from renderer.renderer_lib.task_inputs import include_vars

# Every role that passes `manifests_self_rollouts`, which the census renders as flux-owned
# whatever `owner` its entry carries. pihole and observability are its named members. flux's
# three controllers read no ConfigMap or Secret, so the census passes them unstamped.
SELF_ROLLED = frozenset({"flux", "pihole", "observability"})
# observability's workloads that read a ConfigMap or Secret. kube-state-metrics reads
# neither, so D3's rule gives it no value.
OBSERVABILITY_STAMPED = frozenset(
    {"alertmanager", "grafana", "loki", "otel-collector", "prometheus", "tempo"}
)


def _workloads(out: Path, tag: str) -> dict[str, dict]:
    """Each Deployment, DaemonSet or StatefulSet in `tag`'s directories, by name."""
    found = {}
    for directory in (tag, tag + DEFERRED_SUFFIX):
        for path in sorted((out / "cluster" / directory).glob("*.yaml")):
            for obj in yaml_fast.safe_load_all(path.read_text()):
                template = pod_template(obj)
                if template is not None:
                    found[obj["metadata"]["name"]] = template
    return found


def _annotation(template: dict) -> str | None:
    return ((template.get("metadata") or {}).get("annotations") or {}).get(
        RESTART_ANNOTATION
    )


def unstamped_self_rollouts(out: Path, tags: Iterable[str]) -> list[tuple[str, str]]:
    """Each `(tag, workload)` self rollout of `tags` that `out` leaves with no restart trigger.

    A self rollout is flagged when its pod template names a ConfigMap or Secret and carries
    no `checksum/restart`, or when the render holds no such workload.
    """
    found = []
    for tag in sorted(tags):
        workloads = _workloads(out, tag)
        for name in self_rollouts(tag, tag_context(K8S_ROLES / tag, tag, {})):
            template = workloads.get(name)
            if template is None or (
                pod_references(template) and not _annotation(template)
            ):
                found.append((tag, name))
    return found


def _edit(text: str, old: str, new: str) -> str:
    assert old in text, f"the fixture edit no longer matches: {old!r}"
    return text.replace(old, new, 1)


def _edited(tags: list[str], keys: list[str]) -> PlaceholderRender:
    """The placeholder render with one Alertmanager Secret edit and one pihole ConfigMap edit."""
    render = render_placeholders(tags, keys)
    for tag in render.tags:
        if tag.tag == "observability":
            tag.files["alertmanager-secret.yaml"] = _edit(
                tag.files["alertmanager-secret.yaml"],
                "resolve_timeout: 5m",
                "resolve_timeout: 6m",
            )
        if tag.tag == "pihole":
            tag.files["configmap.yaml"] = _edit(
                tag.files["configmap.yaml"],
                "\ndata:\n",
                '\ndata:\n  99-edit.conf: ""\n',
            )
    return render


@pytest.fixture(scope="module")
def renders(tmp_path_factory) -> dict[str, Path]:
    """Both self-rolled roles rendered as flux-owned: `base` as committed, then `edited`."""
    root = tmp_path_factory.mktemp("self-rollouts")
    source = root / "cluster-vars.source"
    source.write_text(
        "".join(f"{key} {'d' * 64}\n" for key in homelab_render.secret_keys())
    )
    cluster_file = root / "cluster-vars.sops.env"
    cluster_file.write_text("k=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n")
    outs = {}
    for name, render in (("base", render_placeholders), ("edited", _edited)):
        out = root / name
        code = homelab_render.cluster(
            out,
            sorted(SELF_ROLLED),
            None,
            render=render,
            flux_owned=lambda: set(SELF_ROLLED),
            cluster_file=cluster_file,
            source_file=source,
        )
        assert code == RENDER_OK
        outs[name] = out
    return outs


def _values(out: Path, tag: str) -> dict[str, str | None]:
    return {name: _annotation(t) for name, t in _workloads(out, tag).items()}


def test_every_role_that_rolls_its_own_workloads_is_a_named_member():
    """The census renders `SELF_ROLLED`, so a third such role must join it."""
    passing = {
        tag
        for tag in select_tags(None)
        if (K8S_ROLES / tag / "tasks").is_dir()
        and include_vars(K8S_ROLES / tag).get("manifests_self_rollouts")
        not in (None, [], "")
    }
    assert passing == SELF_ROLLED


def test_a_self_rollout_is_a_target_whatever_its_restart_on_names(tmp_path):
    """observability declares `restart_on: []`, and pihole-2 `rolled_by_role: true`."""
    (tmp_path / "fixture" / "tasks").mkdir(parents=True)
    (tmp_path / "fixture" / "tasks" / "main.yml").write_text(
        "- name: Deploy fixture\n"
        "  ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_rollout: ''\n"
        "    manifests_self_rollouts:\n"
        "      - { name: quiet, kind: deploy, restart_on: [] }\n"
        "      - { name: marked, kind: deploy, rolled_by_role: true }\n"
    )
    both = frozenset({"ConfigMap", "Secret"})
    assert restart_targets("fixture", {}, roles=tmp_path) == {
        "marked": both,
        "quiet": both,
    }


def test_no_self_rollout_of_a_flux_owned_role_is_left_without_a_trigger_is_clean(
    renders,
):
    assert unstamped_self_rollouts(renders["base"], SELF_ROLLED) == []
    stamped = {n for n, v in _values(renders["base"], "observability").items() if v}
    assert stamped == OBSERVABILITY_STAMPED


def test_pihole_2_with_its_annotation_removed_is_flagged(renders, tmp_path):
    out = tmp_path / "out"
    shutil.copytree(renders["base"], out)
    path = out / "cluster" / f"pihole{DEFERRED_SUFFIX}" / "deployment-2.yaml"
    text = path.read_text()
    kept = [line for line in text.splitlines(True) if RESTART_ANNOTATION not in line]
    assert len(kept) == len(text.splitlines()) - 1
    path.write_text("".join(kept))
    assert unstamped_self_rollouts(out, SELF_ROLLED) == [("pihole", "pihole-2")]


def test_an_alertmanager_secret_edit_moves_alertmanager_alone(renders):
    before = _values(renders["base"], "observability")
    after = _values(renders["edited"], "observability")
    assert {name for name in before if before[name] != after[name]} == {"alertmanager"}


def test_a_pihole_config_map_edit_moves_both_instances(renders):
    """Instance 2's annotation renders in `pihole-deferred`, which D4 orders after `pihole`."""
    deferred = renders["base"] / "cluster" / f"pihole{DEFERRED_SUFFIX}"
    assert RESTART_ANNOTATION in (deferred / "deployment-2.yaml").read_text()
    before = _values(renders["base"], "pihole")
    after = _values(renders["edited"], "pihole")
    assert set(before) == {"pihole", "pihole-2"}
    assert all(before.values())
    assert all(before[name] != after[name] for name in before)


DASHBOARDS = "grafana-dashboards.yaml"


def _flipped_values(
    tmp_path: Path, edit: bool
) -> tuple[dict[str, str], dict[str, str]]:
    """observability's restart values with the flip's edit, and its rendered directory.

    The tree links every role but observability, which is a copy without the exclusion.
    `edit` adds a board to one dashboard ConfigMap and a rule file to `prometheus-rules`.
    """
    roles = tmp_path / ("edited" if edit else "base")
    roles.mkdir()
    for role in K8S_ROLES.iterdir():
        if role.name != "observability":
            (roles / role.name).symlink_to(role)
    shutil.copytree(K8S_ROLES / "observability", roles / "observability")
    tasks = roles / "observability" / "tasks" / "main.yml"
    tasks.write_text(
        _edit(tasks.read_text(), f"[service.yaml, {DASHBOARDS}]", "[service.yaml]")
    )
    keys = homelab_render.secret_keys()
    (tag,) = render_placeholders(["observability"], keys, roles=roles).tags
    if edit:
        for name in (DASHBOARDS, "prometheus-rules.yaml"):
            tag.files[name] = _edit(
                tag.files[name], "\ndata:\n", '\ndata:\n  edit.json: "{}"\n'
            )
    context = tag_context(K8S_ROLES / "observability", "observability", {})
    values = restart_values(
        "observability",
        tag.directories(),
        restart_targets("observability", context),
        keys,
        {key: "d" * 64 for key in keys},
    )
    return values, tag.files


def test_a_board_or_rule_edit_after_the_flip_moves_no_restart_value(tmp_path):
    """Grafana and Prometheus re-read these ConfigMaps in place; Ansible never rolled them."""
    # fact: ansible/roles/k8s/observability/CLAUDE.md#One config change, one restart
    before, base = _flipped_values(tmp_path, edit=False)
    after, edited = _flipped_values(tmp_path, edit=True)
    assert DASHBOARDS in base
    assert edited[DASHBOARDS] != base[DASHBOARDS]
    assert edited["prometheus-rules.yaml"] != base["prometheus-rules.yaml"]
    assert set(before) == OBSERVABILITY_STAMPED
    assert after == before
