"""`render_role_template` renders under the host's precedence, and an override reaches it.

Pins the two decisions in its docstring that seven guards now depend on. The precedence one
is live: daniel-stage sets `traefik_k8s_manage_crowdsec: false` over the role default of
true, and Traefik's rendered static config carries the bouncer only when the flag holds —
so the host's value is visible in the output, not just in the context.
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


def test_a_host_var_beats_the_role_default():
    assert host_context("daniel-stage")[_FLAG] is False
    assert _FLAG not in host_context("daniel-box")
    assert _bouncer_declared("daniel-box")
    assert not _bouncer_declared("daniel-stage")


def test_an_override_beats_the_host():
    assert not _bouncer_declared("daniel-box", {_FLAG: False})
    assert _bouncer_declared("daniel-stage", {_FLAG: True})
