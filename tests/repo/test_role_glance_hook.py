"""The `regen-role-glance` prek hook rewrites a stale At a glance block at commit time (#3698).

Editing a role's defaults, templates, tasks, playbook entry or `containers_list` entry used to
fail CI until someone ran `scripts/docs/gen_role_glance.py` by hand. The hook runs it in
`--fix` mode on the commit instead. This guard holds the two things a later edit could
quietly lose: the hook's `files` gate matches every class of source the generator reads and
none of the docs-refresh cron's staged paths, and `main(["--fix"])` over a stale fixture role
rewrites the doc and exits 1, which is what stops the commit for a re-stage.

Run: uv run pytest tests/repo/test_role_glance_hook.py
"""

import functools
import re
import shutil
import tomllib
from collections.abc import Callable
from pathlib import Path

import gen_role_glance as g
import pytest
import yaml
from _helpers import REPO
from docs.catalog_lib import catalog_backup, catalog_facts, catalog_model
from lib.estate import Inventory

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
        "k8s/roles/sonarr/defaults/main.yml",
        "k8s/roles/sonarr/templates/deployment.yaml.j2",
        "k8s/roles/sonarr/CLAUDE.md",
        "inventory/host_vars/daniel-box.yml",
        "inventory/host_vars/daniel-pi.yml",
        "inventory/group_vars/all.yml",
        # Where the libraries the generator imports live now, read from the modules
        # themselves, so a move to a directory outside the gate goes red here.
        *(
            Path(m.__file__).resolve().relative_to(REPO).as_posix()
            for m in (catalog_backup, catalog_facts, catalog_model)
        ),
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


def _stale_sonarr_fixture(tmp_path: Path) -> tuple[Path, Callable[..., list[str]]]:
    """A copy of sonarr with a hand-edited block, and a `find_stale` scoped to it.

    sonarr's `media-data` claim is declared by media-volume, so that role is copied too.
    """
    roles = tmp_path / "roles"
    for name in ("sonarr", "media-volume"):
        shutil.copytree(g.K8S_ROLES / name, roles / name)
    entry = g.Estate().k8s_entries()["sonarr"]
    services = tmp_path / "services.yml"
    services.write_text(yaml.safe_dump({"containers_list": [entry]}))
    estate = g.Estate(Inventory(host_vars=tmp_path, services=services))
    doc = roles / "sonarr" / "CLAUDE.md"
    text = doc.read_text()
    assert "`land.sh --tags=sonarr`" in text
    doc.write_text(text.replace("`land.sh --tags=sonarr`", "`land.sh --tags=sonar`", 1))
    return doc, functools.partial(g.stale_k8s_docs, estate=estate, k8s_roles=roles)


def test_fix_mode_rewrites_a_stale_doc_and_fails(tmp_path, capsys):
    doc, find_stale = _stale_sonarr_fixture(tmp_path)
    assert g.main(["--fix"], find_stale=find_stale) == 1
    assert "k8s/sonarr" in capsys.readouterr().out
    assert "`land.sh --tags=sonarr`" in doc.read_text()
    assert find_stale(write=False) == []


def test_fix_mode_passes_once_the_doc_is_fresh(tmp_path):
    _, find_stale = _stale_sonarr_fixture(tmp_path)
    g.main(["--fix"], find_stale=find_stale)
    assert g.main(["--fix"], find_stale=find_stale) == 0
