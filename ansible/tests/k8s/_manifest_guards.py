"""Paths, inventory vars and the single-template renderer the `test_k8s_manifests_*` guards share.

These guards render ONE template with a hand-built context to assert on its output, which is
the shape `_k8s_render.rendered_docs` (every template, the deploy's own context) does not
serve.
"""

from pathlib import Path

from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env, template_env
from lib.render_guard import BUILT_IMAGE_TAG_STUBS
from jinja2 import Undefined

from lib.k8s_roles import manifest_template
from validate.k8s_manifests import make_lookup
from _helpers import ANSIBLE


K3S = ANSIBLE / "roles" / "setup" / "k3s"

K8S = ANSIBLE / "roles" / "k8s"

ALL_VARS = yaml_fast.safe_load(
    (ANSIBLE / "inventory" / "group_vars" / "all.yml").read_text()
)

BOX_VARS = yaml_fast.safe_load(
    (ANSIBLE / "inventory" / "host_vars" / "daniel-box.yml").read_text()
)


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


def _role_defaults(role: str) -> dict:
    """A role's defaults with `{{ ... }}` inside VALUES expanded, as Ansible expands them.

    n8n's image default is `"{{ k8s_registry_pull_host }}/n8n:{{ k8s_built_image_tags.get(...)
    }}"`, and `k8s_registry_pull_host` is itself `"localhost:{{ k8s_registry_port }}"` — so the
    raw YAML carries braces two levels deep. Passed through unexpanded they reach the rendered
    manifest, where `{` opens a flow mapping and the whole document fails to parse for a reason
    that has nothing to do with the template being tested.

    `k8s_built_image_tags` is a play fact k8s/image-builder publishes at deploy time, so it
    reaches no defaults file; `BUILT_IMAGE_TAG_STUBS` stands in, the same map `_k8s_render.py`
    renders against through BASE_CONTEXT.
    """
    values = {
        "k8s_built_image_tags": BUILT_IMAGE_TAG_STUBS,
        **ALL_VARS,
        **yaml_fast.safe_load((K8S / role / "defaults" / "main.yml").read_text()),
    }
    # `bool` is an Ansible filter, not a Jinja builtin — a group_var using it (k8s_no_mutate)
    # would fail this loop with "No filter named 'bool'". `make_ansible_env` registers
    # ansible-core's own `to_bool`; `lib.k8s_context`'s shim exists for a latency budget a
    # test does not have.
    env = make_ansible_env([ANSIBLE / "templates"], undefined_cls=Undefined)
    for _ in range(5):
        pending = {k: v for k, v in values.items() if isinstance(v, str) and "{{" in v}
        if not pending:
            break
        for key, value in pending.items():
            values[key] = env.from_string(value).render(values)
    return values


K3S_DEFAULTS = yaml_fast.safe_load((K3S / "defaults" / "main.yml").read_text())
