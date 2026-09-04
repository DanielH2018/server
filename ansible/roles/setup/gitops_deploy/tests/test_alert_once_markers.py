"""alert_once()'s first argument is a `DeployerState` marker name, not an arbitrary string.

A typo raises `KeyError` inside `STATE.write()`, several calls deep, reachable only from
`entrypoint()`'s generic crash handler — nothing at the call site itself checks the name against
`DeployerState.MARKERS`. This walks `gitops_deploy.py`'s AST for every literal passed as
`alert_once`'s first positional argument and checks each one against `MARKERS`, so a typo fails
here instead of on the next tick that happens to reach that branch.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_alert_once_markers.py
"""

import ast
import pathlib

import deploy_io

_SRC = pathlib.Path(__file__).resolve().parents[1] / "files" / "gitops_deploy.py"

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
    """Every string literal passed as `alert_once`'s first argument, in source order."""
    markers = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "alert_once"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            markers.append(node.args[0].value)
    return markers


def test_every_alert_once_call_site_names_a_real_deployerstate_marker():
    markers = _alert_once_markers(_SRC.read_text())
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
    bad_source = 'alert_once("k9s_alerted", "k8s", origin, content)'
    markers = _alert_once_markers(bad_source)
    assert markers == ["k9s_alerted"]
    assert "k9s_alerted" not in deploy_io.DeployerState.MARKERS
