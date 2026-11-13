"""The host-component render guards produce the bytes the host package ships (#8182).

Run: uv run pytest hosts/tests/test_setup_render_matches_package.py
"""

from _setup_render import (
    COMPONENTS,
    render_component_text,
    render_setup_texts,
    role_context,
)
from renderer.renderer_lib.environment import render_file
from renderer.renderer_lib.host_sources import confined_env


def test_a_guard_render_equals_the_package_render(tmp_path):
    """An `{% include %}` loses its last newline in the package, so it must in the guard too."""
    component = tmp_path / "widget"
    (component / "templates").mkdir(parents=True)
    (component / "component.yml").write_text("{}\n")
    (component / "templates" / "item.j2").write_text("x\n")
    (component / "templates" / "widget.conf.j2").write_text(
        "a={% include 'templates/item.j2' %};b=1\n"
    )
    shipped = render_file(confined_env(component), "templates/widget.conf.j2", {})
    assert render_setup_texts(components=tmp_path) == (
        ("widget", "widget.conf.j2", shipped),
    )


def test_a_component_text_equals_the_package_render():
    """registry-gc's gc-job template includes its script, whose last newline the package drops."""
    component, name = "registry-gc", "dockerhub-mirror-gc-job.yaml.j2"
    directory = COMPONENTS / component
    shipped = render_file(
        confined_env(directory), f"templates/{name}", role_context(directory)
    )
    assert render_component_text(component, name) == shipped
