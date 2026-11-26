"""The CrowdSec bouncer Middleware runs no usage-metrics ticker.

`metricsUpdateIntervalSeconds: 0` is the only setting under which the plugin starts no metrics
ticker, and the plugin's own default is 600, so dropping the key or setting a positive value
turns the ticker on, and restarts Traefik when it ships. The reasoning lives at the DECIDED
marker in k8s/roles/traefik/templates/dynamic.yaml.j2.

Checked on the RENDERED Middleware, the same way as the sibling
test_traefik_http_entrypoint_crowdsec.py, whose `_context` shape this file reuses.
"""

import pytest

from lib import yaml_fast
from lib.render_context import render_context

from lib.guard_jinja_env import register_ansible_filters
from lib.k8s_yaml import make_lookup
from lib.render_guard import make_env, render_or_error
from lib.repo_paths import K8S_ROLES, SHARED_TPL

_ROLE = "traefik"


def _context() -> dict:
    entries = render_context(K8S_ROLES, strict=True)["services"]
    entry = next(c for c in entries if c["name"] == _ROLE)
    return render_context(
        K8S_ROLES / _ROLE, overrides={"container_item": entry}, strict=True
    )


def _bouncer_config() -> dict:
    ctx = _context()
    env = make_env([K8S_ROLES / _ROLE / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, "dynamic.yaml.j2", ctx)
    assert rendered is not None, f"{_ROLE}/dynamic.yaml.j2 failed to render: {err}"
    bouncers = [
        doc["spec"]["plugin"]["bouncer"]
        for doc in yaml_fast.safe_load_all(rendered)
        if doc
        and doc.get("kind") == "Middleware"
        and "bouncer" in (doc.get("spec", {}).get("plugin") or {})
    ]
    # Non-vacuity: a guard that finds no bouncer Middleware would pass on nothing.
    assert len(bouncers) == 1, f"expected one bouncer Middleware, found {len(bouncers)}"
    return bouncers[0]


def metrics_ticker_problem(bouncer: dict) -> str | None:
    """None when the plugin starts no metrics ticker, else why it would start one."""
    if "metricsUpdateIntervalSeconds" not in bouncer:
        return (
            "metricsUpdateIntervalSeconds is unset, so the plugin's default of 600 starts the "
            "metrics ticker"
        )
    if bouncer["metricsUpdateIntervalSeconds"] != 0:
        return (
            f"metricsUpdateIntervalSeconds is {bouncer['metricsUpdateIntervalSeconds']!r}; any "
            f"value but 0 starts the metrics ticker"
        )
    return None


def test_the_rendered_bouncer_starts_no_metrics_ticker() -> None:
    bouncer = _bouncer_config()
    assert bouncer.get("crowdsecMode") == "stream", "the stall is a stream-mode defect"
    problem = metrics_ticker_problem(bouncer)
    assert problem is None, problem


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
