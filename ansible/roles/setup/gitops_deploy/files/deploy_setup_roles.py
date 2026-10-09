# ansible/roles/setup/gitops_deploy/files/deploy_setup_roles.py
"""Which playbook and `--tags` value apply a setup role, and whether the tick's run applies it.

Split out of `deploy_changes` at the module-length cap. `deploy_changes` re-exports every name,
so its readers keep their imports.
"""

from __future__ import annotations

from gitops_markers import SETUP_ROLES_OFF_THE_TICK_HOST

# Setup roles `ansible/initial_setup.yml` does NOT include, mapped to the playbook that does.
# `None` means no playbook includes the role at all.
#
# THE BUG THIS EXISTS TO KILL: `--tags` matching no task makes Ansible exit 0, so a guessed tag
# records an apply of nothing (PR #702; `docs/gitops-pipeline.md`, *Broad changes*).
#
# `common` is the sharper shape: no playbook includes it, yet roles on two hosts read its files
# by path. Its resolv.conf.j2 applies twice, via k3s-bringup.yml on daniel-box and via
# initial_setup.yml on daniel-pi.
_SETUP_ROLES_OUTSIDE_INITIAL_SETUP: dict[str, str | None] = {
    "k3s": "ansible/k3s-bringup.yml",
    "common": None,
}
# Setup roles whose `--tags` value is not their directory name. Same silent-exit-0 failure:
# `--tags chezmoi_setup` matches nothing, because the playbook tags that role `chezmoi`.
_SETUP_ROLE_TAG_OVERRIDES = {"chezmoi_setup": "chezmoi"}

INITIAL_SETUP = "ansible/initial_setup.yml"


def setup_role_playbook(role: str) -> str | None:
    """The playbook that applies a setup role, or None when no playbook includes it."""
    if role in _SETUP_ROLES_OUTSIDE_INITIAL_SETUP:
        return _SETUP_ROLES_OUTSIDE_INITIAL_SETUP[role]
    return INITIAL_SETUP


def tick_applies_setup_role(role: str) -> bool:
    """Whether the tick's own `initial_setup.yml --tags <role>` run applies `role`."""
    playbook = setup_role_playbook(role)
    return playbook == INITIAL_SETUP and role not in SETUP_ROLES_OFF_THE_TICK_HOST


def setup_role_tag(role: str) -> str:
    """The `--tags` value that actually selects a setup role, which is not always its name."""
    return _SETUP_ROLE_TAG_OVERRIDES.get(role, role)
