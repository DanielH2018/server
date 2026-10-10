"""`collection_resolves` against the real `ansible-galaxy`, one collection tree it must find and one it must not.

The probe decides whether the tag-scoping `--list-tasks` test runs at all, so a probe that
always answers False turns that test into a permanent skip with nothing red. The real binary,
not a stub, is the point: `ansible-galaxy collection list` exits 0 either way, and the probe
reads its table, whose format belongs to ansible-core.
"""

import json
import os
import shutil
from pathlib import Path

import pytest
from _ansible_collections import collection_resolves


@pytest.fixture
def playbook() -> str:
    found = shutil.which("ansible-playbook")
    if found is None:
        pytest.skip("ansible-playbook not on PATH")
    return found


def _env(collections: Path) -> dict[str, str]:
    return {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(collections)}


def test_an_installed_collection_resolves(tmp_path, playbook):
    manifest = tmp_path / "ansible_collections/community/general/MANIFEST.json"
    manifest.parent.mkdir(parents=True)
    info = {"namespace": "community", "name": "general", "version": "1.0.0"}
    manifest.write_text(json.dumps({"collection_info": info}))

    assert collection_resolves("community.general", playbook, tmp_path, _env(tmp_path))


def test_an_empty_collections_path_does_not_resolve(tmp_path, playbook):
    assert not collection_resolves(
        "community.general", playbook, tmp_path, _env(tmp_path)
    )
