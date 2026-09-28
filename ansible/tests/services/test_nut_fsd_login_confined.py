"""Only the UPS shutdown chain renders upsd's FSD-capable login.

`nut_monitor_password` is the `upsmon` primary login. It can raise FSD, which powers off every
armed node. The in-cluster nut role's own upsmon and the hosts' secondaries (`nut_host`) need
it. Anything that only reads the UPS takes the read-only `nut_ha_password` login instead.
PeaNUT, a dashboard, rendered the primary login into its Secret until 2026-09-28.

Run: uv run pytest ansible/tests/services/test_nut_fsd_login_confined.py
"""

from _helpers import ANSIBLE

ROLES = ANSIBLE / "roles"
SHUTDOWN_CHAIN = frozenset({"k8s/nut", "setup/nut_host"})


def roles_outside_the_chain(holders: set[str]) -> set[str]:
    """The roles in `holders` that are not part of the shutdown chain."""
    return holders - SHUTDOWN_CHAIN


def test_a_dashboard_holding_the_login_is_flagged():
    assert roles_outside_the_chain({"k8s/nut", "k8s/peanut"}) == {"k8s/peanut"}


def test_the_shutdown_chain_alone_is_clean():
    assert roles_outside_the_chain(set(SHUTDOWN_CHAIN)) == set()


def _roles_rendering_the_login() -> set[str]:
    """`<plane>/<role>` for every role with a template that names the primary login."""
    return {
        "/".join(path.relative_to(ROLES).parts[:2])
        for path in ROLES.glob("*/*/templates/**/*.j2")
        if "nut_monitor_password" in path.read_text()
    }


def test_only_the_shutdown_chain_renders_the_primary_login():
    holders = _roles_rendering_the_login()
    assert SHUTDOWN_CHAIN <= holders, (
        f"the census missed the chain itself: {sorted(holders)}"
    )
    outside = roles_outside_the_chain(holders)
    assert not outside, (
        f"these roles can raise FSD and only need reads: {sorted(outside)}"
    )
