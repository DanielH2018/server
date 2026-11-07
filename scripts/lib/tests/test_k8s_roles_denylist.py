"""The derivation of the `k8s_autodeploy` denylist from each role's declaration.

renovate.json's no-automerge rule and the Renovate lander read this list, and a role absent
from it is eligible for automerge. So every test here that asserts a raise is asserting that a
broken repo state refuses a landing instead of widening what lands unattended.
"""

import os
from pathlib import Path

import pytest
from lib import yaml_fast
from lib.k8s_roles import is_leftover_dir, k8s_autodeploy_denylist
from lib.repo_paths import ANSIBLE as _ANSIBLE


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
    _role(tmp_path, "denied", _OK)
    assert k8s_autodeploy_denylist(str(tmp_path)) == ["denied"]


def test_a_directory_holding_only_pycache_is_not_a_role(tmp_path: Path) -> None:
    """A retired role's gitignored `__pycache__/` must not read as an undeclared role.

    The fast-forward that retires a role removes its tracked files; a `__pycache__/` left by
    a pytest run in the primary checkout keeps the directory itself alive. Reading that shell
    as a role would raise, which refuses every Renovate landing.
    """
    _role(tmp_path, "denied", _OK)
    ghost = _role(tmp_path, "retired", None)
    pycache = ghost / "files" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "check.cpython-312.pyc").write_bytes(b"\x00")
    assert k8s_autodeploy_denylist(str(tmp_path)) == ["denied"]


