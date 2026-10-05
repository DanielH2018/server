"""The three render accessors reach every artifact a guard asserts on, and fail loudly otherwise.

`_compose_render.rendered_texts`, `_k8s_render.rendered_build_texts` and
`_shell_render.rendered_shell_texts` are the accessors for the three template classes the
manifest harness skips: the Pi's Docker roles, every k8s role's `Dockerfile*.j2`, and every
role's `*.sh.j2`. A guard built on any of them reads a set it does not enumerate itself, so an
accessor that quietly came back short would turn each of those guards into a pass over nothing
(#3175, #3178).

`_k8s_render.render_role_template` is covered here too, for the same reason rather than because
it skips anything: a guard reaches for a template's BYTES when the accessor cannot render it,
and an included helper role with no `containers_list` entry was one such case (#3107).

Run: uv run pytest ansible/tests/repo/test_shared_render_accessors.py
"""

import pytest

from _compose_render import (
    host_vars,
    rendered_text,
    rendered_texts,
    render_role_template,
)
from _helpers import ROLES, load_defaults
from _k8s_render import (
    BUILD_ROLES,
    rendered_build_text,
    rendered_build_texts,
    # Aliased: `_compose_render` exports a `render_role_template` of its own, for the Pi.
    render_role_template as render_k8s_template,
)
from _shell_render import (
    render_shell_script,
    rendered_shell_function,
    rendered_shell_text,
    rendered_shell_texts,
)
from lib import yaml_fast
from lib.render_guard import containers_entries_in, entry_platform
from validate.shell_templates import discover_templates

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


def test_an_included_role_with_no_census_entry_renders() -> None:
    """`k8s/image-builder` is included by caller roles and is in no `containers_list`.

    The accessor looked its entry up with a bare `next()`, so asking it for such a role raised
    `StopIteration` before the render started — and a guard had to read the template's bytes
    instead, which is the exposure #2809's series removes. The caller's own `vars:` arrive as
    overrides, so the Job renders under the name its caller hands over.
    """
    job = yaml_fast.safe_load(
        render_k8s_template(
            "image-builder", "build-job.yaml.j2", {"image_builder_name": "freshrss"}
        )
    )
    assert job["kind"] == "Job"
    assert job["metadata"]["name"] == "build-freshrss"
    # The role's own defaults still reach the render; only the census entry is absent.
    containers = job["spec"]["template"]["spec"]["containers"]
    assert containers[0]["image"] == IMAGE_BUILDER_DEFAULTS["image_builder_k8s_image"]


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


# The shell templates a guard reads today, across `ansible/tests/services/` (#3178) and
# `ansible/tests/longhorn/` (#3186). Named so a renamed or moved template fails as a missing
# member rather than as a guard over an empty set.
GUARDED_SHELL_TEMPLATES = frozenset(
    {
        ("k8s", "crowdsec", "crowdsec-update-home-allowlist.sh.j2"),
        ("k8s", "artifacts", "sync-artifacts.sh.j2"),
        ("setup", "k3s", "longhorn-restore-drill.sh.j2"),
        ("setup", "k3s", "longhorn-backup-health.sh.j2"),
    }
)

ARTIFACTS_DEFAULTS = load_defaults(ROLES / "k8s" / "artifacts")
IMAGE_BUILDER_DEFAULTS = load_defaults(ROLES / "k8s" / "image-builder")


def test_every_shell_template_in_the_tree_is_rendered() -> None:
    rendered = {entry[:3] for entry in rendered_shell_texts()}
    assert GUARDED_SHELL_TEMPLATES <= rendered, (
        f"accessor lost {sorted(GUARDED_SHELL_TEMPLATES - rendered)}"
    )
    # The same roster the shellcheck gate sweeps: one accessor, one render, no second set.
    assert len(rendered) == len(discover_templates())


def test_a_rendered_shell_template_has_its_role_defaults_expanded() -> None:
    """The layering this accessor exists for, and the red proof for it.

    `StubUndefined` iterates empty, so a context without `artifacts_peer_sources` renders
    `sync-artifacts.sh` with its whole peer loop — every rsync call and every streak line —
    dropped. `"{{" not in text` passes just as happily on that emptied render, so the assertion
    that catches it is the loop body being THERE, at the threshold the defaults declare.
    """
    script = rendered_shell_text("k8s", "artifacts", "sync-artifacts.sh.j2")
    assert "{{" not in script
    peer = ARTIFACTS_DEFAULTS["artifacts_peer_sources"][0]["name"]
    assert f"{peer} failed (consecutive run" in script
    threshold = ARTIFACTS_DEFAULTS["artifacts_sync_alert_after_failures"]
    assert f'[ "$fails" -eq {threshold} ]' in script


def test_an_unrendered_shell_template_is_named_rather_than_missed() -> None:
    with pytest.raises(AssertionError, match="sync-artifact.sh.j2"):
        rendered_shell_text("k8s", "artifacts", "sync-artifact.sh.j2")


def test_a_shell_render_takes_an_override_the_inventory_does_not_hold() -> None:
    """The artifacts guard runs the script for real, so its peer tree must be a tmp_path."""
    script = render_shell_script(
        "k8s",
        "artifacts",
        "sync-artifacts.sh.j2",
        overrides={"artifacts_peer_dir": "/tmp/peer"},
    )
    assert "/tmp/peer/" in script


def test_an_override_beats_a_default_the_role_itself_declares() -> None:
    """The restore-drill harness renders into a `tmp_path`, which is a key the role DOES declare.

    The case above proves an override lands where nothing else defines the name. This one proves
    precedence: if the role's own default won, `ansible/tests/longhorn/_restore_drill.py` would
    render the production stamp tree and its tests would write there.
    """
    declared = load_defaults(ROLES / "setup" / "k3s")[
        "k3s_longhorn_restore_drill_stamp_dir"
    ]
    script = render_shell_script(
        "setup",
        "k3s",
        "longhorn-restore-drill.sh.j2",
        overrides={"k3s_longhorn_restore_drill_stamp_dir": "/tmp/drill-stamps"},
    )
    assert 'STAMP_DIR="/tmp/drill-stamps"' in script
    assert declared not in script


def test_a_shell_template_that_is_not_in_the_tree_fails_the_caller() -> None:
    with pytest.raises(AssertionError, match="no-such-script.sh.j2"):
        render_shell_script("k8s", "artifacts", "no-such-script.sh.j2")


def test_a_shell_function_the_script_does_not_define_is_named() -> None:
    with pytest.raises(AssertionError, match=r"no_such_function\(\) is gone"):
        rendered_shell_function(
            "k8s",
            "crowdsec",
            "crowdsec-update-home-allowlist.sh.j2",
            "no_such_function",
        )
