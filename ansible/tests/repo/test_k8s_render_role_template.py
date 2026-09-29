"""`render_role_template` renders under the host's precedence, and an override reaches it.

Pins the two decisions in its docstring that seven guards now depend on. The subject is
`traefik_k8s_manage_crowdsec`, which defaults true: Traefik's rendered static config carries
the bouncer only when the flag holds, so the resolved value is visible in the output and not
just in the context.

daniel-stage was the host that set it false, until #2941 retired it. No host_vars file
overrides a role default today, so the precedence half is driven by laying a role default
against an override rather than against a host.
"""

from lib import yaml_fast

from _k8s_render import host_context, render_role_template

_FLAG = "traefik_k8s_manage_crowdsec"


def _bouncer_declared(host: str, overrides: dict | None = None) -> bool:
    text = render_role_template(
        "traefik", "static-config.yaml.j2", overrides, host=host
    )
    # Off the parsed entrypoint chains, not a substring: comments name crowdsec either way.
    config = yaml_fast.safe_load(yaml_fast.safe_load(text)["data"]["traefik.yml"])
    ref = f"{host_context(host)['k8s_namespace']}-crowdsec@kubernetescrd"
    return any(
        ref in (ep.get("http", {}).get("middlewares") or [])
        for ep in config["entryPoints"].values()
    )


def test_the_role_default_reaches_the_render():
    """No host_vars file sets the flag, so what the render carries is the role's own default."""
    assert _FLAG not in host_context("daniel-box")
    assert _bouncer_declared("daniel-box")


def test_an_override_beats_the_role_default():
    """Both directions, so an override silently ignored fails rather than agreeing."""
    assert not _bouncer_declared("daniel-box", {_FLAG: False})
    assert _bouncer_declared("daniel-box", {_FLAG: True})
