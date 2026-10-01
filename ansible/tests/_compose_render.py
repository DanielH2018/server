"""Render one Docker role's compose template, for tests that need to assert on the OUTPUT.

A compose guard that scans the template's source text cannot tell a key from a commented-out
copy of it: a `# - WEBHOOK_JSON_KEY=content` line or a `{# … #}`-wrapped `healthcheck:` block
still matches the pattern while the container ships without it (#2809). Parsing the rendered
service sees what Compose would.

Secret values render as `STUB` (the `DECIDED:` rule in `lib/render_guard.py`), so a guard built
on this keys on field names, never on credential-shaped values.
"""

from _helpers import ANSIBLE, load_yaml
from lib import yaml_fast
from lib.ansible_jinja_env import template_env
from lib.render_guard import ALL_VARS, BASE_CONTEXT, HOST_VARS, render_or_error


def host_vars(host: str = "daniel-pi") -> dict:
    """`host`'s host_vars, for a caller that layers its own overrides before rendering."""
    return load_yaml(HOST_VARS / f"{host}.yml")


def render_service(role: str, vars_: dict | None = None) -> dict:
    """The `role` service as its host deploys it, with `vars_` (default: the Pi's) over the base.

    Fails the calling test on a render error rather than returning nothing, so a template that
    stopped rendering cannot read as a service with no findings.
    """
    vars_ = host_vars() if vars_ is None else vars_
    template = (
        ANSIBLE / "roles" / "containers" / role / "templates" / "docker-compose.yml.j2"
    )
    entry = next(c for c in vars_["containers_list"] if c["name"] == role)
    ctx = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **vars_, "container_item": entry}
    ctx.pop("containers_list", None)
    rendered, err = render_or_error(template_env(template.parent), template.name, ctx)
    assert rendered is not None, err
    return yaml_fast.safe_load(rendered)["services"][role]
