"""The retired flat weekly group label is gone from longhorn-reconcile (#8288).

No live volume carries `recurring-job-group.longhorn.io/weekly-backup` (0 of 44 on 2026-11-13),
and the PR for #8168 deleted the backup-health reader of it. The job neither migrates nor names
the label. The census skips this file, which names the label it forbids.

Run: uv run pytest k8s/roles/longhorn-reconcile/tests/test_longhorn_reconcile_no_flat_weekly.py
"""

import json
import re
from pathlib import Path

import longhorn_reconcile as lr
import pytest
from lib.proc_testing import run
from _reconcile_world import NS, P, PVC_CLASS, TIERS, World, _pvc, _volume

ROLE = Path(__file__).resolve().parents[1]
REPO = ROLE.parents[2]
LEGACY_CONSTANT = "LEGACY" + "_WEEKLY"
FLAT_GROUP = "weekly" + "-backup"
FLAT_GROUP_TOKEN = re.compile(re.escape(FLAT_GROUP) + r"(?![-\w])")


def _role_files() -> list[Path]:
    out = run(
        ["git", "ls-files", "--", str(ROLE.relative_to(REPO))], cwd=REPO, check=True
    ).stdout
    files = [REPO / line for line in out.splitlines()]
    files = [path for path in files if path != Path(__file__).resolve()]
    names = {path.name for path in files}
    assert {"longhorn_reconcile.py", "CLAUDE.md"} <= names, names
    return files


def _naming(pattern: re.Pattern, suffix: str) -> list[str]:
    return [
        f"{path.relative_to(REPO)}:{number}"
        for path in _role_files()
        if path.suffix == suffix
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]


def test_no_tracked_role_file_defines_or_reads_the_legacy_weekly_constant():
    """Verify-by: `grep -n LEGACY_WEEKLY` over the role returns nothing."""
    hits = [
        f"{path.relative_to(REPO)}:{number}"
        for path in _role_files()
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if LEGACY_CONSTANT in line
    ]
    assert hits == []


@pytest.mark.parametrize(
    "claim",
    ["homelab/radarr-config", "homelab/authelia-config"],
    ids=["claim-on-the-weekly-list", "claim-off-the-weekly-list"],
)
def test_a_volume_on_the_flat_weekly_label_is_no_longer_patched(claim, tmp_path):
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps(TIERS))
    env = {"RECONCILE_NAMESPACE": NS, "RECONCILE_TIERS_FILE": str(tiers)}
    world = World(
        [_volume("pvc-1", claim, FLAT_GROUP)],
        [_pvc(c, k) for c, k in PVC_CLASS.items()],
    )
    assert lr.main([], env, world) == 0
    assert world.writes() == []
    assert world.volumes["pvc-1"]["metadata"]["labels"] == {
        f"{P}{FLAT_GROUP}": "enabled"
    }


def test_the_reconcile_script_names_no_flat_weekly_group():
    """The module docstring's migration step and the group lists drop the flat label."""
    assert _naming(FLAT_GROUP_TOKEN, ".py") == []


def test_the_role_claude_md_names_no_flat_weekly_group():
    """The role CLAUDE.md's no-claim sentence and its written-keys list drop the flat label."""
    assert _naming(FLAT_GROUP_TOKEN, ".md") == []
