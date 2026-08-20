"""Unit tests for the filter that derives the auto-deploy denylist from role declarations.

The filter is the live safety boundary: whatever it returns becomes
K8S_AUTODEPLOY_DENYLIST, and a role absent from that list is auto-deployable. So every
test here that asserts a raise is asserting that a broken repo state fails the deploy
instead of silently widening what may be deployed unattended.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from ansible.errors import AnsibleFilterError

from k8s_autodeploy import SHARED_ROLES, k8s_autodeploy_denylist

_REPO = Path(__file__).resolve().parents[2]
_ANSIBLE = _REPO / "ansible"


def _role(tmp_path: Path, name: str, body: str | None) -> Path:
    """Create a role dir under tmp_path/roles/k8s, optionally with a defaults/main.yml."""
    role = tmp_path / "roles" / "k8s" / name
    role.mkdir(parents=True)
    if body is not None:
        defaults = role / "defaults"
        defaults.mkdir()
        (defaults / "main.yml").write_text(body)
    return role


_OK = 'k8s_autodeploy: false\nk8s_autodeploy_reason: "because"\n'
_OK_TRUE = 'k8s_autodeploy: true\nk8s_autodeploy_reason: "stateless, gated, probed"\n'


def test_returns_only_the_roles_declaring_false(tmp_path: Path) -> None:
    _role(tmp_path, "denied-one", _OK)
    _role(tmp_path, "denied-two", _OK)
    _role(tmp_path, "allowed", _OK_TRUE)
    assert k8s_autodeploy_denylist(str(tmp_path)) == ["denied-one", "denied-two"]


def test_result_is_sorted(tmp_path: Path) -> None:
    for name in ("zulu", "alpha", "mike"):
        _role(tmp_path, name, _OK)
    assert k8s_autodeploy_denylist(str(tmp_path)) == ["alpha", "mike", "zulu"]


def test_shared_roles_are_skipped_without_declaring(tmp_path: Path) -> None:
    for name in SHARED_ROLES:
        _role(tmp_path, name, None)
    _role(tmp_path, "denied", _OK)
    assert k8s_autodeploy_denylist(str(tmp_path)) == ["denied"]


def test_a_role_with_no_defaults_file_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "undeclared", None)
    with pytest.raises(AnsibleFilterError, match="undeclared"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_role_not_declaring_the_key_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "silent", "some_other_var: 1\n")
    with pytest.raises(AnsibleFilterError, match="silent"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_non_boolean_declaration_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "stringly", 'k8s_autodeploy: "false"\nk8s_autodeploy_reason: "x"\n')
    with pytest.raises(AnsibleFilterError, match="stringly"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_declaration_without_a_reason_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "unreasoned", "k8s_autodeploy: false\n")
    with pytest.raises(AnsibleFilterError, match="unreasoned"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_unparseable_yaml_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "broken", "k8s_autodeploy: false\n  bad: [indent\n")
    with pytest.raises(AnsibleFilterError, match="broken"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_an_empty_result_raises(tmp_path: Path) -> None:
    _role(tmp_path, "allowed", _OK_TRUE)
    with pytest.raises(AnsibleFilterError, match="EMPTY"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_missing_roles_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(AnsibleFilterError, match="no such directory"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_the_real_repo_derives_a_plausible_denylist() -> None:
    """Sanity-check against the live tree, by property rather than by restating the list."""
    denied = k8s_autodeploy_denylist(str(_ANSIBLE))
    assert denied == sorted(set(denied)), "duplicated or unsorted"
    # Roles whose exclusion is load-bearing: losing the ingress or auth plane removes the
    # ability to see or fix a failed deploy.
    for role in (
        "traefik",
        "authelia",
        "sonarr",
        "seed-volume",
        "code-server",
        "speedtest",
    ):
        assert role in denied
    # Every derived name must be a real role that really says false — catches a filter that
    # invents, mangles or mis-cases names.
    for role in denied:
        data = yaml.safe_load(
            (_ANSIBLE / "roles/k8s" / role / "defaults/main.yml").read_text()
        )
        assert data["k8s_autodeploy"] is False


def test_the_template_still_consumes_the_derived_variable() -> None:
    """The filter is only a safety boundary if the rendered config still reads it."""
    template = (
        _ANSIBLE / "roles/setup/gitops_deploy/templates/config.env.j2"
    ).read_text()
    assert (
        "K8S_AUTODEPLOY_DENYLIST={{ gitops_deploy_k8s_autodeploy_denylist | join(',') }}"
        in template
    )
