"""The renderer ends a file with the newlines ansible-core's `template` module writes.

Each case renders one template through ansible-core's own `Templar`, called as the module's
action plugin calls it, and through `environment.render_file`, and holds the bytes equal.

Run: uv run pytest scripts/renderer/tests/test_renderer_trailing_newlines.py
"""

import functools
import warnings

import pytest
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import filter_loader, init_plugin_loader
from ansible.template import Templar, trust_as_template

from lib.render_guard import SHARED_TPL
from lib.repo_paths import FILTER_PLUGINS
from renderer.renderer_lib import environment

LABEL_WRAPPER = "role-labelled.yaml.j2"
SERVICE = "apiVersion: v1\nkind: Service\nmetadata:\n  name: {{ name }}\n"


@functools.cache
def _load_plugins() -> None:
    """Load the builtin plugins and the repo's filters, which the label wrapper calls."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", "AnsibleCollectionFinder has already been")
        init_plugin_loader()
    filter_loader.add_directory(str(FILTER_PLUGINS))


def _template_module(source: str, variables: dict, basedir) -> str:
    """`source` rendered as `ansible.builtin.template` renders it, before it writes the file."""
    _load_plugins()
    loader = DataLoader()
    loader.set_basedir(str(basedir))
    templar = Templar(loader=loader, variables=variables)
    return templar.template(trust_as_template(source), escape_backslashes=False)


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
    source = (SHARED_TPL / LABEL_WRAPPER).read_text()

    expected = _template_module(source, variables, SHARED_TPL)
    env = environment.renderer_env(SHARED_TPL, variables)
    assert environment.render_file(env, LABEL_WRAPPER, variables) == expected
    assert expected.endswith("homelab/role: littlelink\n")


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(SERVICE, id="one-newline"),
        pytest.param("name: {{ name }}\n\n\n", id="three-newlines"),
        pytest.param("name: {{ name }}", id="no-newline"),
        pytest.param("{{ block }}\n", id="value-ends-in-a-newline"),
        pytest.param("{% if name %}\nname: {{ name }}\n{% endif %}\n", id="block-last"),
    ],
)
def test_a_template_ends_as_the_template_module_writes_it(tmp_path, source):
    (tmp_path / "file.yaml.j2").write_text(source)
    variables = {"name": "littlelink", "block": "a: 1\nb: 2\n"}

    expected = _template_module(source, variables, tmp_path)
    env = environment.renderer_env(tmp_path, variables)
    assert environment.render_file(env, "file.yaml.j2", variables) == expected
