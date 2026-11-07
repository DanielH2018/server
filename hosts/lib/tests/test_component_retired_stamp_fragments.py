"""A component retires the setup-manifest fragments its Ansible-era task actually stamped (#6279).

An Ansible setup role records what it installed in two fragments, through
`ansible/roles/setup/common/tasks/stamp_render.yml` (`setup-render-manifest.d/<stamp_render_name>`)
and `ansible/roles/setup/common/tasks/stamp_deployed.yml`
(`setup-deployed-manifest.d/<stamp_deployed_name>`). The two names can differ. When a component
takes a task over, its `legacy:` list must name each fragment as the task stamped it: a fragment
left behind names a file reconcile removed, and setup-drift-check.sh reads it as missing
every run. loki-route-witness listed its render name for both and left
`setup-deployed-manifest.d/loki-route-health` on daniel-box.

The loki-route-witness tasks still run on daniel-server, so that row is derived from
`initial_setup/tasks/crons.yml`. Every other mover's task is gone, so its row is recorded from
the task file at the commit before the move.

Run: uv run pytest hosts/lib/tests/test_component_retired_stamp_fragments.py
"""

import yaml

from lib.repo_paths import REPO

COMPONENTS = REPO / "hosts" / "components"
CRONS = REPO / "ansible" / "roles" / "setup" / "initial_setup" / "tasks" / "crons.yml"
KINDS = {
    "/var/lib/homelab/setup-render-manifest.d/": "render",
    "/var/lib/homelab/setup-deployed-manifest.d/": "deployed",
}

# The fragments each retired task stamped, read from `stamp_render_name` and
# `stamp_deployed_name` in the task file at the parent of the commit that moved it.
RECORDED = {
    # 94d0fb13a^:ansible/roles/setup/homelab_builder/tasks/builder.yml
    "builder": {("deployed", "homelab-builder"), ("render", "homelab-builder")},
    # 3b3e4abb1:ansible/roles/setup/renovate_notify/tasks/{service,code}.yml
    "renovate-notify": {("render", "renovate-notify"), ("deployed", "renovate-notify")},
    # 3b3e4abb1:ansible/roles/setup/renovate_agent/tasks/{service,code}.yml
    "renovate-agent": {("render", "renovate-agent"), ("deployed", "renovate-agent")},
    # 3b3e4abb1:ansible/roles/setup/fake_remux/tasks/main.yml
    "fake-remux": {("render", "fake-remux"), ("deployed", "fake-remux")},
    # 3b3e4abb1:ansible/roles/setup/gitops_deploy/tasks/github_checks.yml
    "github-checks": {("render", "gitops-deploy-github-checks")},
    # cb61ae0d94:ansible/roles/setup/initial_setup/tasks/crons.yml
    "worktree-holders": {("deployed", "worktree-holders")},
    # cb61ae0d9:ansible/roles/setup/initial_setup/tasks/crons.yml
    "loki-route-witness": {
        ("render", "loki-route-witness"),
        ("deployed", "loki-route-health"),
    },
    # 39f4bd5c4^:ansible/roles/setup/initial_setup/tasks/crons.yml
    "docs-refresh": {("render", "docs-refresh")},
    "eval-sweep": {("render", "eval-run")},
    "secret-rotation": {("render", "secret-rotation")},
    # c1d338364:ansible/roles/setup/k3s/tasks/health-crons.yml, and #2978 retired the reap one.
    "k3s-host-checks": {
        ("render", "k3s-backup-health"),
        ("render", "k3s-disk-health"),
        ("render", "k3s-etcd-snapshot"),
        ("deployed", "k3s-longhorn-backup-health-reader"),
        ("deployed", "k3s-longhorn-reap"),
    },
    # a2bed5696^:ansible/roles/setup/nut_host/tasks/main.yml
    "nut-secondary": {("render", "nut-host")},
    # 84bb0bbf0a:ansible/roles/setup/initial_setup/tasks/integrity.yml
    "integrity-scans": {("render", "rkhunter-check"), ("render", "aide-check")},
    # 85d2bf9f7:ansible/roles/setup/claude_code/tasks/main.yml, before the role's render stamp
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
# A component whose Ansible tasks still run on another host; its row comes from those tasks, as
# (task file, tag) pairs.
DERIVED = {}


def retired_fragments(spec: dict) -> set[tuple[str, str]]:
    found = set()
    for entry in spec.get("legacy") or []:
        path = entry.get("path", "")
        for prefix, kind in KINDS.items():
            if path.startswith(prefix):
                found.add((kind, path.removeprefix(prefix)))
    return found


def stamped_fragments(tasks_file, tag: str) -> set[tuple[str, str]]:
    found = set()
    for task in yaml.safe_load(tasks_file.read_text()):
        if tag not in (task.get("tags") or []):
            continue
        task_vars = task.get("vars") or {}
        for kind in ("render", "deployed"):
            if name := task_vars.get(f"stamp_{kind}_name"):
                found.add((kind, name))
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


def expected() -> dict[str, set[tuple[str, str]]]:
    derived = {
        name: set().union(*(stamped_fragments(*src) for src in sources))
        for name, sources in DERIVED.items()
    }
    return RECORDED | derived


def test_every_component_retires_exactly_the_fragments_its_task_stamped():
    found = components()
    assert {"loki-route-witness", "builder", "integrity-scans"} <= set(found)
    assert mismatches(found, expected()) == []


def test_a_render_name_listed_for_the_deployed_fragment_is_reported():
    before_6279 = {("render", "loki-route-witness"), ("deployed", "loki-route-witness")}
    assert mismatches({"loki-route-witness": before_6279}, expected()) == [
        "loki-route-witness: retires [('deployed', 'loki-route-witness'), "
        "('render', 'loki-route-witness')], the task stamped "
        "[('deployed', 'loki-route-health'), ('render', 'loki-route-witness')]"
    ]
