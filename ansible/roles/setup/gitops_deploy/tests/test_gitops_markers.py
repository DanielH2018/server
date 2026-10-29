"""The shared marker parsers: one good line and the garbage each must read as nothing.

There is one parser per format, so what to pin is each parser's own must-fire / must-not-fire pair: a
reader that guessed at a torn line would page on a lock or a role nobody can find.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_markers.py
"""

import pytest

from lib.ansible_inventory import inventory_hosts
from lib.repo_paths import HOSTS_INI
from gitops_markers import (
    CONTENTION_CLEAR_CMD,
    LOCAL_CONNECTION_HOSTS,
    SHARED_K8S_ROLES,
    ContentionEntry,
    on_hosts,
    owed_clear_cmd,
    parse_behind,
    parse_contention,
)

_SHA = "abc1230000000000000000000000000000000000"


# ── behind_since ──────────────────────────────────────────────────────────────────────────
def test_a_behind_line_yields_the_sha_and_the_stamp():
    assert parse_behind(f"{_SHA} 1000") == (_SHA, 1000.0)


@pytest.mark.parametrize("text", [None, "", _SHA, f"{_SHA} not-a-stamp", "a b c"])
def test_a_garbled_behind_marker_reads_as_not_behind(text):
    assert parse_behind(text) is None


# ── contention_since ──────────────────────────────────────────────────────────────────────
def test_a_contention_line_yields_the_whole_streak():
    assert parse_contention(f"{_SHA} sonarr 1000.0 1900.0 2") == ContentionEntry(
        _SHA, "sonarr", 1000.0, 1900.0, 2
    )


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        f"{_SHA} sonarr not-a-stamp 1900.0 2",
        "four fields only here",
        "a b c d e f",
    ],
)
def test_a_garbled_contention_marker_reads_as_no_streak(text):
    assert parse_contention(text) is None


# ── the clear commands every surface prints ───────────────────────────────────────────────
def test_the_clear_commands_name_the_gitops_state_subcommands():
    assert owed_clear_cmd("k8s_deferred", "sonarr").endswith(
        "gitops_state.py clear-owed k8s_deferred sonarr"
    )
    assert CONTENTION_CLEAR_CMD.endswith("clear-contention")


def test_an_owed_clear_names_applied_tags_only_where_the_cli_takes_them():
    """`gitops_state.py` refuses `--applied` off `manual_plane`, so it must never be printed."""
    assert owed_clear_cmd("manual_plane", "k3s", {"k3s", "kubeconfig"}).endswith(
        "clear-owed manual_plane k3s --applied kubeconfig"
    )
    with pytest.raises(ValueError, match="manual_plane only"):
        owed_clear_cmd("k8s_unapplied", "authelia", {"authelia"})


# ── the shared k8s roles ──────────────────────────────────────────────────────────────────
def test_the_shared_k8s_roles_are_the_roles_with_no_containers_list_entry():
    """The literal the banner reads, against the derivation it stands in for (#4373)."""
    from lib.k8s_roles import k8s_entries, role_dirs

    derived = {p.name for p in role_dirs()} - set(k8s_entries())
    assert "manifests" in derived
    assert SHARED_K8S_ROLES == derived


_APPLY = "uv run ansible-playbook ansible/initial_setup.yml --tags hypervisor"


def test_a_local_connection_host_is_applied_over_ssh_from_its_own_pulled_checkout():
    """`-e target=daniel-server` from daniel-box runs the play on daniel-box (#6510)."""
    assert on_hosts(_APPLY, ("daniel-server",)) == (
        f'ssh daniel-server "cd /home/ubuntu/server && git pull --ff-only && {_APPLY}"'
    )


def test_an_ssh_host_is_targeted_from_the_operators_checkout():
    """The rejecting half: daniel-pi is driven over ssh by the play, so it keeps `-e target`."""
    assert on_hosts(_APPLY, ("daniel-pi",)) == f"{_APPLY} -e target=daniel-pi"


def test_no_host_leaves_the_command_as_it_is():
    assert on_hosts(_APPLY, ()) == _APPLY


def test_local_connection_hosts_match_the_inventory():
    """The set is a literal, because monitor-bridge's copy of this module reads no inventory."""
    local = {h.name for h in inventory_hosts(HOSTS_INI) if h.connection == "local"}
    assert local == LOCAL_CONNECTION_HOSTS
    assert "daniel-server" in local
