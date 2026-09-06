"""The deployer's denylist re-render and the handler it must suppress stay wired together.

`deploy_phases.reconcile_denylist` runs `initial_setup.yml --tags gitops_deploy` from INSIDE
`gitops-deploy.service` to re-derive `K8S_AUTODEPLOY_DENYLIST` (issue #1294). Rendering
config.env notifies the role's "Run gitops-deploy once" handler, whose `systemctl start`
blocks until that unit's activation finishes — the activation the render is itself running
under. So the render passes `gitops_deploy_kick_after_change=false` and the handler reads that
variable; a rename on either side self-deadlocks the heal until its timeout, with nothing in
the play output naming the variable that stopped matching.

Three files carry one name, so this asserts the name they carry is the same one:

- `deploy_phases.RENDER_CONFIG_ARGV` passes it as `-e <var>=false`;
- `handlers/main.yml` gates the kick on `when: <var> | bool`;
- `defaults/main.yml` defines it true, so every ordinary apply still kicks.

Run: uv run pytest ansible/tests/deploy/test_denylist_render_suppresses_the_kick.py
"""

import re

import pytest
from _helpers import REPO as _REPO


_ROLE = _REPO / "ansible/roles/setup/gitops_deploy"
_PHASES = _ROLE / "files/deploy_phases.py"
_HANDLERS = _ROLE / "handlers/main.yml"
_DEFAULTS = _ROLE / "defaults/main.yml"

_KICK_HANDLER = "Run gitops-deploy once"
# The name every one of the three files must carry. A literal, not a derivation: a check that
# read the name out of one file and looked for it in the others would still pass if all three
# were renamed to something the deployer never passes.
_VAR = "gitops_deploy_kick_after_change"

_EXTRA_VAR = re.compile(r'"-e",\s*\n?\s*"([a-z_]+)=false"')


def _render_extra_var(source: str) -> str | None:
    """The variable `RENDER_CONFIG_ARGV` sets to false, or None when it sets none."""
    body = source.split("RENDER_CONFIG_ARGV = [", 1)
    if len(body) == 1:
        return None
    match = _EXTRA_VAR.search(body[1].split("]", 1)[0])
    return match.group(1) if match else None


def _handler_gate(handlers: str) -> str | None:
    """The variable the kick handler is gated on, or None when it is ungated."""
    for block in handlers.split("- name: "):
        if block.startswith(_KICK_HANDLER):
            match = re.search(r"when:\s*([a-z_]+)\s*\|\s*bool", block)
            return match.group(1) if match else None
    raise AssertionError(f"no {_KICK_HANDLER!r} handler in {_HANDLERS}")


def test_the_render_suppresses_the_kick_by_the_name_the_handler_reads():
    assert _render_extra_var(_PHASES.read_text()) == _VAR
    assert _handler_gate(_HANDLERS.read_text()) == _VAR


def test_an_ungated_kick_handler_is_flagged():
    """The rejecting half: the handler as it read before the reconcile existed.

    Without it this file would pass just as happily against a handler that always kicks, which
    is the shape that deadlocks.
    """
    assert (
        _handler_gate(
            f"---\n- name: {_KICK_HANDLER}\n  ansible.builtin.systemd:\n"
            "    name: gitops-deploy.service\n    state: started\n"
        )
        is None
    )


def test_a_render_that_passes_no_extra_var_is_flagged():
    """The other rejecting half, for the deploy_phases side of the same pair."""
    assert (
        _render_extra_var(
            'RENDER_CONFIG_ARGV = [\n    "uv",\n    "run",\n    "--frozen",\n]\n'
        )
        is None
    )


def test_the_default_arms_the_kick_for_an_ordinary_apply():
    """A first install must still activate without a manual `systemctl start`."""
    assert re.search(rf"^{_VAR}:\s*true\s*$", _DEFAULTS.read_text(), re.MULTILINE)


def test_the_kick_handler_exists_to_be_gated():
    """Non-vacuity: both readers above are pattern-driven, so a renamed handler or a moved
    function would otherwise make this whole file assert nothing."""
    assert _KICK_HANDLER in _HANDLERS.read_text()
    assert "RENDER_CONFIG_ARGV" in _PHASES.read_text()
    with pytest.raises(AssertionError):
        _handler_gate("---\n- name: Reload systemd\n  ansible.builtin.systemd:\n")
