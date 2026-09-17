"""Every role `initial_setup.yml` includes sits where the deployer and land.sh can see it.

Written for issue #1916. `nut_host` lived at `ansible/roles/nut_host/` — outside both
`roles/setup/` and `roles/k8s/` — so PR #1915's change to its tasks matched no deploy tag,
no `_BROAD_SETUP_PREFIXES` entry and no `land_reach` role dir. The landing reported
`needs-manual-apply` for `initial_setup` alone, the deployer's `broad_applied` recorded the
same, and `/usr/local/bin/ups-secondary-health.sh` stayed `0755` on both hosts until a hand
run. The change landed on master and nothing said it was unapplied.

Both mappers derive the setup plane from one shape, `ansible/roles/setup/<role>/`:
`deploy_changes._SETUP_ROLE` on the deployer side, `land_reach._SETUP_ROLES_DIR` on the
landing side. `ansible.cfg`'s `roles_path` resolves a bare `role: <name>` across
`roles/containers`, `roles/setup` and `roles/`, so Ansible itself runs a role from any of the
three and neither mapper is told. This guard closes that gap from the playbook's side: each
role in `initial_setup.yml`'s `roles:` must route through `services_from_changed_paths` to
`setup_roles` and yield a `setup_tags_for` tag, and its directory must be the one
`land_reach` reads.

Red-proof pair on a synthetic tree under `tmp_path`; non-vacuity pins `nut_host` and
`initial_setup` in the live census so a renamed playbook cannot pass by including nothing.

Run: uv run pytest ansible/tests/setup/test_initial_setup_roles_are_visible_to_the_deployer.py
"""

import sys
from pathlib import Path

from _helpers import ANSIBLE, REPO
import yaml

sys.path.insert(0, str(REPO / "scripts" / "deploy_tools"))
import land_reach
from deploy_changes import services_from_changed_paths, setup_tags_for

INITIAL_SETUP_YML = ANSIBLE / "initial_setup.yml"
ROLES = ANSIBLE / "roles"

# The live playbook must include at least these; a census that finds neither is reading the
# wrong file, not a clean tree.
KNOWN_ROLES = frozenset({"initial_setup", "nut_host", "gitops_deploy"})


def _role_dir(role: str, roles_dir: Path) -> Path | None:
    """Where `roles_path` would find `role`: one or two levels under `roles_dir`."""
    for candidate in (roles_dir / role, *sorted(roles_dir.glob(f"*/{role}"))):
        if (candidate / "tasks").is_dir():
            return candidate
    return None


def invisible_roles(
    playbook: Path = INITIAL_SETUP_YML, roles_dir: Path = ROLES
) -> dict[str, str]:
    """{role: why} for every included role neither mapper would route to the setup plane."""
    problems: dict[str, str] = {}
    for role in land_reach._initial_setup_roles(playbook):
        role_dir = _role_dir(role, roles_dir)
        if role_dir is None:
            problems[role] = "no role directory under ansible/roles/"
            continue
        # The path the deployer sees in a push: repo-relative, as `git diff --name-only` prints
        # it. Computed from `roles_dir` so a synthetic tree reads the same as the real one.
        rel = (
            "ansible/roles/"
            + role_dir.relative_to(roles_dir).as_posix()
            + "/tasks/main.yml"
        )
        cs = services_from_changed_paths([rel])
        if role not in cs.setup_roles or not setup_tags_for([rel]):
            problems[role] = (
                f"{rel} does not route to the setup plane in deploy_changes.py "
                f"(setup_roles={sorted(cs.setup_roles)}, tags={sorted(setup_tags_for([rel]))})"
            )
            continue
        if role_dir != roles_dir / "setup" / role:
            problems[role] = (
                f"{role_dir} is not the ansible/roles/setup/<role> dir land_reach reads"
            )
    return problems


def test_every_initial_setup_role_is_visible_to_both_mappers():
    roles = set(land_reach._initial_setup_roles(INITIAL_SETUP_YML))
    assert KNOWN_ROLES <= roles, (
        f"census read the wrong playbook: missing {KNOWN_ROLES - roles}"
    )
    assert invisible_roles() == {}


def _synthetic_tree(tmp_path: Path, role_rel: str) -> tuple[Path, Path]:
    roles_dir = tmp_path / "ansible" / "roles"
    (roles_dir / role_rel / "tasks").mkdir(parents=True)
    (roles_dir / role_rel / "tasks" / "main.yml").write_text("- name: x\n  debug: {}\n")
    playbook = tmp_path / "ansible" / "initial_setup.yml"
    playbook.write_text(
        yaml.safe_dump(
            [{"hosts": "all", "roles": [{"role": "ups_side", "tags": ["ups_side"]}]}]
        )
    )
    return playbook, roles_dir


def test_a_role_under_roles_setup_is_clean(tmp_path):
    playbook, roles_dir = _synthetic_tree(tmp_path, "setup/ups_side")
    assert invisible_roles(playbook, roles_dir) == {}


def test_a_role_beside_roles_setup_is_flagged(tmp_path):
    playbook, roles_dir = _synthetic_tree(tmp_path, "ups_side")
    problems = invisible_roles(playbook, roles_dir)
    assert set(problems) == {"ups_side"}
    assert "does not route to the setup plane" in problems["ups_side"]


def test_a_role_with_no_directory_is_flagged(tmp_path):
    playbook, roles_dir = _synthetic_tree(tmp_path, "setup/other")
    problems = invisible_roles(playbook, roles_dir)
    assert problems == {"ups_side": "no role directory under ansible/roles/"}
