"""The context `renderer_lib.context` builds: the toposorted `services` and the `hosts` layer.

Run: uv run pytest scripts/renderer/tests/test_renderer_context.py
"""

import jinja2
import pytest
import yaml

from lib.ansible_jinja_env import template_env
from lib.estate import Inventory
from lib.render_guard import StrictChainableUndefined, host_files, host_layer, load_yaml
from lib.repo_paths import HOST_VARS, K8S_ROLES, K8S_SERVICES
from renderer import shadow_diff
from renderer.renderer_lib.context import sorted_services, template_context
from renderer.template_filters.toposort import build_k8s_dep_map, toposort_containers

ROLE = K8S_ROLES / "littlelink"


@pytest.fixture(scope="module")
def ctx():
    return template_context(ROLE, shadow_diff.stub_vars())


def _names(entries):
    return [e["name"] for e in entries]


def test_services_is_the_toposorted_service_list_and_no_host_view_remains(ctx):
    raw = load_yaml(K8S_SERVICES)["containers_list"]
    expected = _names(toposort_containers(raw, build_k8s_dep_map(raw, str(K8S_ROLES))))
    assert _names(ctx["services"]) == expected
    assert "containers_list" not in ctx
    assert "hostvars" not in ctx


def test_sorted_services_sorts_a_reordered_list():
    # The real file is already in toposort order (test_k8s_toposort.py holds it there), so
    # only a reordered list shows that the view is sorted rather than copied.
    raw = load_yaml(K8S_SERVICES)["containers_list"]
    view = _names(sorted_services(raw[::-1]))
    assert view != _names(raw[::-1])
    position = {name: i for i, name in enumerate(view)}
    deps = build_k8s_dep_map(raw, str(K8S_ROLES))
    assert deps["traefik"] == ["crowdsec", "traefik-crds"]
    assert all(position[d] < position[n] for n, ds in deps.items() for d in ds)


def test_the_pi_client_ip_is_the_pis_own_server_ip(ctx):
    pi_ip = load_yaml(HOST_VARS / "daniel-pi.yml")["server_ip"]
    assert _render(ctx, "{{ k8s_pi_client_ip }}") == pi_ip
    # Read from daniel-box's own layer, it would still be an address, just the wrong one.
    assert pi_ip != ctx["server_ip"]


def _render(ctx, text):
    env = template_env(ROLE / "templates", undefined_cls=StrictChainableUndefined)
    return env.from_string(text).render(ctx)


def test_an_undefined_attribute_with_a_default_is_clean(ctx):
    text = "{{ hosts['daniel-pi'].no_such_key | default('fallback') }}"
    assert _render(ctx, text) == "fallback"


def test_a_name_no_layer_supplies_is_flagged(ctx):
    # The render guards' BASE_CONTEXT stubs this name, and the renderer's context must not.
    with pytest.raises(jinja2.UndefinedError, match="k8s_built_image_tags"):
        _render(ctx, "{{ k8s_built_image_tags }}")


@pytest.mark.parametrize("name", ["inventory_hostname", "claude_code_watch_uids"])
def test_a_name_only_a_play_or_a_host_component_reads_is_flagged(ctx, name):
    with pytest.raises(jinja2.UndefinedError, match=name):
        _render(ctx, f"{{{{ {name} }}}}")


def _inventory(tmp_path, **edits):
    """Each host's layer as the renderer reads it, those named in `edits` edited."""
    host_vars = tmp_path / "host_vars"
    host_vars.mkdir()
    for path in host_files(HOST_VARS):
        layer = host_layer(HOST_VARS, path.stem)
        edit = edits.get(path.stem.replace("-", "_"))
        (host_vars / path.name).write_text(
            yaml.safe_dump(edit(layer) if edit else layer)
        )
    services = tmp_path / "services.yml"
    entries = load_yaml(K8S_SERVICES)["containers_list"]
    edit = edits.get("services")
    services.write_text(
        yaml.safe_dump({"containers_list": edit(entries) if edit else entries})
    )
    return Inventory(host_vars=host_vars, services=services)


def test_template_context_installs_the_sorted_view_over_a_reordered_list(tmp_path):
    def reverse(entries):
        return entries[::-1]

    inventory = _inventory(tmp_path, services=reverse)
    reordered = reverse(load_yaml(K8S_SERVICES)["containers_list"])
    ctx = template_context(ROLE, shadow_diff.stub_vars(), inventory=inventory)

    expected = _names(sorted_services(reordered))
    assert expected != _names(reordered)
    assert _names(ctx["services"]) == expected


def test_a_value_naming_an_undefined_variable_is_flagged(tmp_path):
    def add(layer):
        return {**layer, "zz": "a-{{ no_such_name }}", "yy": "{{ no_such_name }}"}

    inventory = _inventory(tmp_path, daniel_box=add)
    ctx = template_context(ROLE, shadow_diff.stub_vars(), inventory=inventory)
    for name in ("zz", "yy"):
        # Left undefined rather than resolved to STUB or None, so reading it stops a render.
        with pytest.raises(jinja2.UndefinedError, match=name):
            _render(ctx, f"{{{{ {name} }}}}")


def test_a_pi_host_without_its_server_ip_stops_the_render_that_reads_it(tmp_path):
    def drop(layer):
        return {k: v for k, v in layer.items() if k != "server_ip"}

    inventory = _inventory(tmp_path, daniel_pi=drop)
    ctx = template_context(ROLE, shadow_diff.stub_vars(), inventory=inventory)
    with pytest.raises(jinja2.UndefinedError, match="k8s_pi_client_ip"):
        _render(ctx, "{{ k8s_pi_client_ip }}")
