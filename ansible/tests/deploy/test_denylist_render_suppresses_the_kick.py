"""The deployer's denylist re-render and the handler it must suppress stay wired together.

`deploy_phases.reconcile_denylist` runs `initial_setup.yml --tags gitops_deploy` from INSIDE
`gitops-deploy.service` to re-derive `K8S_AUTODEPLOY_DENYLIST`. Rendering
config.env notifies the role's "Run gitops-deploy once" handler, whose `systemctl start`
blocks until that unit's activation finishes — the activation the render is itself running
under. So the render passes `gitops_deploy_kick_after_change=false` and the handler reads that
variable; a rename on either side self-deadlocks the heal until its timeout, with nothing in
the play output naming the variable that stopped matching.

Three files carry one name, so this asserts the name they carry is the same one:

- `deploy_phases.RENDER_CONFIG_ARGV` passes it as `-e <var>=false`;
- `handlers/main.yml` gates the kick on `when: <var> | bool`;
- `defaults/main.yml` defines it true, so every ordinary apply still kicks.

Each file is read parsed: the argv through `ast`, the handler and the default as YAML (#3663).
The text reading split the handlers file on `- name: `, which a reordered key or a quoted name
breaks, and matched the default with a line regex.

Run: uv run pytest ansible/tests/deploy/test_denylist_render_suppresses_the_kick.py
"""

import ast
import itertools
import re

import pytest
from lib import yaml_fast
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

_FALSE_EXTRA_VAR = re.compile(r"([a-z_]+)=false")
_BOOL_GATE = re.compile(r"([a-z_]+)\s*\|\s*bool")


def _render_extra_var(source: str) -> str | None:
    """The variable `RENDER_CONFIG_ARGV` sets to false, or None when it sets none."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and [
            getattr(t, "id", None) for t in node.targets
        ] == ["RENDER_CONFIG_ARGV"]:
            argv = ast.literal_eval(node.value)
            for flag, value in itertools.pairwise(argv):
                match = _FALSE_EXTRA_VAR.fullmatch(value)
                if flag == "-e" and match:
                    return match.group(1)
            return None
    return None


def _handler_gate(handlers: str) -> str | None:
    """The variable the kick handler is gated on, or None when it is ungated."""
    for handler in yaml_fast.safe_load(handlers) or []:
        if handler.get("name") == _KICK_HANDLER:
            match = _BOOL_GATE.fullmatch(str(handler.get("when", "")).strip())
            return match.group(1) if match else None
    raise AssertionError(f"no {_KICK_HANDLER!r} handler in {_HANDLERS}")


def test_the_render_suppresses_the_kick_by_the_name_the_handler_reads():
    assert _render_extra_var(_PHASES.read_text()) == _VAR
    assert _handler_gate(_HANDLERS.read_text()) == _VAR


def test_an_ungated_kick_handler_is_flagged():
    """The rejecting half: the handler without the gate.

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


def test_a_gate_written_after_the_module_is_still_read():
    """The accepting case the `- name: ` split could not read: key order carries no meaning."""
    assert (
        _handler_gate(
            "---\n- ansible.builtin.systemd:\n    name: gitops-deploy.service\n"
            f"  name: '{_KICK_HANDLER}'\n  when: {_VAR} | bool\n"
        )
        == _VAR
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
    assert yaml_fast.safe_load(_DEFAULTS.read_text()).get(_VAR) is True


def test_the_kick_handler_exists_to_be_gated():
    """Non-vacuity: a renamed handler or a moved assignment must fail rather than read None."""
    with pytest.raises(AssertionError):
        _handler_gate("---\n- name: Reload systemd\n  ansible.builtin.systemd:\n")
    assert "RENDER_CONFIG_ARGV" in {
        t.id
        for node in ast.parse(_PHASES.read_text()).body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
