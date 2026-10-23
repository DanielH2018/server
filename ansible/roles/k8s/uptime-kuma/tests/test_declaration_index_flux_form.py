"""The Flux form of uptime-kuma's "Check the declaration index is populated" assert (#5712).

The Ansible deploy parsed the rendered `static-monitors` Secret back into an index of AutoKuma
declarations and refused a deploy whose index was thin. Under AutoKuma's `ON_DELETE=delete`, an
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
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs

TAG = "uptime-kuma"
# The `that:` of the assert the flip deleted from tasks/main.yml. M6's A0 swapped its numeric
# floor (`length > 50`) for the entities that must outlive Phase A and M4 until C-1: Phase A
# shrinks the index to five, so a count would refuse the first Kuma-side PR.
REQUIRED = ["status-page-sync", "discord", "tag-severity", "tag-runbook"]
INDEX_ASSERT = (
    f"{REQUIRED!r} | select('in', kuma_declarations) | list | length == {len(REQUIRED)}"
)
SURVIVORS = [*REQUIRED, "email"]


def index_passes(declarations: dict) -> bool:
    env = make_ansible_env(undefined_cls=jinja2.StrictUndefined)
    passed = env.compile_expression(INDEX_ASSERT)(kuma_declarations=declarations)
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
    assert set(REQUIRED) <= index.keys()
    assert index_passes(index)


def test_an_empty_index_is_refused():
    assert not index_passes(declaration_index({}))


def test_the_five_survivors_alone_are_accepted():
    """The index Phase A and M4 leave must pass, which a count above 50 refused."""
    assert index_passes(declaration_index({f"{n}.json": "{}" for n in SURVIVORS}))


@pytest.mark.parametrize("missing", REQUIRED)
def test_an_index_missing_a_required_member_is_refused(static_monitors, missing):
    """Dropping any one named entity is refused, however many others remain."""
    index = declaration_index(static_monitors)
    del index[missing]
    assert len(index) > 50
    assert not index_passes(index)