def test_a_directory_holding_a_real_file_is_still_a_role(tmp_path: Path) -> None:
    """The other half of the pair: only an all-debris directory is skipped.

    A role that carries any tracked file still has to declare a stance, so the skip above
    cannot widen what may land unattended.
    """
    _role(tmp_path, "denied", _OK)
    undeclared = _role(tmp_path, "half-retired", None)
    pycache = undeclared / "files" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "check.cpython-312.pyc").write_bytes(b"\x00")
    (undeclared / "files" / "check.py").write_text("x = 1\n")
    with pytest.raises(ValueError, match=r"half-retired.*has no defaults/main\.yml"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_role_with_no_defaults_file_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "undeclared", None)
    with pytest.raises(ValueError, match=r"undeclared.*has no defaults/main\.yml"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_role_not_declaring_the_key_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "silent", "some_other_var: 1\n")
    with pytest.raises(ValueError, match=r"silent.*does not set"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_non_dict_yaml_raises(tmp_path: Path) -> None:
    """A bare top-level list has no `k8s_autodeploy` key to find — same failure as no key."""
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "listy", "- one\n- two\n")
    with pytest.raises(ValueError, match=r"listy.*does not set"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_non_boolean_declaration_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "stringly", 'k8s_autodeploy: "false"\nk8s_autodeploy_reason: "x"\n')
    with pytest.raises(ValueError, match=r"stringly.*not a boolean"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_declaration_without_a_reason_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "unreasoned", "k8s_autodeploy: false\n")
    with pytest.raises(ValueError, match=r"unreasoned.*but no"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_whitespace_only_reason_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    _role(
        tmp_path, "blankreason", 'k8s_autodeploy: false\nk8s_autodeploy_reason: "   "\n'
    )
    with pytest.raises(ValueError, match=r"blankreason.*but no"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_unparseable_yaml_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    _role(tmp_path, "broken", "k8s_autodeploy: false\n  bad: [indent\n")
    with pytest.raises(ValueError, match=r"cannot read.*broken"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_non_utf8_bytes_raise(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    role = _role(tmp_path, "badbytes", _OK)
    (role / "defaults" / "main.yml").write_bytes(b"k8s_autodeploy: false\n\xff\xfe\n")
    with pytest.raises(ValueError, match=r"cannot read.*badbytes"):
        k8s_autodeploy_denylist(str(tmp_path))


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 file")
def test_permission_denied_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    role = _role(tmp_path, "locked", _OK)
    defaults_file = role / "defaults" / "main.yml"
    defaults_file.chmod(0o000)
    try:
        with pytest.raises(ValueError, match=r"cannot read.*locked"):
            k8s_autodeploy_denylist(str(tmp_path))
    finally:
        defaults_file.chmod(0o644)


def test_a_dangling_symlink_raises(tmp_path: Path) -> None:
    _role(tmp_path, "denied", _OK)
    roles_dir = tmp_path / "roles" / "k8s"
    (roles_dir / "ghost").symlink_to(roles_dir / "nonexistent-target")
    with pytest.raises(ValueError, match=r"ghost.*not a directory.*symlink"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_non_directory_dotfile_is_skipped(tmp_path: Path) -> None:
    """Editor/VCS cruft (a stray .gitkeep etc.) is not a role and must not raise."""
    _role(tmp_path, "denied", _OK)
    (tmp_path / "roles" / "k8s" / ".gitkeep").write_text("")
    assert k8s_autodeploy_denylist(str(tmp_path)) == ["denied"]


def test_a_stray_pycache_directory_is_skipped(tmp_path: Path) -> None:
    """A stray __pycache__ (or dotdir) under roles/k8s/ is not a role and must not raise."""
    _role(tmp_path, "denied", _OK)
    (tmp_path / "roles" / "k8s" / "__pycache__").mkdir()
    assert k8s_autodeploy_denylist(str(tmp_path)) == ["denied"]


def test_an_empty_result_raises(tmp_path: Path) -> None:
    _role(tmp_path, "allowed", _OK_TRUE)
    with pytest.raises(ValueError, match="derived an EMPTY denylist"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_a_missing_roles_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no such directory"):
        k8s_autodeploy_denylist(str(tmp_path))


def test_the_real_repo_derives_a_plausible_denylist() -> None:
    """Sanity-check against the live tree, by property rather than by restating the list."""
    denied = k8s_autodeploy_denylist(str(_ANSIBLE))
    assert denied == sorted(set(denied)), "duplicated or unsorted"
    # Roles whose exclusion is load-bearing: losing the ingress or auth plane removes the
    # sonarr and speedtest are asserted OUT of this list below, not just dropped, so a re-denial
    # of either fails loud rather than reading as this test not having been updated.
    for role in (
        "traefik",
        "authelia",
        "code-server",
    ):
        assert role in denied
    # A SECOND, DISTINCT denial category — state coupled to something OUTSIDE the volume, so
    # the pre-apply snapshot and revert cannot see it and a revert can desynchronise the two.
    # Not the load-bearing reasons above, and not "migrating state with no recovery point"
    # either (the snapshot/revert covers that for these three). Each is denied for the coupling
    # question asked per service:
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
    # A FOURTH denial reason, tdarr alone. The snapshot/revert machinery works fine for tdarr —
    # this is not the trio's hazard recurring — the compounding reasons are specific to it: it
    # also mounts media-data (shared RWX, never reverted) and rewrites library files there IN
    # PLACE, so a revert can't undo an already-committed transcode; and its own two claims
    # (tdarr-configs, tdarr-server) revert non-atomically. The denylist also turns Renovate's
    # automerge off for it (test_renovate_automerge_follows_the_autodeploy_denylist.py).
    # Checked by name for the same reason as the groups above.
    for role in ("tdarr",):
        assert role in denied
    # The promoted roles, checked by name so a regression reads as
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
        data = yaml_fast.safe_load(
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
        and not is_leftover_dir(str(role_dir))
        and (role_dir / "defaults" / "main.yml").is_file()
        and (
            yaml_fast.safe_load((role_dir / "defaults" / "main.yml").read_text()) or {}
        ).get("k8s_autodeploy")
        is False
    ]
    assert len(denied) == len(actually_false)
    # 36 measured (uv run python scripts/dev/k8s_autodeploy_counts.py). The floor sits two below
    # the measured count — enough to catch an accidental bulk flip without needing an edit for
    # every single-role promotion.
    assert len(denied) >= 34
