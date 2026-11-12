"""A component retires the setup-manifest fragments its Ansible-era task actually stamped (#6279).

An Ansible setup role recorded what it installed in two fragments,
`setup-render-manifest.d/<stamp_render_name>` and `setup-deployed-manifest.d/<stamp_deployed_name>`,
through the shared stamp task files H7.63 deleted. The two names can differ. When a component
took a task over, its `legacy:` list had to name each fragment as the task stamped it, or the
fragment stayed behind naming a file reconcile removed. loki-route-witness listed its render name
for both and left `setup-deployed-manifest.d/loki-route-health` on daniel-box.

Every mover's task is gone, so each row is recorded from the task file at the commit before
the move.

Run: uv run pytest hosts/lib/tests/test_component_retired_stamp_fragments.py
"""

import yaml

from lib.repo_paths import REPO

COMPONENTS = REPO / "hosts" / "components"
KINDS = {
    "/var/lib/homelab/setup-render-manifest.d/": "render",
    "/var/lib/homelab/setup-deployed-manifest.d/": "deployed",
}

# The fragments each retired task stamped, read from `stamp_render_name` and
# `stamp_deployed_name` in the task file at the parent of the commit that moved it.
RECORDED = {
    # 94d0fb13a^, the homelab_builder role's tasks/builder.yml
    "builder": {("deployed", "homelab-builder"), ("render", "homelab-builder")},
    # 3b3e4abb1, the renovate_notify role's tasks/{service,code}.yml
    "renovate-notify": {("render", "renovate-notify"), ("deployed", "renovate-notify")},
    # 3b3e4abb1, the renovate_agent role's tasks/{service,code}.yml
    "renovate-agent": {("render", "renovate-agent"), ("deployed", "renovate-agent")},
    # 3b3e4abb1, the fake_remux role's tasks/main.yml
    "fake-remux": {("render", "fake-remux"), ("deployed", "fake-remux")},
    # 3b3e4abb1, the gitops_deploy role's tasks/github_checks.yml
    "github-checks": {("render", "gitops-deploy-github-checks")},
    # 57f107926, the deploy_ui role's tasks/{service,code}.yml
    "deploy-ui": {("render", "deploy-ui"), ("deployed", "deploy-ui")},
    # cb61ae0d94, the initial_setup role's tasks/crons.yml
    "worktree-holders": {("deployed", "worktree-holders")},
    # cb61ae0d9, the initial_setup role's tasks/crons.yml
    "loki-route-witness": {
        ("render", "loki-route-witness"),
        ("deployed", "loki-route-health"),
    },
    # 39f4bd5c4^, the initial_setup role's tasks/crons.yml
    "docs-refresh": {("render", "docs-refresh")},
    "eval-sweep": {("render", "eval-run")},
    "secret-rotation": {("render", "secret-rotation")},
    # c1d338364, the k3s role's tasks/health-crons.yml, and #2978 retired the reap one.
    "k3s-host-checks": {
        ("render", "k3s-backup-health"),
        ("render", "k3s-disk-health"),
        ("render", "k3s-etcd-snapshot"),
        ("deployed", "k3s-longhorn-backup-health-reader"),
        ("deployed", "k3s-longhorn-reap"),
    },
    # a2bed5696^, the nut_host role's tasks/main.yml
    "nut-secondary": {("render", "nut-host")},
    # 84bb0bbf0a, the initial_setup role's tasks/integrity.yml
    "integrity-scans": {("render", "rkhunter-check"), ("render", "aide-check")},
    # 85d2bf9f7, the claude_code role's tasks/main.yml, before the role's render stamp
    # took the name claude-code-ansible. H7.40's third slice moved triage, whose templates were
    # all claude-code-ansible still listed, so it retires that render stamp and the triage one.
    # H7.40's fourth slice moved the artifacts node's crons, whose task stamped
    # claude-code-node-crons (the role task file node_crons.yml, since deleted).
    "claude-code": {
        ("render", "claude-code"),
        ("render", "claude-code-ansible"),
        ("render", "claude-code-node-crons"),
        ("deployed", "claude-code-triage"),
        ("deployed", "claude-code-bg-resume"),
        ("deployed", "claude-code-cgroup-metrics"),
        ("deployed", "claude-code-clone-sync"),
        ("deployed", "claude-code-dotfiles-sync"),
    },
}


def retired_fragments(spec: dict) -> set[tuple[str, str]]:
    found = set()
    for entry in spec.get("legacy") or []:
        path = entry.get("path", "")
        for prefix, kind in KINDS.items():
            if path.startswith(prefix):
                found.add((kind, path.removeprefix(prefix)))
    return found


def mismatches(components: dict, expected: dict) -> list[str]:
    """Each component whose retired fragments differ from what its task stamped."""
    return [
        f"{name}: retires {sorted(have)}, the task stamped {sorted(expected.get(name, set()))}"
        for name, have in sorted(components.items())
        if have != expected.get(name, set())
    ]


def components() -> dict[str, set[tuple[str, str]]]:
    return {
        path.parent.name: retired_fragments(yaml.safe_load(path.read_text()))
        for path in COMPONENTS.glob("*/component.yml")
    }


def test_every_component_retires_exactly_the_fragments_its_task_stamped():
    found = components()
    assert {"loki-route-witness", "builder", "integrity-scans"} <= set(found)
    assert mismatches(found, RECORDED) == []


def test_a_render_name_listed_for_the_deployed_fragment_is_reported():
    before_6279 = {("render", "loki-route-witness"), ("deployed", "loki-route-witness")}
    assert mismatches({"loki-route-witness": before_6279}, RECORDED) == [
        "loki-route-witness: retires [('deployed', 'loki-route-witness'), "
        "('render', 'loki-route-witness')], the task stamped "
        "[('deployed', 'loki-route-health'), ('render', 'loki-route-witness')]"
    ]
