"""The environment the pod gets, rendered from `templates/`, for the suite.

The settings render from `templates/config/env.yaml.j2` into the Deployment's `env`, and the
credentials from `templates/env-secret.yaml.j2` (#5574). Since #3659 every key either renders
has no default in `bridge/config*.py`, so a test that builds a `Config` starts from this render
rather than from an empty dict. It is the same render a deploy of daniel-box makes
(`_k8s_render.render_pod_env`), with two changes so a test cannot reach a real service by
accident:

- A SOPS secret is undefined in a render and comes out as `STUB`. Each one is blanked, which is
  the value that disables the check reading it — the convention every check follows.
- Nothing else is touched, so a threshold a test reads is the deployed threshold.
"""

import functools

from _k8s_render import render_pod_env

UNDEFINED = "STUB"


@functools.cache
def _rendered() -> tuple[tuple[str, str], ...]:
    return tuple(
        (key, "" if str(value) == UNDEFINED else str(value))
        for key, value in render_pod_env("monitor-bridge").items()
    )


def bridge_env(**overrides: str) -> dict[str, str]:
    """A fresh copy of the rendered env, with `overrides` laid on top."""
    return {**dict(_rendered()), **overrides}
