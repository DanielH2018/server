"""The play-time context `renderer_lib.context` builds: the k8s play's view and `hostvars`.

Run: uv run pytest scripts/renderer/tests/test_renderer_context.py
"""

import jinja2
import pytest

from lib.ansible_inventory import inventory_hosts
from lib.ansible_jinja_env import template_env
from lib.render_guard import load_yaml
from lib.repo_paths import ANSIBLE, HOST_VARS, HOSTS_INI, K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib.cluster import StrictChainableUndefined
from renderer.renderer_lib.context import play_containers_list, play_context
from toposort import build_k8s_dep_map, filter_by_platform, toposort_containers

ROLE = K8S_ROLES / "littlelink"


@pytest.fixture(scope="module")
def ctx():
    return play_context(ROLE, shadow_diff.stub_vars())


def _names(entries):
    return [e["name"] for e in entries]


def test_containers_list_is_the_play_view_in_toposort_order(ctx):
    raw = load_yaml(HOST_VARS / "daniel-box.yml")["containers_list"]
    k8s = filter_by_platform(raw, "k8s")
    expected = _names(toposort_containers(k8s, build_k8s_dep_map(k8s, str(ANSIBLE))))
    assert _names(ctx["containers_list"]) == expected


def test_play_containers_list_drops_docker_entries_and_sorts_a_reordered_list():
    # The real file is already in toposort order (test_k8s_toposort.py holds it there), so
    # only a reordered list shows that the view is sorted rather than copied.
    raw = load_yaml(HOST_VARS / "daniel-box.yml")["containers_list"]
    reordered = [*reversed(raw), {"name": "a-docker-service"}]
    view = _names(play_containers_list(reordered))
    assert "a-docker-service" not in view
    assert view != _names(reversed(raw))
    position = {name: i for i, name in enumerate(view)}
    deps = build_k8s_dep_map(raw, str(ANSIBLE))
    assert deps["traefik"] == ["crowdsec"]
    assert all(position[d] < position[n] for n, ds in deps.items() for d in ds)


def test_hostvars_of_the_primary_node_hold_the_play_view(ctx):
    assert ctx["k8s_primary_node"] == "daniel-box"
    primary = ctx["hostvars"][ctx["k8s_primary_node"]]
    assert primary["containers_list"] == ctx["containers_list"]
    assert "hostvars" not in primary


def test_hostvars_of_the_pi_take_ansible_user_from_hosts_ini(ctx):
    (pi,) = [h for h in inventory_hosts(HOSTS_INI) if h.name == "daniel-pi"]
    assert "ansible_user" not in load_yaml(HOST_VARS / "daniel-pi.yml")
    assert ctx["hostvars"]["daniel-pi"]["ansible_user"] == pi.settings["ansible_user"]


def test_hostvars_of_the_pi_resolve_ansible_host_to_its_own_server_ip(ctx):
    pi_ip = load_yaml(HOST_VARS / "daniel-pi.yml")["server_ip"]
    assert ctx["hostvars"]["daniel-pi"]["ansible_host"] == pi_ip
    # Resolved against daniel-box's context, it would still be an address, just the wrong one.
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
