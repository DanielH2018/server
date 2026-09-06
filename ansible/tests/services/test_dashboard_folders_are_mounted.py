#!/usr/bin/env python3
"""A dashboard in an unlisted folder is provisioned nowhere, silently.

`tasks/dashboards.yml` bakes one ConfigMap per name in `claude_otel_dashboard_folders`, and the
Deployment mounts each by EXPLICIT per-folder volume name (see the comment above that variable in
`defaults/main.yml`). A board dropped into `files/dashboards/<folder>/` whose folder is not on that
list is therefore never baked and never mounted: the deploy is green, Grafana is Ready, and the
board simply does not exist. Nothing else catches it — `scripts/validate/grafana_dashboards.py`
walks the files on disk and never reads the folder list, so it passes on exactly this input.

Paired, per the repo's red-proof rule: one tree the rule must accept, one it must reject.

Run: uv run pytest ansible/tests/services/test_dashboard_folders_are_mounted.py
"""

from pathlib import Path

import pytest
from _helpers import ANSIBLE
from _helpers import load_yaml

ROLE = ANSIBLE / "roles" / "k8s" / "claude-otel"
DASHBOARDS_DIR = ROLE / "files" / "dashboards"

# The folders that must still exist, named rather than counted. A census that globs for its own
# subject returns an empty set once the tree moves, and `all()` over nothing passes.
KNOWN_FOLDERS = frozenset(
    {"AI", "Apps", "Infrastructure", "Logs", "Networking", "Security"}
)

# Boards this suite knows are provisioned, so a rename or a move out of a mounted folder fails
# here rather than at "the board is missing from Grafana".
KNOWN_BOARDS = frozenset(
    {
        "Apps/exportarr-arr-stack.json",
        "Apps/speedtest-tracker.json",
        "Apps/uptime-kuma-monitors.json",
        "Infrastructure/longhorn-storage.json",
        "Security/crowdsec-overview.json",
    }
)


def unmounted_folders(dashboards_dir: Path, provisioned: list[str]) -> set[str]:
    """Folders holding a board that no per-folder ConfigMap is built for."""
    holding = {p.parent.name for p in dashboards_dir.rglob("*.json")}
    return holding - set(provisioned)


def provisioned_folders() -> list[str]:
    return load_yaml(ROLE / "defaults" / "main.yml")["claude_otel_dashboard_folders"]


def test_every_folder_holding_a_board_is_provisioned():
    """The accepting half, against the real tree."""
    assert unmounted_folders(DASHBOARDS_DIR, provisioned_folders()) == set()


def test_the_known_folders_and_boards_are_all_still_there():
    """Non-vacuity. Both assertions above walk a glob, and an empty glob passes them."""
    assert KNOWN_FOLDERS <= set(provisioned_folders())
    on_disk = {
        str(p.relative_to(DASHBOARDS_DIR)) for p in DASHBOARDS_DIR.rglob("*.json")
    }
    assert KNOWN_BOARDS <= on_disk


def test_a_board_in_an_unlisted_folder_is_flagged(tmp_path):
    """The rejecting half. A rule that flagged nothing would pass the two tests above."""
    (tmp_path / "Apps").mkdir()
    (tmp_path / "Apps" / "kept.json").write_text("{}")
    (tmp_path / "Strays").mkdir()
    (tmp_path / "Strays" / "lost.json").write_text("{}")

    assert unmounted_folders(tmp_path, ["Apps"]) == {"Strays"}


@pytest.mark.parametrize("board", sorted(KNOWN_BOARDS))
def test_each_known_board_parses_as_json(board):
    """A board that fails to parse is baked into the ConfigMap anyway and 500s on load."""
    import json

    json.loads((DASHBOARDS_DIR / board).read_text())
