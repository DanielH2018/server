"""Every declared secret output is unreadable to `other`.

A host component's `outputs:` entry names a file `homelab-secrets` renders with a credential in
it, and the entry's `mode` is the file's mode. This holds each one to a mode with no `other`
bits. The bound is `other`, not "exactly 0600": `/etc/nut/upsmon.conf` is `0640` so the nut
group can read it, which is the same guarantee for everyone outside that group.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_bearing_host_scripts_are_not_world_readable.py
"""

from pathlib import Path

import pytest

from lib.repo_paths import K3S_HOST_CHECKS
from secrets_mgmt.secret_bearing_host_paths import (
    readable_beyond_owner_and_group,
    world_readable_outputs,
)


def test_no_declared_secret_output_is_world_readable():
    assert world_readable_outputs() == []


def test_the_real_tree_declares_an_output_to_check():
    """Non-vacuity: the check above passes over nothing if the specs declare no output."""
    assert "outputs:" in (K3S_HOST_CHECKS / "component.yml").read_text()


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


def _tree(tmp_path: Path, mode: str) -> Path:
    spec = tmp_path / "hosts" / "components" / "demo" / "component.yml"
    spec.parent.mkdir(parents=True)
    spec.write_text(
        "outputs:\n  - {src: templates/demo.conf.j2, path: /etc/demo/demo.conf,"
        f' owner: root, group: root, mode: "{mode}", keys: [demo_push_token]}}\n'
    )
    return tmp_path


def test_a_world_readable_output_is_flagged(tmp_path: Path):
    tree = _tree(tmp_path, "0644")
    assert world_readable_outputs(tree) == [
        ("hosts/components/demo/component.yml", "/etc/demo/demo.conf", "0644")
    ]


def test_an_owner_only_output_is_clean(tmp_path: Path):
    assert world_readable_outputs(_tree(tmp_path, "0600")) == []
