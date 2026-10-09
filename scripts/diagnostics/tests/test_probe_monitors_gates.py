"""`probe.py kuma-drift`: a tile's gate is judged on every variable it reads (#3414).

A gate may be a secret, an inventory flag, or an `and` of both. These tests were split from
test_probe_monitors.py, which covers the drift report itself.
"""

from diagnostics.probe_lib import monitors

with open(monitors.STATIC_MONITORS_PATH) as _f:
    REAL_STATIC_MONITORS_TEXT = _f.read()
RENOVATE_TILE = "Renovate Agent — Alive"
RENOVATE_GATE = (
    "renovate_agent_kuma_push_token | default('')",
    "renovate_agent_enabled | default(false)",
)


def test_parse_declared_monitors_keeps_every_conjunct_of_an_and_gate():
    """The Renovate agent's tile is gated on its token AND its arming flag (#3411). Keeping
    only the leading identifier judged the tile on the token alone (#3414).
    """
    declared = monitors.parse_declared_monitors(REAL_STATIC_MONITORS_TEXT)
    assert declared[RENOVATE_TILE]["gate"] == RENOVATE_GATE


def test_an_and_gate_with_a_false_flag_is_gated_off_is_clean():
    """The token is set, the flag is false: the template withdraws the tile, so its absence is
    not drift — and a false conjunct decides even when another could not be read.
    """
    declared = {
        RENOVATE_TILE: {
            "type": "push",
            "interval": 60,
            "gated": True,
            "gate": RENOVATE_GATE,
        }
    }
    for token_state in (True, None):
        text, code = monitors.format_kuma_drift(
            declared,
            set(),
            86400 * 3,
            gate_states={RENOVATE_GATE[0]: token_state, RENOVATE_GATE[1]: False},
            created=set(declared),
        )
        assert code == 0, text
        assert (
            f"gated off (a secret genuinely unset or a flag false), skipped: {RENOVATE_TILE}"
            in text
        )


def test_an_and_gate_with_every_conjunct_true_is_flagged():
    declared = {
        RENOVATE_TILE: {
            "type": "push",
            "interval": 60,
            "gated": True,
            "gate": RENOVATE_GATE,
        }
    }
    text, code = monitors.format_kuma_drift(
        declared,
        set(),
        86400 * 3,
        gate_states={RENOVATE_GATE[0]: True, RENOVATE_GATE[1]: True},
        created=set(declared),
    )
    assert code == 1
    assert f"{RENOVATE_TILE}: declared, not live" in text


def test_a_non_secret_gate_is_read_from_the_inventory_without_sops(monkeypatch):
    """`renovate_agent_enabled` is a daniel-box host_var, and `traefik_k8s_manage_crowdsec`
    is set nowhere, so its `| default(true)` decides. Asking SOPS for either got "undeclared",
    which read False. A secret reader that raises proves neither conjunct reached a decrypt.
    """

    def no_decrypt(var):
        raise AssertionError(f"decrypted {var}")

    monkeypatch.setitem(monitors.monitor_vars(), "renovate_agent_enabled", False)
    assert (
        monitors.gate_conjunct_state(RENOVATE_GATE[1], read_secret=no_decrypt) is False
    )
    monkeypatch.setitem(monitors.monitor_vars(), "renovate_agent_enabled", True)
    assert (
        monitors.gate_conjunct_state(RENOVATE_GATE[1], read_secret=no_decrypt) is True
    )
    crowdsec = "traefik_k8s_manage_crowdsec | default(true)"
    assert monitors.gate_conjunct_state(crowdsec, read_secret=no_decrypt) is True


def test_a_disarmed_renovate_agent_is_listed_as_gated_not_missing(monkeypatch):
    """The issue's verify-by, through the real template and the real resolver."""
    monkeypatch.setitem(monitors.monitor_vars(), "renovate_agent_enabled", False)
    declared = monitors.parse_declared_monitors(REAL_STATIC_MONITORS_TEXT)
    live = set(declared) - {RENOVATE_TILE}
    gate_states = monitors.resolve_gate_states(declared, live, no_secrets=True)
    text, code = monitors.format_kuma_drift(
        declared, live, 86400 * 3, gate_states, created=set(declared)
    )
    assert code == 0, text
    assert f"skipped: {RENOVATE_TILE}" in text
    assert f"{RENOVATE_TILE}: declared, not live" not in text


def test_an_or_gate_is_not_resolved_is_flagged():
    """An `or` or `not` breaks "every conjunct must hold", so it reads as unreadable."""
    assert monitors.gate_conjunct_state("a_token | default('') or other_flag") is None
