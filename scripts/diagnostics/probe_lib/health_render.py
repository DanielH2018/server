"""The render context `probe.py health` resolves a role's workloads with, built once per process.

`health.py` renders a role's templates to find the workloads it declares, and the alert plane
reads the role's `containers_list` entry. Both come from the validator module, so this module
builds that pair once and hands it to every caller.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

_RENDER_CONTEXT = None

from lib.repo_paths import K8S_ROLES  # noqa: E402


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


def rendered_docs(role, roles_dir=K8S_ROLES):
    """The documents `role`'s manifest templates render to, or None for a role not rendered here.

    None when `role` is not a k8s role this can resolve, so the caller falls back to probing
    the tag name itself. Raises on a render failure rather than returning None, so a role whose
    templates stopped rendering cannot degrade to the guess-the-name path, where a miss reads
    as a skip. `health._role_kind_targets` and `health.role_suspended_cronjobs` read the same
    render, so the gate and the suspend check cannot disagree about what the role declares.
    """
    role_dir = roles_dir / role
    if not role_dir.is_dir():
        return None

    validator, entries = render_context()
    if role in validator.SKIP_ROLES or role not in entries:
        return None

    templates = sorted(
        p
        for p in (role_dir / "templates").glob("*.j2")
        if validator.is_manifest_template(p)
    )
    ctx = validator.render_context(
        role_dir, overrides={"container_item": entries[role]}, strict=True
    )
    docs = []
    for tpl in templates:
        err, rendered = validator.check_template(role, tpl, ctx)
        if err:
            raise RuntimeError(f"{role}/{tpl.name} failed to render: {err}")
        docs += [doc for doc in rendered if isinstance(doc, dict)]
    return docs
