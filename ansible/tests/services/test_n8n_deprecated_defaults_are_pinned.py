"""The n8n defaults that 2.41.6 warns will change are pinned, on both pods (#3255).

n8n logs five deprecation warnings at boot. Each names a default that a future version
changes, and an unset variable would take that change silently through a Renovate digest bump.

- **The n8n Deployment sets the three broker-side variables.** n8n's deprecation service warns
  only while the variable is unset, so setting it is both the pin and the silencer.
- **Each runner lists both timeout variables in `allowed-env`, and the runners Deployment sets
  both.** The launcher warns for any runner whose `allowed-env` lacks one. `env-overrides`
  does not silence it, whatever the warning text says. `allowed-env` passes only what the
  launcher's own environment holds, so an entry with no value behind it pins nothing.

Run: uv run pytest ansible/tests/services/test_n8n_deprecated_defaults_are_pinned.py
"""

import json

from _helpers import ROLES
from _k8s_render import rendered_docs

BROKER_VARS = frozenset(
    {
        "N8N_RUNNERS_TASK_TIMEOUT",
        "N8N_COMPRESSION_NODE_MAX_DECOMPRESSED_SIZE_BYTES",
        "N8N_COMPRESSION_NODE_MAX_ZIP_ENTRIES",
    }
)
RUNNER_VARS = frozenset(
    {"N8N_RUNNERS_TASK_TIMEOUT", "N8N_RUNNERS_AUTO_SHUTDOWN_TIMEOUT"}
)
RUNNERS_CONFIG = ROLES / "k8s/n8n/templates/config/n8n-task-runners.json.j2"


# --- the rule, as a predicate ---------------------------------------------------------


def unpinned_runner_vars(config: dict, launcher_env: set[str]) -> set[str]:
    """The runner timeout variables that are missing from a runner's `allowed-env` or unset."""
    missing = set(RUNNER_VARS - launcher_env)
    for runner in config["task-runners"]:
        missing |= RUNNER_VARS - set(runner.get("allowed-env", []))
    return missing


# --- red proofs -----------------------------------------------------------------------


def _config(*allowed_lists):
    return {"task-runners": [{"allowed-env": list(a)} for a in allowed_lists]}


def test_both_runners_passing_both_set_variables_is_clean():
    config = _config(RUNNER_VARS, RUNNER_VARS)
    assert unpinned_runner_vars(config, set(RUNNER_VARS)) == set()


def test_a_runner_with_only_env_overrides_is_flagged():
    config = {
        "task-runners": [
            {"allowed-env": list(RUNNER_VARS)},
            {"env-overrides": {v: "60" for v in RUNNER_VARS}},
        ]
    }
    assert unpinned_runner_vars(config, set(RUNNER_VARS)) == set(RUNNER_VARS)


def test_an_allowed_variable_the_launcher_does_not_hold_is_flagged():
    config = _config(RUNNER_VARS, RUNNER_VARS)
    assert unpinned_runner_vars(config, {"N8N_RUNNERS_TASK_TIMEOUT"}) == {
        "N8N_RUNNERS_AUTO_SHUTDOWN_TIMEOUT"
    }


# --- applied to the real templates -----------------------------------------------------


def _env_names(name: str) -> set[str]:
    for role, _tpl, doc in rendered_docs():
        if (
            role == "n8n"
            and doc.get("kind") == "Deployment"
            and doc["metadata"]["name"] == name
        ):
            container = doc["spec"]["template"]["spec"]["containers"][0]
            return {e["name"] for e in container.get("env", []) if "value" in e}
    raise AssertionError(f"n8n renders no Deployment named {name}")


def test_the_n8n_deployment_sets_the_broker_side_defaults():
    assert BROKER_VARS <= _env_names("n8n")


def test_every_runner_receives_both_timeouts_from_the_launcher():
    config = json.loads(RUNNERS_CONFIG.read_text())
    assert {r["runner-type"] for r in config["task-runners"]} == {
        "javascript",
        "python",
    }
    assert unpinned_runner_vars(config, _env_names("n8n-runners")) == set()
