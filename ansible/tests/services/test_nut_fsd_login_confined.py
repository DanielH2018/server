"""Only the UPS shutdown chain renders upsd's FSD-capable login.

`nut_monitor_password` is the `upsmon` primary login. It can raise FSD, which powers off every
armed node. The in-cluster nut role's own upsmon and the hosts' secondaries (`nut_host`) need
it. Anything that only reads the UPS takes the read-only `nut_ha_password` login instead.

The census renders every role on all three planes with the password set to a sentinel and
counts the renders that carry it, so a template aliasing the secret through a second variable
is caught where a scan of template source would miss it (#3183).

Run: uv run pytest ansible/tests/services/test_nut_fsd_login_confined.py
"""

from _compose_render import host_vars as pi_host_vars
from _compose_render import render_texts as compose_render_texts
from _helpers import ANSIBLE
from _k8s_render import render_texts as k8s_render_texts
from _setup_render import render_setup_texts

ROLES = ANSIBLE / "roles"
SHUTDOWN_CHAIN = frozenset({"k8s/nut", "setup/nut_host"})
# Distinctive enough that no template renders it by accident.
SENTINEL = "nut-monitor-password-sentinel-3183"


def roles_outside_the_chain(holders: set[str]) -> set[str]:
    """The roles in `holders` that are not part of the shutdown chain."""
    return holders - SHUTDOWN_CHAIN


def test_a_dashboard_holding_the_login_is_flagged():
    assert roles_outside_the_chain({"k8s/nut", "k8s/peanut"}) == {"k8s/peanut"}


def test_the_shutdown_chain_alone_is_clean():
    assert roles_outside_the_chain(set(SHUTDOWN_CHAIN)) == set()


def _roles_naming_the_login() -> set[str]:
    """`<plane>/<role>` for every role with a template whose SOURCE names the primary login."""
    return {
        "/".join(path.relative_to(ROLES).parts[:2])
        for path in ROLES.glob("*/*/templates/**/*.j2")
        if "nut_monitor_password" in path.read_text()
    }


def _roles_rendering(overrides: dict, marker: str) -> set[str]:
    """`<plane>/<role>` for every role, on all three planes, whose render carries `marker`."""
    planes = (
        ("k8s", k8s_render_texts(overrides)),
        ("setup", render_setup_texts(overrides)),
        ("containers", compose_render_texts({**pi_host_vars(), **overrides})),
    )
    return {
        f"{plane}/{role}"
        for plane, texts in planes
        for role, _, text in texts
        if marker in text
    }


def _roles_rendering_the_login() -> set[str]:
    """Every role whose render carries the primary login's VALUE, not just its name.

    The password is set to a sentinel, so a template that reaches it through an alias is
    counted, where a source scan for the name sees nothing.
    """
    return _roles_rendering({"nut_monitor_password": SENTINEL}, SENTINEL)


def test_an_alias_of_the_login_reaches_a_setup_render(tmp_path):
    role = tmp_path / "aliasing_role"
    (role / "templates").mkdir(parents=True)
    (role / "defaults").mkdir()
    (role / "templates" / "upsmon.conf.j2").write_text(
        "MONITOR ups {{ upsmon_secret }}\n"
    )
    (role / "defaults" / "main.yml").write_text(
        'upsmon_secret: "{{ nut_monitor_password }}"\n'
    )
    texts = render_setup_texts({"nut_monitor_password": SENTINEL}, setup=tmp_path)
    assert texts == (("aliasing_role", "upsmon.conf.j2", f"MONITOR ups {SENTINEL}\n"),)


def test_a_role_naming_nothing_renders_no_sentinel(tmp_path):
    role = tmp_path / "reader_role"
    (role / "templates").mkdir(parents=True)
    (role / "templates" / "upsmon.conf.j2").write_text(
        "MONITOR ups {{ nut_ha_password }}\n"
    )
    texts = render_setup_texts({"nut_monitor_password": SENTINEL}, setup=tmp_path)
    assert SENTINEL not in texts[0][2]


def test_only_the_shutdown_chain_renders_the_primary_login():
    holders = _roles_rendering_the_login()
    assert SHUTDOWN_CHAIN <= holders, (
        f"the census missed the chain itself: {sorted(holders)}"
    )
    outside = roles_outside_the_chain(holders)
    assert not outside, (
        f"these roles can raise FSD and only need reads: {sorted(outside)}"
    )


def test_the_render_census_covers_every_role_naming_the_login():
    """The render census replaced a source scan; it must not see fewer roles than that did.

    A role whose template names the login but that the render accessors never reach — a new
    plane, a template class they skip — would drop out of the census silently.
    """
    missed = _roles_naming_the_login() - _roles_rendering_the_login()
    assert not missed, (
        f"these roles name the login in source but no render reached it: {sorted(missed)}"
    )
