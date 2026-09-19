"""wg-easy's compose healthcheck widens the image probe's timing without replacing the probe.

The image ships `wg show | grep -q interface` as its HEALTHCHECK, and the role's CLAUDE.md
forbids a redundant `test:`. Its Dockerfile puts the timing flags inside the CMD string, so
`Config.Healthcheck` carried none and the container ran Docker's 30s/30s/3 defaults while
#1910 widened every other Pi probe to the host's `container_healthcheck_*` values. A
`healthcheck:` with timing and NO `test:` closes that: dockerd fills `Test` from the image
(`merge()` in daemon/commit.go, verified on daniel-pi's Docker 29.5.3 + Compose v5.1.4 on
2026-09-17). This pins both halves — the timing reads the host vars, and no `test:` appears —
because either drift is silent: a `test:` would duplicate the probe the rule forbids, and a
hardcoded timing would put wg-easy back outside the tunable the other Pi probes read (#1921).

Run: uv run pytest ansible/tests/services/test_wg_easy_healthcheck_timing.py
"""

import pytest

from _helpers import ANSIBLE, load_yaml
from lib import yaml_fast
from lib.render_guard import ALL_VARS, BASE_CONTEXT, HOST_VARS, render_or_error
from validate import compose_templates as vct

PI_HOST_VARS = HOST_VARS / "daniel-pi.yml"
TEMPLATE = (
    ANSIBLE / "roles" / "containers" / "wg-easy" / "templates" / "docker-compose.yml.j2"
)

TIMING_KEYS = ("interval", "timeout", "retries")


def _render(host_vars: dict) -> dict:
    """The wg-easy service as the Pi deploys it, with `host_vars` layered over the base."""
    entry = next(c for c in host_vars["containers_list"] if c["name"] == "wg-easy")
    ctx = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **host_vars, "container_item": entry}
    ctx.pop("containers_list", None)
    rendered, err = render_or_error(vct.build_env("wg-easy"), TEMPLATE.name, ctx)
    assert rendered is not None, err
    return yaml_fast.safe_load(rendered)["services"]["wg-easy"]


@pytest.fixture(scope="module")
def pi_vars() -> dict:
    return load_yaml(PI_HOST_VARS)


def test_timing_comes_from_the_pi_host_vars(pi_vars: dict) -> None:
    """ACCEPT: the block reads the same three tunables docker-proxy reads."""
    hc = _render(pi_vars)["healthcheck"]
    expected = {k: pi_vars[f"container_healthcheck_{k}"] for k in TIMING_KEYS}
    assert {k: hc[k] for k in TIMING_KEYS} == expected, hc


def test_the_image_probe_is_not_restated(pi_vars: dict) -> None:
    """REJECT a `test:` — that is the redundant probe the role's CLAUDE.md rule forbids."""
    hc = _render(pi_vars)["healthcheck"]
    assert "test" not in hc, f"wg-easy restates the image probe: {hc['test']!r}"
    assert set(hc) == set(TIMING_KEYS), f"unexpected healthcheck keys: {sorted(hc)}"


def test_the_pi_widens_past_dockers_defaults(pi_vars: dict) -> None:
    """The point of the block: the Pi's values must exceed the 30s/3 the image ran under."""
    assert int(pi_vars["container_healthcheck_timeout"].rstrip("s")) > 30
    assert int(pi_vars["container_healthcheck_retries"]) > 3


def test_a_host_without_the_tunables_still_renders(pi_vars: dict) -> None:
    """A host that leaves the vars unset gets Docker's own defaults, not a render error."""
    bare = {
        k: v for k, v in pi_vars.items() if not k.startswith("container_healthcheck_")
    }
    hc = _render(bare)["healthcheck"]
    assert hc == {"interval": "30s", "timeout": "30s", "retries": 3}, hc
