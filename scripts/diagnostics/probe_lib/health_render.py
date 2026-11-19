"""What `probe.py health` reads off a role's render, built once per process.

`health.py` renders a role's templates to find the workloads it declares, and the alert plane
reads the role's `containers_list` entry. This module reads the entries once and hands them to
every caller.
"""

import functools
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.repo_paths import K8S_ROLES


@functools.cache
def entries() -> dict[str, dict]:
    """The `containers_list` entries by name, read once per process.

    The import is deferred because it pulls in PyYAML and the render pieces, and twelve of
    probe.py's thirteen subcommands never need any of it. Measured on daniel-box: 0.41s to
    import with the k8s render family, ~5 ms to build one role's context (2026-10-09), 0.02s
    median to render one role and 0.22s for the slowest (home-assistant) — against the 300s
    `PROBE_TIMEOUT_S` the notifier allows.
    """
    # The bootstrap above put `scripts/` on sys.path, the root pytest imports from.
    from lib.k8s_roles import k8s_entries

    return k8s_entries()


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

    from lib.k8s_roles import SKIP_ROLES, is_manifest_template
    from lib.render_context import render_context
    from validate.render_lib.k8s import check_template

    entry = entries().get(role)
    if role in SKIP_ROLES or entry is None:
        return None

    templates = sorted(
        p for p in (role_dir / "templates").glob("*.j2") if is_manifest_template(p)
    )
    ctx = render_context(role_dir, overrides={"container_item": entry}, strict=True)
    docs = []
    for tpl in templates:
        err, rendered = check_template(role, tpl, ctx)
        if err:
            raise RuntimeError(f"{role}/{tpl.name} failed to render: {err}")
        docs += [doc for doc in rendered if isinstance(doc, dict)]
    return docs
