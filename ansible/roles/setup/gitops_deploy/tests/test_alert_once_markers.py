"""alert_once()'s first argument is a `DeployerState` marker name, not an arbitrary string.

A typo raises `KeyError` inside `state.write()`, several calls deep, reachable only from
`entrypoint()`'s generic crash handler — nothing at the call site itself checks the name against
`DeployerState.MARKERS`. This walks the AST of every module that calls `alert_once` for the
literal passed as its marker argument and checks each one against `MARKERS`, so a typo fails
here instead of on the next tick that happens to reach that branch.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_alert_once_markers.py
"""

import ast
import pathlib

import deploy_io

_FILES = pathlib.Path(__file__).resolve().parents[1] / "files"
# Every module that calls `alert_once`, named rather than globbed: a glob over files/ would
# keep passing if the call sites all moved to a module the glob stopped matching.
_SOURCES = (
    "deploy_alerts.py",
    "deploy_handlers.py",
    "deploy_phases.py",
)

# The call sites known to exist today, named rather than just counted — so a call site
# disappearing (a refactor that renames or drops one) fails this test too, not only an unknown
# marker appearing.
_KNOWN_MARKERS = frozenset(
    {
        "secrets_alerted",
        "tasks_alerted",
        "meta_alerted",
        "k8s_alerted",
        "staging_alerted",
        "stale_denylist_alerted",
        "ci_alerted",
        "broad_alerted",
    }
)


def _alert_once_markers(source: str) -> list[str]:
    """Every string literal passed as `alert_once`'s marker argument, in source order.

    The marker is the FOURTH argument — `alert_once(tools, state, config, marker, ...)` — and
    the call is `deploy_alerts.alert_once(...)` everywhere except inside `deploy_alerts`
    itself, where it is a bare name. Both forms count.
    """
    markers = []
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
            markers.append(node.args[3].value)
    return markers


def test_every_alert_once_call_site_names_a_real_deployerstate_marker():
    markers = [
        marker
        for source in _SOURCES
        for marker in _alert_once_markers((_FILES / source).read_text())
    ]
    assert set(markers) >= _KNOWN_MARKERS, (
        f"expected at least {sorted(_KNOWN_MARKERS)}, found {sorted(set(markers))} — a call "
        "site disappeared, or the AST walk stopped matching alert_once's call shape."
    )
    unknown = [m for m in markers if m not in deploy_io.DeployerState.MARKERS]
    assert not unknown, (
        f"alert_once() is called with marker(s) not in DeployerState.MARKERS: {unknown} — "
        "this raises KeyError inside STATE.write(), reachable only from entrypoint()'s crash "
        "path."
    )


def test_an_unknown_marker_is_caught():
    # Red proof for the pair above: the same extractor, driven against a source whose literal
    # is a typo that does not name a real marker.
    bad_source = 'deploy_alerts.alert_once(tools, state, config, "k9s_alerted", "k8s", origin, body)'
    markers = _alert_once_markers(bad_source)
    assert markers == ["k9s_alerted"]
    assert "k9s_alerted" not in deploy_io.DeployerState.MARKERS
