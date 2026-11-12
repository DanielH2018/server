"""The play-time context `renderer_lib.context` builds: the k8s play's view and `hostvars`.

Run: uv run pytest scripts/renderer/tests/test_renderer_context.py
"""

import jinja2
import pytest
import yaml

from lib.ansible_jinja_env import template_env
from lib.estate import Inventory
from lib.render_guard import StrictChainableUndefined, host_files, host_layer, load_yaml
from lib.repo_paths import ANSIBLE, HOST_VARS, K8S_ROLES, K8S_SERVICES
from renderer import shadow_diff
from renderer.renderer_lib.context import play_containers_list, play_context
from toposort import build_k8s_dep_map, filter_by_platform, toposort_containers

ROLE = K8S_ROLES / "littlelink"


@pytest.fixture(scope="module")
def ctx():
    return play_context(ROLE, shadow_diff.stub_vars())


def _names(entries):
    return [e["name"] for e in entries]


def test_containers_list_is_the_play_view_in_toposort_order(ctx):
    raw = load_yaml(K8S_SERVICES)["containers_list"]
    k8s = filter_by_platform(raw, "k8s")
    expected = _names(toposort_containers(k8s, build_k8s_dep_map(k8s, str(ANSIBLE))))
    assert _names(ctx["containers_list"]) == expected


def test_play_containers_list_drops_docker_entries_and_sorts_a_reordered_list():
    # The real file is already in toposort order (test_k8s_toposort.py holds it there), so
    # only a reordered list shows that the view is sorted rather than copied.
    raw = load_yaml(K8S_SERVICES)["containers_list"]
    reordered = [*reversed(raw), {"name": "a-docker-service"}]
    view = _names(play_containers_list(reordered))
    assert "a-docker-service" not in view
    assert view != _names(reversed(raw))
    position = {name: i for i, name in enumerate(view)}
    deps = build_k8s_dep_map(raw, str(ANSIBLE))
    assert deps["traefik"] == ["crowdsec", "traefik-crds"]
    assert all(position[d] < position[n] for n, ds in deps.items() for d in ds)


def test_hostvars_of_the_primary_node_hold_the_play_view(ctx):
    assert ctx["k8s_primary_node"] == "daniel-box"
    primary = ctx["hostvars"][ctx["k8s_primary_node"]]
    assert primary["containers_list"] == ctx["containers_list"]
    assert "hostvars" not in primary


def test_the_pi_client_ip_is_the_pis_own_server_ip(ctx):
    pi_ip = load_yaml(HOST_VARS / "daniel-pi.yml")["server_ip"]
    assert _render(ctx, "{{ k8s_pi_client_ip }}") == pi_ip
    # Read from daniel-box's own layer, it would still be an address, just the wrong one.
    assert pi_ip != ctx["server_ip"]


def _render(ctx, text):
    env = template_env(ROLE / "templates", undefined_cls=StrictChainableUndefined)
    return env.from_string(text).render(ctx)


def test_an_undefined_attribute_with_a_default_is_clean(ctx):
    text = "{{ hostvars['daniel-pi'].no_such_key | default('fallback') }}"
    assert _render(ctx, text) == "fallback"


def test_a_name_no_layer_supplies_is_flagged(ctx):
    # The render guards' BASE_CONTEXT stubs this name, and the renderer's context must not.
    with pytest.raises(jinja2.UndefinedError, match="k8s_built_image_tags"):
        _render(ctx, "{{ k8s_built_image_tags }}")


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
    return Inventory(host_vars=host_vars)


def test_play_context_installs_the_play_view_over_a_reordered_list(tmp_path):
    def reverse(layer):
        return {**layer, "containers_list": layer["containers_list"][::-1]}

    inventory = _inventory(tmp_path, daniel_box=reverse)
    reordered = reverse(host_layer(HOST_VARS, "daniel-box"))["containers_list"]
    ctx = play_context(ROLE, shadow_diff.stub_vars(), inventory=inventory)

    expected = _names(play_containers_list(reordered))
    assert expected != _names(reordered)
    assert _names(ctx["containers_list"]) == expected
    assert _names(ctx["hostvars"]["daniel-box"]["containers_list"]) == expected


def test_a_value_naming_an_undefined_variable_is_flagged(tmp_path):
    def add(layer):
        return {**layer, "zz": "a-{{ no_such_name }}", "yy": "{{ no_such_name }}"}

    inventory = _inventory(tmp_path, daniel_box=add)
    ctx = play_context(ROLE, shadow_diff.stub_vars(), inventory=inventory)
    for name in ("zz", "yy"):
        # Left undefined rather than resolved to STUB or None, so reading it stops a render.
        with pytest.raises(jinja2.UndefinedError, match=name):
            _render(ctx, f"{{{{ {name} }}}}")


def test_a_pi_host_without_its_server_ip_stops_the_render_that_reads_it(tmp_path):
    def drop(layer):
        return {k: v for k, v in layer.items() if k != "server_ip"}

    inventory = _inventory(tmp_path, daniel_pi=drop)
    ctx = play_context(ROLE, shadow_diff.stub_vars(), inventory=inventory)
    with pytest.raises(jinja2.UndefinedError, match="k8s_pi_client_ip"):
        _render(ctx, "{{ k8s_pi_client_ip }}")
