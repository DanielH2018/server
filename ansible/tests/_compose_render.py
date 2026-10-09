"""Render the Pi's Docker roles' templates, for tests that need to assert on the OUTPUT.

A compose guard that scans the template's source text cannot tell a key from a commented-out
copy of it: a `# - WEBHOOK_JSON_KEY=content` line or a `{# … #}`-wrapped `healthcheck:` block
still matches the pattern while the container ships without it. Parsing the rendered
service sees what Compose would.

The same gap reaches a role's bind-mounted CONFIG templates, which `_k8s_render` does not
cover (it renders `roles/k8s/` only). alloy's `config.alloy.j2` is River, so its guard reads
text rather than a parsed document — `rendered_texts` is that text, with `{{ domain }}`
already expanded (#3175).

The context comes from `lib.render_context`, the builder `validate/compose_templates.py`
renders with, so a guard here asserts on the context the gate lints (#3792). Values are
resolved the way Ansible resolves them, and a role's own defaults go under the inventory, so a
variable that aliases a secret (`x: "{{ some_secret }}"`) reaches the render as the secret's
value rather than as literal braces (#3191).

Secret values render as `STUB` (the `DECIDED:` rule in `lib/render_guard.py`), so a guard built
on this keys on field names, never on credential-shaped values.
"""

from pathlib import Path

from _helpers import CONTAINER_ROLES, load_yaml
from lib import yaml_fast
from lib.ansible_jinja_env import template_env
from lib.render_context import render_context
from lib.render_guard import (
    HOST_VARS,
    containers_entries_in,
    entry_platform,
    render_or_error,
)

PI = "daniel-pi"


def host_vars(host: str = PI) -> dict:
    """`host`'s host_vars, for a caller that layers its own overrides before rendering."""
    return load_yaml(HOST_VARS / f"{host}.yml")


def host_context(
    vars_: dict | None = None,
    role_dir: Path = CONTAINER_ROLES,
    overrides: dict | None = None,
) -> dict:
    """The resolved context a render of a template under `role_dir` starts from.

    Built by `render_context`, as `validate/compose_templates.py` builds it: role defaults
    under all.yml under the host's vars, each value resolved against the whole, so an alias
    of a secret, in the inventory or in a default, carries whatever the secret holds. A value
    that will not expand is dropped and renders as `STUB`, never as literal braces.

    `containers_list` is dropped the way the validator drops it: a template reads its OWN
    entry through `container_item`, and leaving the list in would let one render against
    another service's entry.

    Exposed so a guard can assert on a value the render expanded. The Pi's Alloy config names
    `loki-homelab.local.{{ domain }}`; the plaintext inventory carries no `domain`, so the
    render uses the stub `render_context` starts from, and a guard that hardcoded a domain
    would hold only while that stub does.

    Args:
        vars_: The host's vars. None reads daniel-pi's host_vars file. A dict REPLACES that
            file rather than laying keys over it, so a test can render a host that leaves a
            key unset: it goes in as overrides with no host named, and a `roles/containers/`
            path has no plane host to fall back on.
        role_dir: The role whose `defaults/main.yml` goes under the inventory. The default,
            the plane directory, holds no defaults file, for a caller that wants the
            inventory alone.
        overrides: Values laid over everything, the validator's `container_item` among them.
    """
    if vars_ is None:
        ctx = render_context(role_dir, host=PI, overrides=overrides)
    else:
        ctx = render_context(role_dir, overrides={**vars_, **(overrides or {})})
    ctx.pop("containers_list", None)
    return ctx


def _entry(role: str, vars_: dict) -> dict:
    return next(c for c in vars_["containers_list"] if c["name"] == role)


def render_role_template(
    role: str,
    template: str = "docker-compose.yml.j2",
    vars_: dict | None = None,
    roles: Path = CONTAINER_ROLES,
) -> str:
    """One Docker role's template rendered as TEXT, with `vars_` (default: the Pi's) as the host.

    Fails the calling test on a render error rather than returning nothing, so a template that
    stopped rendering cannot read as a service with no findings. `roles` is a parameter so a
    test can point it at a `tmp_path` tree.
    """
    entry = _entry(role, host_vars() if vars_ is None else vars_)
    path = roles / role / "templates" / template
    ctx = host_context(vars_, roles / role, {"container_item": entry})
    rendered, err = render_or_error(template_env(path.parent), path.name, ctx)
    assert rendered is not None, f"{role}/{template}: {err}"
    return rendered


def render_service(role: str, vars_: dict | None = None) -> dict:
    """The `role` service as its host deploys it, with `vars_` (default: the Pi's) over the base."""
    return yaml_fast.safe_load(render_role_template(role, vars_=vars_))["services"][
        role
    ]


_TEXTS: dict[str, tuple[tuple[str, str, str], ...]] = {}


def rendered_texts(host: str = PI) -> tuple[tuple[str, str, str], ...]:
    """(role, template name, rendered TEXT) for every template of every Docker role on `host`.

    Every `templates/*.j2` of every non-k8s `containers_list` entry, compose and bind-mounted
    config alike — the Pi's equivalent of `_k8s_render.rendered_texts()`. The whole set is
    rendered rather than only what a caller asks for, so the render failure of a template no
    guard reads yet still arrives as a failure here.

    Cached per host: the render is a pure function of the repo tree, which no test writes to.
    """
    if host not in _TEXTS:
        _TEXTS[host] = render_texts(host_vars(host))
    return _TEXTS[host]


def render_texts(
    vars_: dict, roles: Path = CONTAINER_ROLES
) -> tuple[tuple[str, str, str], ...]:
    """`rendered_texts` for a host whose vars are `vars_`, uncached.

    For a census at a value the inventory does not hold: lay a secret's sentinel over
    `host_vars()` and every template whose render reaches the secret carries the sentinel,
    whether it names the secret or reads it through an alias in the inventory or a role
    default. `roles` is a parameter so a test can point it at a `tmp_path` tree.
    """
    texts = []
    for entry in containers_entries_in(vars_):
        if entry_platform(entry) == "k8s":
            continue
        role = entry["name"]
        for tpl in sorted((roles / role / "templates").glob("*.j2")):
            texts.append(
                (role, tpl.name, render_role_template(role, tpl.name, vars_, roles))
            )
    return tuple(texts)


def rendered_text(role: str, template: str, host: str = PI) -> str:
    """One entry of `rendered_texts`, by role and template name.

    Reads the cache rather than rendering again, and fails naming the template when the set
    does not hold it — a renamed template is a guard over nothing, not a passing guard.
    """
    for name, tpl, text in rendered_texts(host):
        if (name, tpl) == (role, template):
            return text
    raise AssertionError(
        f"{role}/{template} is not among the rendered templates for {host}: "
        f"{sorted((n, t) for n, t, _ in rendered_texts(host))}"
    )
