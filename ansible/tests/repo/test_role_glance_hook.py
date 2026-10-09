"""The `regen-role-glance` prek hook rewrites a stale At a glance block at commit time (#3698).

Editing a role's defaults, templates, tasks, playbook entry or `containers_list` entry used to
fail CI until someone ran `scripts/docs/gen_role_glance.py` by hand. The hook runs it in
`--fix` mode on the commit instead. This guard holds the two things a later edit could
quietly lose: the hook's `files` gate matches every class of source the generator reads and
none of the docs-refresh cron's staged paths, and `--fix` exits 1 when it wrote a doc, which
is what stops the commit for a re-stage.

Run: uv run pytest ansible/tests/repo/test_role_glance_hook.py
"""

import re
import tomllib

import pytest
from _helpers import REPO

import gen_role_glance as g

HOOK_ID = "regen-role-glance"


def _hook() -> dict:
    config = tomllib.loads((REPO / "prek.toml").read_text())
    hooks = [
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == HOOK_ID
    ]
    assert len(hooks) == 1, f"expected exactly one {HOOK_ID} hook, found {len(hooks)}"
    return hooks[0]


def test_the_hook_runs_the_generator_in_fix_mode():
    assert _hook()["entry"].endswith(f"python {g.SELF} --fix")


@pytest.mark.parametrize(
    "path",
    [
        "ansible/roles/k8s/sonarr/defaults/main.yml",
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
        "ansible/roles/k8s/sonarr/CLAUDE.md",
        "ansible/roles/setup/gitops_deploy/tasks/main.yml",
        "ansible/roles/containers/alloy/templates/docker-compose.yml.j2",
        "ansible/roles/setup/k3s/defaults/main.yml",
        "ansible/inventory/host_vars/daniel-box.yml",
        "ansible/inventory/host_vars/daniel-pi.yml",
        "ansible/inventory/group_vars/all.yml",
        "ansible/initial_setup.yml",
        "ansible/filter_plugins/k8s_autodeploy.py",
        "scripts/docs/glance_facts.py",
        "scripts/lib/render_guard.py",
    ],
)
def test_the_gate_matches_a_source_the_generator_reads(path):
    assert re.search(_hook()["files"], path)


@pytest.mark.parametrize(
    "path",
    [
        "docs/reference/services.md",
        "docs/assets/generated/fragments/x.md",
        "scripts/dev/pytest_shard_weights.json",
        "CLAUDE.md",
    ],
)
def test_the_gate_skips_a_path_the_generator_does_not_read(path):
    """The docs-refresh cron commits the first three, so it never writes under ansible/roles/."""
    assert not re.search(_hook()["files"], path)


def test_fix_mode_fails_after_rewriting_a_stale_doc(capsys):
    assert g.report(["k8s/sonarr"], check=False, fix=True) == 1
    assert "k8s/sonarr" in capsys.readouterr().out


def test_fix_mode_passes_when_nothing_was_stale():
    assert g.report([], check=False, fix=True) == 0
