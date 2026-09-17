"""PR #1915's shape lands reported: `nut_host` is a setup role both mappers can see.

PR #1915 changed `ansible/roles/nut_host/tasks/main.yml`. The role sat outside the
`ansible/roles/setup/<role>/` shape that `deploy_changes.py` (`_SETUP_ROLE`) and `land_reach`
(`_SETUP_ROLES_DIR`) both derive the setup plane from, so it derived no tag, no setup role and
no note: the landing named `initial_setup` alone, the deployer's `broad_applied` recorded the
same, and the change sat unapplied on daniel-box and daniel-server until a hand run (issue
#1916). The role moved under `roles/setup/` on 2026-09-17;
`ansible/tests/setup/test_initial_setup_roles_are_visible_to_the_deployer.py` refuses a
sibling in the old position. This file pins what the landing now says for that PR's path.

Run: uv run pytest scripts/deploy_tools/tests/test_land_nut_host_reaches_both_hosts.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import land_reach
import land_tags

PR_1915_ROLE_FILE = "ansible/roles/setup/nut_host/tasks/main.yml"


def test_nut_host_reaches_daniel_box_and_daniel_server():
    assert land_reach.setup_role_hosts("nut_host") == {"daniel-box", "daniel-server"}


def test_the_tick_self_applies_nut_host_on_its_own_host():
    files = [PR_1915_ROLE_FILE]
    assert land_tags.self_applied(files) is True
    assert "--tags nut_host" in land_tags.self_applied_command(files)
    # Routable, so not owed to a hand by plane_note -- the deployer's own state grades it.
    assert land_tags.plane_note(files) == ""


def test_the_landing_owes_daniel_server_the_same_run():
    note = land_reach.remaining_setup_hosts_note([PR_1915_ROLE_FILE], "daniel-box")
    assert "nut_host" in note
    assert "daniel-server" in note
    assert "daniel-pi" not in note
