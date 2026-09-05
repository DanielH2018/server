"""The two ends of the CI gate's binding: `default_tools` and the unbound default.

Every other field of `DeployTools` defaults to a real implementation, so a `DeployTools()`
built without a `Config` still works. `fetch_ci_verdict` is the exception — it needs three
config values, and a default that guessed at them would guess "CI is green". These two tests
pin both halves: what `default_tools` binds, and what an unbound gate answers.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deploy_toolbox.py
"""

import functools

import deploy_toolbox
from deploy_config import Config

CONTEXTS = frozenset({"lint", "test"})


def test_default_tools_binds_the_ci_gate_to_the_config_it_was_given():
    """The one field `default_tools` exists for, checked at its keywords rather than by call.

    `require_ci` is passed True with a NON-EMPTY context set on purpose: `load_config` disarms
    the gate on an empty one, so a config that left it empty would pin False here and the
    assertion would pass whatever `default_tools` did.
    """
    cfg = Config(require_ci=True, ci_repo="DanielH2018/server", ci_contexts=CONTEXTS)
    gate = deploy_toolbox.default_tools(cfg).fetch_ci_verdict
    assert isinstance(gate, functools.partial)
    assert gate.func is deploy_toolbox.fetch_ci_verdict
    assert gate.keywords == {
        "require_ci": True,
        "repo": "DanielH2018/server",
        "contexts": CONTEXTS,
    }


def test_an_unbound_deploy_tools_defers_rather_than_reporting_a_pass():
    """Fail closed. A caller that forgot `default_tools` must not read as "CI is green"."""
    assert deploy_toolbox.DeployTools().fetch_ci_verdict("abc12345") == "pending"
