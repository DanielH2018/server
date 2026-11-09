"""The renderer ends a file with the newlines ansible-core's `template` module writes.

Each case renders one template through `environment.render_file` and holds the bytes equal to
what ansible-core 2.22's `Templar` returned for it, called as the module's action plugin calls
it, taken before H7 removed ansible-core.

Run: uv run pytest scripts/renderer/tests/test_renderer_trailing_newlines.py
"""

import pytest

from lib.render_guard import SHARED_TPL
from renderer.renderer_lib import environment

LABEL_WRAPPER = "role-labelled.yaml.j2"
SERVICE = "apiVersion: v1\nkind: Service\nmetadata:\n  name: {{ name }}\n"


def test_the_label_wrapper_ends_as_the_template_module_writes_it(tmp_path):
    # Every file of a role that arms `manifests_prune` renders through this wrapper, and the
    # operator's shadow pair at 446a367a differed on every one by a second newline (#4848).
    inner = tmp_path / "service.yaml.j2"
    inner.write_text(SERVICE)
    variables = {
        "manifests_label_src": str(inner),
        "manifests_service": "littlelink",
        "name": "littlelink",
    }
    env = environment.renderer_env(SHARED_TPL, variables)
    assert environment.render_file(env, LABEL_WRAPPER, variables) == (
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: littlelink\n"
        "  labels:\n    homelab/role: littlelink\n"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            SERVICE,
            "apiVersion: v1\nkind: Service\nmetadata:\n  name: littlelink\n",
            id="one-newline",
        ),
        pytest.param(
            "name: {{ name }}\n\n\n", "name: littlelink\n\n\n", id="three-newlines"
        ),
        pytest.param("name: {{ name }}", "name: littlelink", id="no-newline"),
        pytest.param("{{ block }}\n", "a: 1\nb: 2\n", id="value-ends-in-a-newline"),
        pytest.param(
            "{% if name %}\nname: {{ name }}\n{% endif %}\n",
            "name: littlelink\n",
            id="block-last",
        ),
    ],
)
def test_a_template_ends_as_the_template_module_writes_it(tmp_path, source, expected):
    (tmp_path / "file.yaml.j2").write_text(source)
    variables = {"name": "littlelink", "block": "a: 1\nb: 2\n"}
    env = environment.renderer_env(tmp_path, variables)
    assert environment.render_file(env, "file.yaml.j2", variables) == expected
