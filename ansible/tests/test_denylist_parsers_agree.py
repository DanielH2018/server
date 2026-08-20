"""The deployer's regex reader and the Ansible filter must agree on the live tree.

The filter decides the denylist; the deployer's reader only detects that a host's rendered
config has gone stale against it. If the two drift, that detection false-alarms and disarms
auto-deploy on a host that is actually converged — so pin them together against the real roles.
"""

from __future__ import annotations

from pathlib import Path

from deploy_logic import SHARED_K8S_ROLES, declared_denylist
from k8s_autodeploy import SHARED_ROLES, k8s_autodeploy_denylist

_REPO = Path(__file__).resolve().parents[2]
_ANSIBLE = _REPO / "ansible"
_K8S_ROLES = _ANSIBLE / "roles/k8s"


def _sources() -> dict[str, str | None]:
    sources: dict[str, str | None] = {}
    for role in sorted(p for p in _K8S_ROLES.iterdir() if p.is_dir()):
        defaults = role / "defaults/main.yml"
        sources[role.name] = defaults.read_text() if defaults.is_file() else None
    return sources


def test_the_shared_role_sets_match() -> None:
    assert SHARED_K8S_ROLES == SHARED_ROLES


def test_both_readers_derive_the_same_denylist() -> None:
    assert declared_denylist(_sources()) == frozenset(
        k8s_autodeploy_denylist(str(_ANSIBLE))
    )


def test_the_regex_reader_is_biased_toward_denied() -> None:
    """A role the regex cannot read must be denied, never permitted.

    Asserted as a property rather than by example, so it keeps holding as the real tree changes.
    """
    for text in ("k8s_autodeploy: maybe\n", "", "no_declaration_here: 1\n"):
        assert declared_denylist({"probe": text}) == frozenset({"probe"})
