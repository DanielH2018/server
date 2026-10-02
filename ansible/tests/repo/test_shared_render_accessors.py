"""The two render accessors reach every artifact a guard asserts on, and fail loudly otherwise.

`_compose_render.rendered_texts` and `_k8s_render.rendered_build_texts` are the accessors for
the templates the manifest harness skips: the Pi's Docker roles, and every k8s role's
`Dockerfile*.j2`. A guard built on either reads a set it does not enumerate itself, so an
accessor that quietly came back short would turn each of those guards into a pass over nothing
(#3175).

Run: uv run pytest ansible/tests/repo/test_shared_render_accessors.py
"""

import pytest

from _compose_render import (
    host_vars,
    rendered_text,
    rendered_texts,
    render_role_template,
)
from _k8s_render import BUILD_ROLES, rendered_build_text, rendered_build_texts
from lib.render_guard import containers_entries_in, entry_platform

# The Pi's roles whose templates the accessor must render. Derived from `containers_list` below
# as well, so this names what must still be deployed there rather than only what is.
KNOWN_PI_ROLES = frozenset({"alloy", "autoheal", "docker-proxy", "wg-easy"})


def _pi_docker_roles() -> set[str]:
    return {
        entry["name"]
        for entry in containers_entries_in(host_vars())
        if entry_platform(entry) != "k8s"
    }


def test_every_pi_docker_role_is_rendered() -> None:
    rendered = {role for role, _, _ in rendered_texts()}
    assert KNOWN_PI_ROLES <= rendered, (
        f"accessor lost {sorted(KNOWN_PI_ROLES - rendered)}"
    )
    assert rendered == _pi_docker_roles()


def test_every_pi_template_is_rendered_not_just_the_compose() -> None:
    """alloy's River config is the artifact #3175 was filed for; a compose-only set misses it."""
    assert ("alloy", "config.alloy.j2") in {(r, t) for r, t, _ in rendered_texts()}


def test_a_rendered_pi_template_has_its_jinja_expanded() -> None:
    config = rendered_text("alloy", "config.alloy.j2")
    assert "{{" not in config
    assert "loki-homelab.local." in config


def test_an_unrendered_pi_template_is_named_rather_than_missed() -> None:
    with pytest.raises(AssertionError, match="config.river.j2"):
        rendered_text("alloy", "config.river.j2")


def test_a_pi_template_that_will_not_render_fails_the_caller() -> None:
    with pytest.raises(AssertionError, match="render error"):
        render_role_template("alloy", "no-such-template.j2")


def test_every_build_role_dockerfile_is_rendered() -> None:
    rendered = {role for role, _, _ in rendered_build_texts()}
    assert BUILD_ROLES <= rendered, f"accessor lost {sorted(BUILD_ROLES - rendered)}"


def test_both_n8n_dockerfiles_are_rendered() -> None:
    """n8n builds two images from one role, so a one-Dockerfile-per-role accessor is short."""
    n8n = {tpl for role, tpl, _ in rendered_build_texts() if role == "n8n"}
    assert n8n == {"Dockerfile.j2", "Dockerfile-runners.j2"}


def test_a_rendered_dockerfile_has_its_jinja_expanded() -> None:
    """code-server's build reads its node URL and every extension URL from a role default."""
    dockerfile = rendered_build_text("code-server")
    assert "{{" not in dockerfile
    assert "node-v" in dockerfile


def test_an_unrendered_dockerfile_is_named_rather_than_missed() -> None:
    with pytest.raises(AssertionError, match="Containerfile.j2"):
        rendered_build_text("code-server", "Containerfile.j2")
