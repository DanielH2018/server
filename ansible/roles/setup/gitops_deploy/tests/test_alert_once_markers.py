"""alert_once()'s channel argument is a `gitops_markers.ALERT_SLOTS` member, not any string.

A typo raises `KeyError` inside `state.record_alerted()`, several calls deep, reachable only
from `entrypoint()`'s generic crash handler — nothing at the call site itself checks the name
against the slots. This walks the AST of every module that calls `alert_once` for the literal
passed as its channel argument and checks each one against `ALERT_SLOTS`, so a typo fails here
instead of on the next tick that happens to reach that branch.

The channel is the slot, so there is one literal to check and no separate marker name beside
it that could disagree with it.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_alert_once_markers.py
"""

import ast
import pathlib

import gitops_markers

_FILES = pathlib.Path(__file__).resolve().parents[1] / "files"
# Every module that calls `alert_once`, named rather than globbed: a glob over files/ would
# keep passing if the call sites all moved to a module the glob stopped matching.
_SOURCES = (
    "deploy_alerts.py",
    "deploy_defer.py",
    "deploy_handlers.py",
    "deploy_phases.py",
)

# The call sites known to exist today, named rather than just counted — so a call site
# disappearing (a refactor that renames or drops one) fails this test too, not only an unknown
# channel appearing. This is every member of `ALERT_SLOTS`, asserted below rather than aliased
# to it: a slot nothing calls is a dedupe file with no writer.
_KNOWN_CHANNELS = frozenset(
    {
        "secrets",
        "tasks",
        "k8s",
        "stale_denylist",
        "ci",
        "broad",
    }
)


def _alert_once_channels(source: str) -> list[str]:
    """Every string literal passed as `alert_once`'s channel argument, in source order.

    The channel is the FOURTH argument — `alert_once(tools, state, config, channel, ...)` —
    and the call is `deploy_alerts.alert_once(...)` everywhere except inside `deploy_alerts`
    itself, where it is a bare name. Both forms count.
    """
    channels = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        named = (isinstance(node.func, ast.Name) and node.func.id == "alert_once") or (
            isinstance(node.func, ast.Attribute) and node.func.attr == "alert_once"
        )
        if (
            named
            and len(node.args) >= 4
            and isinstance(node.args[3], ast.Constant)
            and isinstance(node.args[3].value, str)
        ):
            channels.append(node.args[3].value)
    return channels


def test_the_named_census_is_every_alert_slot():
    assert _KNOWN_CHANNELS == gitops_markers.ALERT_SLOTS


def test_every_alert_once_call_site_names_a_real_alert_slot():
    channels = [
        channel
        for source in _SOURCES
        for channel in _alert_once_channels((_FILES / source).read_text())
    ]
    assert set(channels) >= _KNOWN_CHANNELS, (
        f"expected at least {sorted(_KNOWN_CHANNELS)}, found {sorted(set(channels))} — a call "
        "site disappeared, or the AST walk stopped matching alert_once's call shape."
    )
    unknown = [c for c in channels if c not in gitops_markers.ALERT_SLOTS]
    assert not unknown, (
        f"alert_once() is called with channel(s) not in ALERT_SLOTS: {unknown} — this raises "
        "KeyError inside STATE.record_alerted(), reachable only from entrypoint()'s crash "
        "path."
    )


def test_an_unknown_channel_is_caught():
    # Red proof for the pair above: the same extractor, driven against a source whose literal
    # is a typo that does not name a real slot.
    bad_source = 'deploy_alerts.alert_once(tools, state, config, "k9s", origin, body)'
    channels = _alert_once_channels(bad_source)
    assert channels == ["k9s"]
    assert "k9s" not in gitops_markers.ALERT_SLOTS
