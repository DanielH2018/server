"""Unit tests for the filter that derives the auto-deploy denylist from role declarations.

The filter is the live safety boundary: whatever it returns becomes
K8S_AUTODEPLOY_DENYLIST, and a role absent from that list is auto-deployable. So every
test here that asserts a raise is asserting that a broken repo state fails the deploy
instead of silently widening what may be deployed unattended.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from ansible.errors import AnsibleFilterError

from k8s_autodeploy import SHARED_ROLES, k8s_autodeploy_denylist
from _helpers import REPO as _REPO


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


def _seed_shared_roles(tmp_path: Path) -> None:
    """Create every SHARED_ROLES member as a bare dir, satisfying the invariant check."""
    for name in SHARED_ROLES:
        _role(tmp_path, name, None)


def test_returns_only_the_roles_declaring_false(tmp_path: Path) -> None:
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied-one", _OK)
    _role(tmp_path, "denied-two", _OK)
    _role(tmp_path, "allowed", _OK_TRUE)
    assert k8s_autodeploy_denylist(str(tmp_path)) == ["denied-one", "denied-two"]


def test_result_is_sorted(tmp_path: Path) -> None:
    _seed_shared_roles(tmp_path)
    for name in ("zulu", "alpha", "mike"):
        _role(tmp_path, name, _OK)
    assert k8s_autodeploy_denylist(str(tmp_path)) == ["alpha", "mike", "zulu"]


def test_shared_roles_are_skipped_without_declaring(tmp_path: Path) -> None:
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied", _OK)
    assert k8s_autodeploy_denylist(str(tmp_path)) == ["denied"]


def test_a_role_with_no_defaults_file_raises(tmp_path: Path) -> None:
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "undeclared", None)
    with pytest.raises(
        AnsibleFilterError, match="undeclared.*has no defaults/main.yml"
    ):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_role_not_declaring_the_key_raises(tmp_path: Path) -> None:
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "silent", "some_other_var: 1\n")
    with pytest.raises(AnsibleFilterError, match="silent.*does not set"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_non_dict_yaml_raises(tmp_path: Path) -> None:
    """A bare top-level list has no `k8s_autodeploy` key to find — same failure as no key."""
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "listy", "- one\n- two\n")
    with pytest.raises(AnsibleFilterError, match="listy.*does not set"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_non_boolean_declaration_raises(tmp_path: Path) -> None:
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "stringly", 'k8s_autodeploy: "false"\nk8s_autodeploy_reason: "x"\n')
    with pytest.raises(AnsibleFilterError, match="stringly.*not a boolean"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_declaration_without_a_reason_raises(tmp_path: Path) -> None:
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "unreasoned", "k8s_autodeploy: false\n")
    with pytest.raises(AnsibleFilterError, match="unreasoned.*but no"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_whitespace_only_reason_raises(tmp_path: Path) -> None:
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied", _OK)
    _role(
        tmp_path, "blankreason", 'k8s_autodeploy: false\nk8s_autodeploy_reason: "   "\n'
    )
    with pytest.raises(AnsibleFilterError, match="blankreason.*but no"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_unparseable_yaml_raises(tmp_path: Path) -> None:
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "broken", "k8s_autodeploy: false\n  bad: [indent\n")
    with pytest.raises(AnsibleFilterError, match="cannot read.*broken"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_non_utf8_bytes_raise(tmp_path: Path) -> None:
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied", _OK)
    role = _role(tmp_path, "badbytes", _OK)
    (role / "defaults" / "main.yml").write_bytes(b"k8s_autodeploy: false\n\xff\xfe\n")
    with pytest.raises(AnsibleFilterError, match="cannot read.*badbytes"):
        k8s_autodeploy_denylist(str(tmp_path))


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 file")
def test_permission_denied_raises(tmp_path: Path) -> None:
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied", _OK)
    role = _role(tmp_path, "locked", _OK)
    defaults_file = role / "defaults" / "main.yml"
    defaults_file.chmod(0o000)
    try:
        with pytest.raises(AnsibleFilterError, match="cannot read.*locked"):
            k8s_autodeploy_denylist(str(tmp_path))
    finally:
        defaults_file.chmod(0o644)


def test_a_dangling_symlink_raises(tmp_path: Path) -> None:
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied", _OK)
    roles_dir = tmp_path / "roles" / "k8s"
    (roles_dir / "ghost").symlink_to(roles_dir / "nonexistent-target")
    with pytest.raises(AnsibleFilterError, match="ghost.*not a directory.*symlink"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_non_directory_dotfile_is_skipped(tmp_path: Path) -> None:
    """Editor/VCS cruft (a stray .gitkeep etc.) is not a role and must not raise."""
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied", _OK)
    (tmp_path / "roles" / "k8s" / ".gitkeep").write_text("")
    assert k8s_autodeploy_denylist(str(tmp_path)) == ["denied"]


def test_a_stray_pycache_directory_is_skipped(tmp_path: Path) -> None:
    """A stray __pycache__ (or dotdir) under roles/k8s/ is not a role and must not raise."""
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "denied", _OK)
    (tmp_path / "roles" / "k8s" / "__pycache__").mkdir()
    assert k8s_autodeploy_denylist(str(tmp_path)) == ["denied"]


def test_an_empty_result_raises(tmp_path: Path) -> None:
    _seed_shared_roles(tmp_path)
    _role(tmp_path, "allowed", _OK_TRUE)
    with pytest.raises(AnsibleFilterError, match="derived an EMPTY denylist"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_missing_roles_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(AnsibleFilterError, match="no such directory"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_shared_role_that_does_not_exist_raises(tmp_path: Path) -> None:
    """Only seed one of the two SHARED_ROLES members — the missing one must raise, not
    silently exclude nothing."""
    (tmp_path / "roles" / "k8s").mkdir(parents=True)
    present = next(iter(SHARED_ROLES))
    _role(tmp_path, present, None)
    _role(tmp_path, "denied", _OK)
    with pytest.raises(
        AnsibleFilterError, match="SHARED_ROLES member.*is not a directory"
    ):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_shared_role_pinning_an_image_key_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    for name in SHARED_ROLES:
        body = (
            "widget_image: alpine:3.24\n" if name == next(iter(SHARED_ROLES)) else None
        )
        _role(tmp_path, name, body)
    with pytest.raises(AnsibleFilterError, match="SHARED_ROLES member.*pins"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_the_real_repo_derives_a_plausible_denylist() -> None:
    """Sanity-check against the live tree, by property rather than by restating the list."""
    denied = k8s_autodeploy_denylist(str(_ANSIBLE))
    assert denied == sorted(set(denied)), "duplicated or unsorted"
    # Roles whose exclusion is load-bearing: losing the ingress or auth plane removes the
    # ability to see or fix a failed deploy. sonarr and speedtest sat here until slice 7b task 7
    # promoted them (and seven siblings) behind the pre-apply Longhorn snapshot and revert — they
    # are asserted OUT of this list below, not just dropped, so a re-denial of either fails loud
    # rather than reading as this test simply not having been updated.
    for role in (
        "traefik",
        "authelia",
        "volume-claim",
        "code-server",
    ):
        assert role in denied
    # A SECOND, DISTINCT denial category — state coupled to something OUTSIDE the volume, so
    # the pre-apply snapshot and revert cannot see it and a revert can desynchronise the two.
    # Not the load-bearing reasons above, and not "migrating state with no recovery point"
    # either (that is what the snapshot/revert solved for these three same roles). Slice 7b task
    # 7 promoted all three on deploy mechanics alone; the scope decision the same day (2026-08-22)
    # held them back once someone asked the coupling question per service:
    #   - zigbee2mqtt — the SLZB-06M coordinator's own NVRAM (network key, frame counters)
    #   - livesync    — CouchDB revisions already synced to connected Obsidian clients
    #   - qbittorrent — the media-data volume qbittorrent-config's bookkeeping references
    # Checked by name, like the load-bearing group above, so a re-promotion fails loud instead
    # of reading as this test not having been updated.
    for role in (
        "livesync",
        "qbittorrent",
        "zigbee2mqtt",
    ):
        assert role in denied
    # A FOURTH denial reason, tdarr alone. Slice 7b task 7 promoted it with the other eight on
    # deploy mechanics; two read-only audits held it back the same day (2026-08-22), after this
    # one. The snapshot/revert machinery works fine for tdarr — this is not the trio's hazard
    # recurring — the compounding reasons are specific to it: it also mounts media-data (shared
    # RWX, never reverted) and rewrites library files there IN PLACE, so a revert can't undo an
    # already-committed transcode; its own two claims (tdarr-configs, tdarr-server) revert
    # non-atomically; and the digest pin's "stays manual" intent is unenforced by renovate.json,
    # which automerges a digest re-push after a 3-day soak. Checked by name for the same reason
    # as the groups above.
    for role in ("tdarr",):
        assert role in denied
    # The eight slice 7b roles actually left promoted, checked by name so a regression reads as
    # a specific assertion failure rather than a shrunk floor further down.
    for role in (
        "bazarr",
        "freshrss",
        "home-assistant",
        "jellyfin",
        "prowlarr",
        "radarr",
        "sonarr",
        "speedtest",
    ):
        assert role not in denied
    # Every derived name must be a real role that really says false — catches a filter that
    # invents, mangles or mis-cases names.
    for role in denied:
        data = yaml.safe_load(
            (_ANSIBLE / "roles/k8s" / role / "defaults/main.yml").read_text()
        )
        assert data["k8s_autodeploy"] is False
    # The named-role assertions above move WITH a bulk flip — flip sonarr/etc to true and
    # they simply stop being asserted, not caught. This count is what actually catches that:
    # it is derived independently, by walking roles/k8s/ in this test rather than trusting
    # the filter's own count, so a bulk flip of several roles to true shrinks it and fails
    # here even though every assertion above still passes. Do not remove or loosen this floor
    # to "simplify" the test — it is the one assertion that can't move with the thing it checks.
    k8s_roles_dir = _ANSIBLE / "roles/k8s"
    actually_false = [
        role_dir.name
        for role_dir in sorted(k8s_roles_dir.iterdir())
        if role_dir.is_dir()
        and not role_dir.name.startswith(".")
        and role_dir.name != "__pycache__"
        and role_dir.name not in SHARED_ROLES
        and (role_dir / "defaults" / "main.yml").is_file()
        and (
            yaml.safe_load((role_dir / "defaults" / "main.yml").read_text()) or {}
        ).get("k8s_autodeploy")
        is False
    ]
    assert len(denied) == len(actually_false)
    # Measured 36 on 2026-08-22 (uv run python scripts/dev/k8s_autodeploy_counts.py): slice 7b task
    # 7's twelve promotions dropped this from 44 to 32, the same-day scope decision re-denied
    # three of the twelve for state coupling (35), then the two post-7b audits re-denied a
    # fourth, tdarr, for a compounding reason of its own (36). The floor sits two below the
    # measured count — enough to catch an accidental bulk flip without needing an edit for every
    # single-role promotion.
    assert len(denied) >= 34


def test_the_template_still_consumes_the_derived_variable() -> None:
    """The filter is only a safety boundary if the rendered config still reads it."""
    template = (
        _ANSIBLE / "roles/setup/gitops_deploy/templates/config.env.j2"
    ).read_text()
    assert (
        "K8S_AUTODEPLOY_DENYLIST={{ gitops_deploy_k8s_autodeploy_denylist | join(',') }}"
        in template
    )
