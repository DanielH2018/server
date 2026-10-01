"""The CrowdSec bouncer Middleware runs no usage-metrics ticker.

The plugin's 600s metrics tick lands on every tenth 60s stream tick, and on some of those
coincidences the stream ticker skips every LAPI pull until a later metrics tick — silent
stalls of exactly 600s or 1200s that can fail a fleet deploy at crowdsec's edge ban gate.
`metricsUpdateIntervalSeconds: 0` is the only setting under which the plugin
starts no metrics ticker, and the plugin's own default is 600, so both dropping the key and
restoring any positive value bring the stall back. The reasoning lives at the DECIDED marker in
roles/k8s/traefik/templates/dynamic.yaml.j2.

Checked on the RENDERED Middleware, the same way as the sibling
test_traefik_http_entrypoint_crowdsec.py, whose `_context` shape this file reuses.
"""

import pytest

from lib import yaml_fast

from validate.k8s_manifests import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    K8S_ROLES,
    SHARED_TPL,
    load_yaml,
    make_env,
    make_lookup,
    register_ansible_filters,
    render_or_error,
    resolve_vars,
    role_defaults,
)

_ROLE = "traefik"
_HOST = "daniel-box"


def _context(host: str) -> dict:
    host_vars = ANSIBLE / "inventory" / "host_vars" / f"{host}.yml"
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **load_yaml(host_vars)}
    base["playbook_dir"] = str(ANSIBLE)
    base = resolve_vars(base, base)
    entry = next(c for c in base["containers_list"] if c["name"] == _ROLE)
    return {**role_defaults(_ROLE, base), **base, "container_item": entry}


def _bouncer_config(host: str) -> dict:
    ctx = _context(host)
    env = make_env([K8S_ROLES / _ROLE / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, "dynamic.yaml.j2", ctx)
    assert rendered is not None, (
        f"{_ROLE}/dynamic.yaml.j2 failed to render for {host}: {err}"
    )
    bouncers = [
        doc["spec"]["plugin"]["bouncer"]
        for doc in yaml_fast.safe_load_all(rendered)
        if doc
        and doc.get("kind") == "Middleware"
        and "bouncer" in (doc.get("spec", {}).get("plugin") or {})
    ]
    # Non-vacuity: a guard that finds no bouncer Middleware would pass on nothing.
    assert len(bouncers) == 1, (
        f"{host}: expected one bouncer Middleware, found {len(bouncers)}"
    )
    return bouncers[0]


def metrics_ticker_problem(bouncer: dict) -> str | None:
    """None when the plugin starts no metrics ticker, else why it would start one."""
    if "metricsUpdateIntervalSeconds" not in bouncer:
        return (
            "metricsUpdateIntervalSeconds is unset, so the plugin's default of 600 starts the "
            "metrics ticker that stalls stream pulls (#2752)"
        )
    if bouncer["metricsUpdateIntervalSeconds"] != 0:
        return (
            f"metricsUpdateIntervalSeconds is {bouncer['metricsUpdateIntervalSeconds']!r}; any "
            f"value but 0 starts the metrics ticker that stalls stream pulls (#2752)"
        )
    return None


def test_the_rendered_bouncer_starts_no_metrics_ticker() -> None:
    bouncer = _bouncer_config(_HOST)
    assert bouncer.get("crowdsecMode") == "stream", "the stall is a stream-mode defect"
    problem = metrics_ticker_problem(bouncer)
    assert problem is None, f"{_HOST}: {problem}"


@pytest.mark.parametrize(
    "bouncer",
    [
        pytest.param({"crowdsecMode": "stream"}, id="key-dropped"),
        pytest.param({"metricsUpdateIntervalSeconds": 600}, id="default-restored"),
        pytest.param({"metricsUpdateIntervalSeconds": 3600}, id="rarer-but-still-on"),
    ],
)
def test_a_running_metrics_ticker_is_flagged(bouncer: dict) -> None:
    assert metrics_ticker_problem(bouncer) is not None
