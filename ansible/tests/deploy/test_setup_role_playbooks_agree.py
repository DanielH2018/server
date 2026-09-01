"""Guard: deploy_logic's setup-role routing must match the playbooks that actually exist.

WHY THE ROUTING IS A CONSTANT AT ALL. `deploy_logic` runs on the host under
`uv run --no-project`, so it cannot import yaml and cannot parse a playbook. It carries a small
hand-written map instead, and a hand-written map rots — so this test derives the truth from the
playbooks and fails when the two disagree.

WHAT ROTS, AND WHY IT IS SILENT. `--tags` matching nothing makes `ansible-playbook` exit 0. A
wrong playbook or a wrong tag therefore produces a green run that changes nothing, and the
deployer records the change as applied. That is not hypothetical: on 2026-09-01 a
`roles/setup/k3s/` change (PR #702, a host DNS forwarder) was auto-applied as
`initial_setup.yml --tags k3s`, which matches no task there because the role appears only in
`k3s-bringup.yml`. The forwarder had to be installed by hand, and nothing reported the gap.

Adding a setup role, moving one between playbooks, or renaming a role's tag all fail here.
"""

from __future__ import annotations

import re

from deploy_logic import setup_role_playbook, setup_role_tag
from _helpers import REPO as _REPO

_ANSIBLE = _REPO / "ansible"
_SETUP_ROLES_DIR = _ANSIBLE / "roles/setup"

# Playbooks that may include a setup role. Parsed textually rather than with yaml, to stay
# readable against the same `- { role: x, tags: ["y"] }` one-liners the playbooks are written in.
_PLAYBOOKS = ("initial_setup.yml", "k3s-bringup.yml", "bootstrap.yml")

_ROLE_ENTRY = re.compile(
    r"""^\s*-\s*\{\s*role:\s*(?P<role>[a-z0-9_]+)\s*,\s*tags:\s*\[\s*["'](?P<tag>[^"']+)["']""",
    re.M,
)


def roles_declared_in_playbooks() -> dict[str, set[tuple[str, str]]]:
    """role dir -> every (playbook, tag) that includes it.

    A SET, not one entry: `sops_setup` is in both `bootstrap.yml` (onboarding a host that
    cannot decrypt yet) and `initial_setup.yml` (the maintenance path). Keying one per role
    silently picked whichever file was parsed last.
    """
    found: dict[str, set[tuple[str, str]]] = {}
    for name in _PLAYBOOKS:
        path = _ANSIBLE / name
        if not path.exists():
            continue
        for m in _ROLE_ENTRY.finditer(path.read_text()):
            role = m.group("role")
            if not (_SETUP_ROLES_DIR / role).is_dir():
                continue  # a role from another plane; this map only covers roles/setup/
            found.setdefault(role, set()).add((f"ansible/{name}", m.group("tag")))
    return found


def test_every_setup_role_routes_to_a_playbook_that_includes_it() -> None:
    declared = roles_declared_in_playbooks()
    problems = []
    for role_dir in sorted(p.name for p in _SETUP_ROLES_DIR.iterdir() if p.is_dir()):
        routed = setup_role_playbook(role_dir)
        if role_dir not in declared:
            if routed is not None:
                problems.append(
                    f"{role_dir}: routed to {routed}, but no playbook includes it — a run "
                    "there would exit 0 having matched nothing"
                )
            continue
        entries = declared[role_dir]
        if (routed, setup_role_tag(role_dir)) not in entries:
            where = ", ".join(f"{pb} --tags {tag}" for pb, tag in sorted(entries))
            problems.append(
                f"{role_dir}: routed to {routed} --tags {setup_role_tag(role_dir)}, which "
                f"no playbook declares. Declared: {where}"
            )
    assert not problems, (
        "setup-role routing disagrees with the playbooks:\n  " + "\n  ".join(problems)
    )


def test_the_playbook_parser_still_finds_roles() -> None:
    """A regex that silently stopped matching would make the test above vacuously green."""
    declared = roles_declared_in_playbooks()
    assert "sops_setup" in declared, (
        "the role-entry regex no longer matches initial_setup.yml"
    )
    assert ("ansible/initial_setup.yml", "sops_setup") in declared["sops_setup"]


def test_the_known_exceptions_are_still_exceptional() -> None:
    """Pins the two shapes that made this guard necessary, so a fix cannot quietly undo them."""
    declared = roles_declared_in_playbooks()
    assert declared.get("k3s") == {("ansible/k3s-bringup.yml", "k3s")}
    assert "common" not in declared, (
        "setup/common gained a playbook; update the routing map"
    )
