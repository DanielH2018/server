"""Paths, inventory vars and the single-template renderer the `test_k8s_manifests_*` guards share.

These guards render ONE template with a hand-built context to assert on its output, which is
the shape `_k8s_render.rendered_docs` (every template, the deploy's own context) does not
serve. Split from `test_k8s_manifests.py` on 2026-09-02.
"""

from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

from validate.k8s_manifests import ansible_bool
from _helpers import ANSIBLE


K3S = ANSIBLE / "roles" / "setup" / "k3s"

K8S = ANSIBLE / "roles" / "k8s"

ALL_VARS = yaml.safe_load(
    (ANSIBLE / "inventory" / "group_vars" / "all.yml").read_text()
)

BOX_VARS = yaml.safe_load(
    (ANSIBLE / "inventory" / "host_vars" / "daniel-box.yml").read_text()
)


def _render(path: Path, **ctx) -> str:
    """Render a template with the given context; undefined values are left to raise."""
    env = Environment(
        loader=FileSystemLoader([str(path.parent), str(ANSIBLE / "templates")]),
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    env.globals.update(ctx)
    return env.get_template(path.name).render(**ctx)


def _k8s_entries() -> list[dict]:
    return [c for c in BOX_VARS["containers_list"] if c.get("platform") == "k8s"]


def _role_defaults(role: str) -> dict:
    """A role's defaults with `{{ ... }}` inside VALUES expanded, as Ansible expands them.

    n8n's image defaults are `"{{ k8s_registry_pull_host }}/n8n:latest"`, and that var is
    itself `"localhost:{{ k8s_registry_port }}"` — so the raw YAML carries braces two levels
    deep. Passed through unexpanded they reach the rendered manifest, where `{` opens a flow
    mapping and the whole document fails to parse for a reason that has nothing to do with the
    template being tested.
    """
    values = {
        **ALL_VARS,
        **yaml.safe_load((K8S / role / "defaults" / "main.yml").read_text()),
    }
    env = Environment(loader=FileSystemLoader([str(ANSIBLE / "templates")]))
    # `bool` is an Ansible filter, not a Jinja builtin — a group_var using it (k8s_no_mutate)
    # would fail this loop with "No filter named 'bool'". Same shim scripts/ registers.
    env.filters["bool"] = ansible_bool
    for _ in range(5):
        pending = {k: v for k, v in values.items() if isinstance(v, str) and "{{" in v}
        if not pending:
            break
        for key, value in pending.items():
            values[key] = env.from_string(value).render(values)
    return values


K3S_DEFAULTS = yaml.safe_load((K3S / "defaults" / "main.yml").read_text())
