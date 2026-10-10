"""Paths, inventory vars and the single-template renderer the `test_k8s_manifests_*` guards share.

These guards render ONE template, with `_role_context`'s context and the values a guard
overrides, to assert on its output. `_k8s_render.rendered_docs` renders every template at the
deploy's own context and does not serve that shape.
"""

from pathlib import Path

from lib import yaml_fast
from lib.ansible_jinja_env import template_env
from lib.render_context import render_context
from jinja2 import Undefined

from lib.k8s_roles import manifest_template
from validate.k8s_manifests import make_lookup
from _helpers import ANSIBLE
from lib.repo_paths import ALL_VARS, HOST_VARS, K3S_DEFAULTS


K8S = ANSIBLE / "roles" / "k8s"

ALL_VARS_VALUES = yaml_fast.safe_load(ALL_VARS.read_text())

BOX_VARS = yaml_fast.safe_load((HOST_VARS / "daniel-box.yml").read_text())

SERVER_VARS = yaml_fast.safe_load((HOST_VARS / "daniel-server.yml").read_text())


def _render(path: Path, **ctx) -> str:
    """Render a template with the given context; undefined values are left to raise.

    `playbook_dir` and `lookup`/`hash` are wired the same way `validate/k8s_manifests.py`
    wires them, not just the caller's own `ctx` — a `deployment.yaml.j2` this renders may
    call `ansible/templates/checksum-annotation.yml.j2`'s path-mode
    `lookup('file', playbook_dir + ..., rstrip=False) | hash('sha1')`
    (valheim-stats', terraria-stats'), and this helper renders every role's
    `deployment.yaml.j2` generically, not just the ones a caller anticipated.
    """
    ctx.setdefault("playbook_dir", str(ANSIBLE))
    env = template_env(path.parent, undefined_cls=Undefined)
    env.globals.update(ctx)
    env.globals["lookup"] = make_lookup(ctx)
    return env.get_template(path.name).render(**ctx)


def _k8s_entries() -> list[dict]:
    return [c for c in BOX_VARS["containers_list"] if c.get("platform") == "k8s"]


def _route_template(role: str) -> Path | None:
    """The template a role's main IngressRoute renders from, or None if it has no route.

    Its own `templates/ingressroute.yaml.j2`, else the shared default. A guard that asked for
    the role's own path alone would stop covering the roles whose route comes from
    `ansible/templates/ingressroute-default.yaml.j2`, and these guards skip a role
    they find no template for -- so the coverage would go quietly.
    `manifest_template` is the one resolver the deploy, the docs generators and these guards
    share.
    """
    return manifest_template(role, "ingressroute.yaml", K8S)


def _route_templates(role: str) -> list[Path]:
    """Every template a role's IngressRoutes render from, main route and extra ones alike."""
    own = sorted((K8S / role / "templates").glob("ingressroute*.j2"))
    shared = _route_template(role)
    if shared is not None and shared not in own:
        own.append(shared)
    return own


def _role_context(role: str, **overrides) -> dict:
    """The context `role`'s templates render with in a deploy, `overrides` laid on top.

    `lib.render_context`'s, the one the validator and `_k8s_render` render with: role defaults
    under all.yml and daniel-box's host_vars, which is Ansible's own precedence. A context built
    here by hand put the defaults over all.yml and left host_vars out, so it agreed with the
    deploy only while no inventory key shared a name with a default.

    `overrides` go in before anything resolves, so a default that reads an overridden name
    (`x: "{{ domain }}"`) expands to the override rather than to its stub.
    """
    return render_context(K8S / role, overrides=overrides, strict=True)


K3S_DEFAULT_VALUES = yaml_fast.safe_load(K3S_DEFAULTS.read_text())
