"""The environment the pod gets, rendered from `templates/`, for the suite.

The settings render from `templates/config/env.yaml.j2` into the Deployment's `env`, and the
credentials from `templates/env-secret.yaml.j2` (#5574). Since #3659 every key either renders
has no default in `bridge/config*.py`, so a test that builds a `Config` starts from this render
rather than from an empty dict. It is the same render a deploy of daniel-box makes
(`_k8s_render.render_pod_env`), with two changes so a test cannot reach a real service by
accident:

- A SOPS secret renders as its stub from `stub_vars`. Each one is blanked, which is the value
  that disables the check reading it — the convention every check follows.
- Nothing else is touched, so a threshold a test reads is the deployed threshold.
"""

import functools

from _k8s_render import render_pod_env
from renderer.shadow_diff import stub_vars


def blank_stubs(env: dict) -> dict[str, str]:
    """`env` as strings, with every value that is a secret's stub blanked."""
    stubs = {value for value in stub_vars().values() if isinstance(value, str)}
    return {
        key: "" if str(value) in stubs else str(value) for key, value in env.items()
    }


@functools.cache
def _rendered() -> tuple[tuple[str, str], ...]:
    return tuple(blank_stubs(render_pod_env("monitor-bridge")).items())


def bridge_env(**overrides: str) -> dict[str, str]:
    """A fresh copy of the rendered env, with `overrides` laid on top."""
    return {**dict(_rendered()), **overrides}
