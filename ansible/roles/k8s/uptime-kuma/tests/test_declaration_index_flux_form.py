"""The Flux form of uptime-kuma's "Check the declaration index is populated" assert (#5712).

`tasks/main.yml` parses the rendered `static-monitors` Secret back into an index of AutoKuma
declarations and refuses a deploy whose index is thin. Under AutoKuma's `ON_DELETE=delete`, an
empty declaration set applied cleanly would delete every monitor. Under Flux no task runs, so
this module evaluates the task's own `that:` over the Secret the renderer emits for the tag,
the bytes Flux applies. CI runs it on every PR, so a template change that empties the index is
refused before it merges, never after Flux applies it.

The push-token assert has no form here: the renderer emits only placeholder tokens. Its
successor is the `_push_token` refusal in `scripts/secrets_mgmt/cluster_vars.py`'s
`successor_refusals`, the one step that reads the real values.

Run: uv run pytest ansible/roles/k8s/uptime-kuma/tests/test_declaration_index_flux_form.py
"""

import json

import jinja2
import pytest

from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs

TAG = "uptime-kuma"
TASKS = K8S_ROLES / TAG / "tasks" / "main.yml"
TASK = "Check the declaration index is populated"


def index_assert() -> str:
    """The `that:` of the index assert, read from tasks/main.yml rather than restated.

    uptime-kuma's flip PR deletes that file, and moves this expression into this module as a
    literal in the same PR.
    """
    task = next(
        t for t in yaml_fast.safe_load(TASKS.read_text()) if t.get("name") == TASK
    )
    return task["ansible.builtin.assert"]["that"]


def index_passes(declarations: dict) -> bool:
    env = make_ansible_env(undefined_cls=jinja2.StrictUndefined)
    passed = env.compile_expression(index_assert())(kuma_declarations=declarations)
    assert isinstance(passed, bool), f"the assert evaluated to {passed!r}, not a bool"
    return passed


def declaration_index(string_data: dict[str, str]) -> dict[str, dict]:
    """The index the task's set_fact builds: each key minus `.json`, its JSON parsed."""
    return {
        name.removesuffix(".json"): json.loads(text)
        for name, text in string_data.items()
    }


@pytest.fixture(scope="module")
def static_monitors(tmp_path_factory) -> dict[str, str]:
    """The `stringData` of the static-monitors Secret the renderer emits for the tag."""
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    rendered = cluster.render_tag(TAG, load_stubs(path))
    secret = yaml_fast.safe_load(rendered.files["static-monitors.yaml"])
    assert secret["kind"] == "Secret"
    return secret["stringData"]


def test_the_rendered_index_passes_the_assert(static_monitors):
    index = declaration_index(static_monitors)
    # Named members: a notification, a tag and a token-gated push tile. A render that stopped
    # opening the gate, or stopped emitting entities other than monitors, loses one of them.
    assert {"discord", "tag-severity", "status-page-sync"} <= index.keys()
    assert index_passes(index)


def test_an_empty_index_is_refused():
    assert not index_passes(declaration_index({}))


def test_a_thin_index_is_refused(static_monitors):
    """A render that kept a handful of entities is refused as an empty one is."""
    thin = dict(sorted(static_monitors.items())[:5])
    assert not index_passes(declaration_index(thin))
