"""The render context `probe.py health` resolves a role's workloads with, built once per process.

`health.py` renders a role's templates to find the workloads it declares, and the alert plane
reads the role's `containers_list` entry. Both come from the validator module, so this module
builds that pair once and hands it to every caller.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

_RENDER_CONTEXT = None


def render_context():
    """(validator module, containers_list entries), built once per process.

    The import is deferred because it pulls in ansible-core, PyYAML and jsonschema,
    and twelve of probe.py's thirteen subcommands never need any of it. Measured on daniel-box:
    0.41s to import, ~5 ms to build one role's context (2026-10-09), 0.02s median to render
    one role and 0.22s for the slowest (home-assistant) — against the 300s `PROBE_TIMEOUT_S`
    the notifier allows.
    """
    global _RENDER_CONTEXT
    if _RENDER_CONTEXT is None:
        # The bootstrap above put `scripts/` on sys.path, the root pytest imports from.
        from validate import k8s_manifests as validator

        _RENDER_CONTEXT = (validator, validator.k8s_entries())
    return _RENDER_CONTEXT
