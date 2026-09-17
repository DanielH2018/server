"""Every deployed host script that embeds a credential is unreadable to `other`.

`secret_bearing_host_paths()` derives which `/usr/local/bin` and `/opt/homelab` files render
a tracked secret inline; this holds each one's install task to a mode with no `other` bits.
Four roles carried a `DECIDED: 0700` marker for it and twelve did not, and three of those
twelve were `0755` on 2026-09-17: `docs-refresh.sh`, `eval-run.sh` and
`ups-secondary-health.sh`, each readable by any local user and each holding a live token.
The bound is `other`, not "exactly 0700": the root-run crowdsec, traefik and gitops_deploy
scripts are `0750 root:root`, which is the same guarantee for their callers.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_bearing_host_scripts_are_not_world_readable.py
"""

from pathlib import Path

import pytest

from secrets_mgmt.secret_bearing_host_paths import (
    readable_beyond_owner_and_group,
    world_readable_secret_bearing_tasks,
)


def test_no_secret_bearing_host_script_is_world_readable():
    assert world_readable_secret_bearing_tasks() == []


@pytest.mark.parametrize(
    ("mode", "readable"),
    [
        ("0700", False),
        ("0750", False),
        ("0755", True),
        ("0644", True),
        ("0640", False),
        (None, True),
    ],
)
def test_the_other_bits_decide(mode, readable):
    assert readable_beyond_owner_and_group(mode) is readable


def _tree(tmp_path: Path, mode_line: str) -> Path:
    role = tmp_path / "roles" / "demo"
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "templates" / "thing.sh.j2").write_text("KUMA={{ demo_push_token }}\n")
    (role / "tasks" / "main.yml").write_text(
        "- name: Install thing\n"
        "  ansible.builtin.template:\n"
        "    src: thing.sh.j2\n"
        "    dest: /usr/local/bin/thing.sh\n" + mode_line
    )
    (tmp_path / "secret_rotation.yml").write_text(
        "entries:\n  demo_push_token:\n    tier: auto\n    last_rotated: '2026-01-01'\n"
    )
    return tmp_path


def test_a_world_readable_secret_bearing_script_is_flagged(tmp_path: Path):
    tree = _tree(tmp_path, '    mode: "0755"\n')
    assert world_readable_secret_bearing_tasks(tree, tree / "secret_rotation.yml") == [
        ("roles/demo/tasks/main.yml", "/usr/local/bin/thing.sh", "0755")
    ]


def test_an_owner_only_script_and_a_secret_free_script_are_clean(tmp_path: Path):
    tree = _tree(tmp_path, '    mode: "0700"\n')
    assert world_readable_secret_bearing_tasks(tree, tree / "secret_rotation.yml") == []
    (tree / "roles" / "demo" / "templates" / "thing.sh.j2").write_text("echo hi\n")
    (tree / "roles" / "demo" / "tasks" / "main.yml").write_text(
        (tree / "roles" / "demo" / "tasks" / "main.yml")
        .read_text()
        .replace('"0700"', '"0755"')
    )
    assert world_readable_secret_bearing_tasks(tree, tree / "secret_rotation.yml") == []
