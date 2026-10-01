#!/usr/bin/env python3
"""Tests for the own-role digest-provable derivation.

Run: uv run pytest scripts/deploy_tools/tests/test_digest_provable.py
"""

import json
from pathlib import Path

import pytest

import deploy_narrow
import digest_provable

_MANIFESTS = """\
- name: Compute the claim name
  ansible.builtin.set_fact:
    homepage_claim: data
- name: Render and apply the manifests
  ansible.builtin.include_role:
    name: k8s/manifests
"""

REPO_K8S = Path(__file__).resolve().parents[3] / "ansible" / "roles" / "k8s"


def _role(tmp_path: Path, tasks: str, extra: dict[str, str] | None = None) -> Path:
    role = tmp_path / "svc"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(tasks)
    for rel, text in (extra or {}).items():
        path = role / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return role


def test_a_role_that_only_renders_its_manifests_is_provable(tmp_path):
    assert digest_provable.digest_provable(_role(tmp_path, _MANIFESTS))


def test_an_included_task_file_is_held_to_the_same_rule(tmp_path):
    tasks = "- ansible.builtin.include_tasks: render.yml\n"
    role = _role(tmp_path, tasks, {"tasks/render.yml": _MANIFESTS})
    assert digest_provable.digest_provable(role)


@pytest.mark.parametrize(
    "tasks, extra",
    [
        pytest.param(
            _MANIFESTS + "- ansible.builtin.copy:\n    src: x\n    dest: /etc/x\n",
            {},
            id="writes-a-host-file",
        ),
        pytest.param(
            "- ansible.builtin.include_role:\n    name: k8s/volume-claim\n",
            {},
            id="includes-another-shared-role",
        ),
        pytest.param(
            "- ansible.builtin.include_tasks: '{{ which }}.yml'\n",
            {},
            id="templated-include",
        ),
        pytest.param(
            "- block:\n    - ansible.builtin.command: kubectl create cm x\n",
            {},
            id="command-inside-a-block",
        ),
        pytest.param(
            _MANIFESTS,
            {"handlers/main.yml": "- name: restart\n  ansible.builtin.command: x\n"},
            id="has-handlers",
        ),
        pytest.param(
            _MANIFESTS,
            {"meta/main.yml": "dependencies:\n  - role: k8s/image-builder\n"},
            id="meta-dependency",
        ),
        pytest.param("- [unbalanced\n", {}, id="unparseable"),
    ],
)
def test_anything_acting_outside_the_digest_is_not_provable(tmp_path, tasks, extra):
    assert not digest_provable.digest_provable(_role(tmp_path, tasks, extra))


def test_the_real_tree_names_a_provable_role_and_refuses_a_volume_claim_one():
    """The derivation finds its subject by walking tasks, so it must find a known member.

    homepage renders manifests and nothing else; sonarr includes `k8s/volume-claim`, whose PVC
    staging the digest never stats.
    """
    assert digest_provable.digest_provable(REPO_K8S / "homepage")
    assert not digest_provable.digest_provable(REPO_K8S / "sonarr")


def test_the_deployers_argv_is_one_main_accepts(capsys):
    """The tick's fakes replace the subprocess, so only this sees a flag the CLI does not take."""
    argv = deploy_narrow.digest_provable_argv({"homepage", "no-such-role"})
    assert argv[4] == deploy_narrow.DIGEST_PROVABLE_SCRIPT
    repo = str(REPO_K8S.parents[2])
    assert digest_provable.main([*argv[5:], "--repo", repo]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "homepage": True,
        "no-such-role": False,
    }
