"""Render every k8s manifest template, for tests that need to assert on the OUTPUT.

Several guards can only be written against rendered manifests: a PVC's name and a container's
securityContext are both Jinja expressions in the template, so a text scan sees `{{ ... }}` and
silently matches nothing — which reads as "no findings" rather than "no coverage".

Rendering goes through validate_k8s_manifests' own machinery rather than a second stub set, so
what a test considers a manifest cannot drift from what that validator does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts"))

from validate_k8s_manifests import (  # noqa: E402 — needs the path insert above
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    K8S_ROLES,
    SHARED_TPL,
    SKIP_ROLES,
    k8s_entries,
    load_yaml,
    make_env,
    make_lookup,
    register_ansible_filters,
    render_or_error,
    resolve_vars,
    role_defaults,
)


def rendered_docs():
    """(role, template name, parsed doc) for every manifest the validator would render.

    Raises on a render failure rather than skipping it — a template that stopped rendering
    would otherwise quietly drop out of every guard built on this.
    """
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), "playbook_dir": str(ANSIBLE)}
    base = resolve_vars(base, base)
    entries = k8s_entries()

    for role_dir in sorted(d for d in K8S_ROLES.iterdir() if d.is_dir()):
        role = role_dir.name
        if role in SKIP_ROLES or role not in entries:
            continue
        ctx = {**base, **role_defaults(role, base), "container_item": entries[role]}
        env = make_env([role_dir / "templates", SHARED_TPL])
        env.globals["lookup"] = make_lookup(ctx)
        register_ansible_filters(env)

        for tpl in sorted(role_dir.glob("templates/*.j2")):
            if tpl.name.endswith(".sh.j2") or tpl.name.startswith("Dockerfile"):
                continue
            rendered, err = render_or_error(env, tpl.name, ctx)
            if err:
                raise AssertionError(f"{role}/{tpl.name} failed to render: {err}")
            for doc in yaml.safe_load_all(rendered):
                if isinstance(doc, dict) and doc.get("kind"):
                    yield role, tpl.name, doc
