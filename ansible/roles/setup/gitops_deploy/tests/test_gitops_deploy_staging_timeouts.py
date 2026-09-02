"""The staging timeouts exist in three places, and only one of them gates production.

defaults/main.yml is the source; config.env.j2 renders it onto the host; gitops_deploy.py
carries a `C.get(name, "<literal>")` fallback for a host whose config predates that render.
Until 2026-08-29 the render was missing entirely, so the FALLBACK was what production ran on
and editing the defaults would have moved nothing. These pin all three together.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_staging_timeouts.py

import pathlib
import re

import yaml

_SRC = pathlib.Path(__file__).resolve().parents[1] / "files" / "gitops_deploy.py"
_TEMPLATES = pathlib.Path(__file__).parents[1] / "templates"
_DEFAULTS = pathlib.Path(__file__).parents[1] / "defaults" / "main.yml"


def _env_fallbacks(source: str) -> dict[str, int]:
    """The literal defaults gitops_deploy.py falls back to when config.env lacks a key."""
    return {
        name: int(value)
        for name, value in re.findall(
            r'C\.get\(\s*"([A-Z0-9_]+)"\s*,\s*"(\d+)"\s*\)', source
        )
    }


def _rendered_env_keys(template: str) -> set[str]:
    """The keys config.env.j2 actually emits (`KEY={{ ... }}` or `KEY=literal`)."""
    return set(re.findall(r"^([A-Z0-9_]+)=", template, re.MULTILINE))


def test_staging_timeout_fallbacks_match_the_ansible_defaults():
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    fallbacks = _env_fallbacks(_SRC.read_text())
    for env_key, default_key in (
        ("STAGING_GATE_TIMEOUT_S", "gitops_deploy_staging_gate_timeout_s"),
        ("STAGING_EXPECT_TIMEOUT_S", "gitops_deploy_staging_expect_timeout_s"),
    ):
        assert env_key in fallbacks, (
            f"{env_key} lost its C.get fallback in gitops_deploy.py"
        )
        assert fallbacks[env_key] == int(defaults[default_key]), (
            f"gitops_deploy.py falls back to {env_key}={fallbacks[env_key]} while "
            f"{default_key} is {defaults[default_key]}. A host whose config.env predates the "
            f"render uses the fallback, so the two disagreeing means the budget the unit is "
            f"sized against is not the budget that host spends."
        )


def test_a_drifted_fallback_is_caught():
    # Red proof for the pair above: the same parser, driven with a source whose literal
    # disagrees with the default it shadows.
    assert _env_fallbacks('X = int(C.get("STAGING_GATE_TIMEOUT_S", "600"))') == {
        "STAGING_GATE_TIMEOUT_S": 600
    }
    drifted = _env_fallbacks('X = int(C.get("STAGING_GATE_TIMEOUT_S", "1200"))')
    assert drifted["STAGING_GATE_TIMEOUT_S"] != 600, (
        "the parser must read the literal rather than the name, or a drifted fallback "
        "reads as agreeing with whatever the defaults say."
    )


def test_staging_timeouts_are_rendered_into_config_env():
    keys = _rendered_env_keys((_TEMPLATES / "config.env.j2").read_text())
    for env_key in ("STAGING_GATE_TIMEOUT_S", "STAGING_EXPECT_TIMEOUT_S"):
        assert env_key in keys, (
            f"config.env.j2 must emit {env_key}, or gitops_deploy.py's C.get fallback is what "
            f"gates production and changing the Ansible default moves nothing on the host."
        )


def test_an_unrendered_key_is_caught():
    # Red proof for the test above, on the same extractor. The rejected input is the shape
    # config.env.j2 actually had until 2026-08-29: STAGING_GATE rendered, its two timeouts not.
    assert _rendered_env_keys("STAGING_GATE={{ x }}\nSTAGING_GATE_TIMEOUT_S=600\n") == {
        "STAGING_GATE",
        "STAGING_GATE_TIMEOUT_S",
    }
    assert "STAGING_GATE_TIMEOUT_S" not in _rendered_env_keys(
        "STAGING_GATE={{ x }}\n# STAGING_GATE_TIMEOUT_S is only a comment\n"
    ), "a key named only in a comment must not count as rendered"
