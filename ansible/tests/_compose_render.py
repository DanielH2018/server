"""Render the Pi's Docker roles' templates, for tests that need to assert on the OUTPUT.

A compose guard that scans the template's source text cannot tell a key from a commented-out
copy of it: a `# - WEBHOOK_JSON_KEY=content` line or a `{# … #}`-wrapped `healthcheck:` block
still matches the pattern while the container ships without it. Parsing the rendered
service sees what Compose would.

The same gap reaches a role's bind-mounted CONFIG templates, which `_k8s_render` does not
cover (it renders `roles/k8s/` only) and `validate/config_templates.py` renders for its own
YAML parse without exposing the text. alloy's `config.alloy.j2` is River, so its guard reads
text rather than a parsed document — `rendered_texts` is that text, with `{{ domain }}`
already expanded (#3175).

Secret values render as `STUB` (the `DECIDED:` rule in `lib/render_guard.py`), so a guard built
on this keys on field names, never on credential-shaped values.
"""

from _helpers import CONTAINER_ROLES, load_yaml
from lib import yaml_fast
from lib.ansible_jinja_env import template_env
from lib.render_guard import (
    ALL_VARS,
    BASE_CONTEXT,
    HOST_VARS,
    containers_entries_in,
    entry_platform,
    render_or_error,
)


def host_vars(host: str = "daniel-pi") -> dict:
    """`host`'s host_vars, for a caller that layers its own overrides before rendering."""
    return load_yaml(HOST_VARS / f"{host}.yml")


def host_context(vars_: dict | None = None) -> dict:
    """The resolved context a render starts from: base stubs, all.yml, then the host's vars.

    `containers_list` is dropped the way `validate/compose_templates.py` drops it — a template
    reads its OWN entry through `container_item`, and leaving the list in would let one render
    against another service's entry.

    Exposed so a guard can assert on a value the render expanded. The Pi's Alloy config names
    `loki-homelab.local.{{ domain }}`; the plaintext inventory carries no `domain`, so the
    render uses `lib/render_guard.py:BASE_CONTEXT`'s stub and a guard that hardcoded a domain
    would hold only while that stub does.
    """
    vars_ = host_vars() if vars_ is None else vars_
    ctx = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **vars_}
    ctx.pop("containers_list", None)
    return ctx


def _entry(role: str, vars_: dict) -> dict:
    return next(c for c in vars_["containers_list"] if c["name"] == role)


def render_role_template(
    role: str, template: str = "docker-compose.yml.j2", vars_: dict | None = None
) -> str:
    """One Docker role's template rendered as TEXT, with `vars_` (default: the Pi's) as the host.

    Fails the calling test on a render error rather than returning nothing, so a template that
    stopped rendering cannot read as a service with no findings.
    """
    vars_ = host_vars() if vars_ is None else vars_
    path = CONTAINER_ROLES / role / "templates" / template
    ctx = {**host_context(vars_), "container_item": _entry(role, vars_)}
    rendered, err = render_or_error(template_env(path.parent), path.name, ctx)
    assert rendered is not None, f"{role}/{template}: {err}"
    return rendered


def render_service(role: str, vars_: dict | None = None) -> dict:
    """The `role` service as its host deploys it, with `vars_` (default: the Pi's) over the base."""
    return yaml_fast.safe_load(render_role_template(role, vars_=vars_))["services"][
        role
    ]


_TEXTS: dict[str, tuple[tuple[str, str, str], ...]] = {}


def rendered_texts(host: str = "daniel-pi") -> tuple[tuple[str, str, str], ...]:
    """(role, template name, rendered TEXT) for every template of every Docker role on `host`.

    Every `templates/*.j2` of every non-k8s `containers_list` entry, compose and bind-mounted
    config alike — the Pi's equivalent of `_k8s_render.rendered_texts()`. The whole set is
    rendered rather than only what a caller asks for, so the render failure of a template no
    guard reads yet still arrives as a failure here.

    Cached per host: the render is a pure function of the repo tree, which no test writes to.
    """
    if host not in _TEXTS:
        vars_ = host_vars(host)
        texts = []
        for entry in containers_entries_in(vars_):
            if entry_platform(entry) == "k8s":
                continue
            role = entry["name"]
            for tpl in sorted((CONTAINER_ROLES / role / "templates").glob("*.j2")):
                texts.append(
                    (role, tpl.name, render_role_template(role, tpl.name, vars_))
                )
        _TEXTS[host] = tuple(texts)
    return _TEXTS[host]


def rendered_text(role: str, template: str, host: str = "daniel-pi") -> str:
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
