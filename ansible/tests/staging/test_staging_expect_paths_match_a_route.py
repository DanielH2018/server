"""Every `staging_expect` path must agree with the route that has to answer it.

THE FAILURE THIS CLOSES (issue #1662). `daniel-stage`'s traefik entry declared `path: /`,
`status: 302` — the dashboard's route matches `PathPrefix(`/dashboard`)`, so `/` matches no
route and Traefik answers 404 before any middleware runs. The expectation asked for a status
its route cannot produce, and it sat that way from 2026-08-28 until a hand probe found it.

Nothing caught it. `staging_expectations.py` is invoked by the GitOps staging gate, `STAGING_GATE`
is off by default, and `deploy.sh` runs no expectation check at all — so the first cost of a stale
expectation is the first armed tick failing, which is when a gate is most likely to be turned back
off again.

WHAT THIS ASSERTS, and what it deliberately does not. It reads each service's rendered
IngressRoutes and compares the declared path against the route's own matchers:

  - a non-404 expectation must be admitted by some route on that hostname. Which status that
    route then answers — 200, or a 302 from the Authelia middleware — is a live fact, and only
    `staging_expect_remote.sh` can measure it.
  - a 404 expectation must be admitted by NO route. That is the half the value-only fix leaves
    unguarded: `traefik /` and `traefik /api/http/routers` assert what is deliberately unrouted
    (see the `# DECIDED: /api is deliberately NOT routed` marker in the traefik role's
    dashboard-ingressroute.yaml.j2), and both would go quietly green if a route grew to cover them.

So this is a render-time guard against an IMPOSSIBLE expectation, not a substitute for measuring.
"""

import ast
import re
import sys

import pytest
from _helpers import REPO
from jinja2 import Environment, StrictUndefined

sys.path.insert(0, str(REPO / "scripts"))

from deploy_tools.staging_expectations import (
    host_context,
    staging_entries,
)
from lib import yaml_fast

from validate.k8s_manifests import (
    K8S_ROLES,
    SHARED_TPL,
    make_env,
    make_lookup,
    register_ansible_filters,
    render_or_error,
    role_defaults,
)

# The census must contain these, or the guard is passing over an empty set — the vacuity failure
# nine guards in this repo shipped with (repo-root CLAUDE.md, "A check that finds its own subject
# by pattern"). Named members rather than a count, so the failure says which one went missing.
_MUST_BE_COVERED = frozenset({"traefik", "freshrss"})

_HOST_MATCHER = re.compile(r"Host\(`([^`]+)`\)")
_PATH_MATCHER = re.compile(r"(PathPrefix|Path)\(`([^`]+)`\)")


def _ingressroute_templates(role: str, ctx: dict) -> list[str]:
    """The IngressRoute manifests this role deploys, with staging's flags applied.

    Read from the role's own `manifests_files` rather than its templates directory, for the same
    reason `staging_expectations.routable_services()` does: a per-cluster flag can retire a route
    whose template is still on disk.
    """
    env = Environment(undefined=StrictUndefined)
    register_ansible_filters(env)
    names: list[str] = []
    for task in yaml_fast.safe_load(
        (K8S_ROLES / role / "tasks" / "main.yml").read_text()
    ):
        include = (
            task.get("ansible.builtin.include_role") or task.get("include_role") or {}
        )
        if include.get("name") != "k8s/manifests":
            continue
        value = (task.get("vars") or {}).get("manifests_files")
        if isinstance(value, str):
            value = ast.literal_eval(env.from_string(value).render(ctx))
        names += value or []
    return [f"{n}.j2" for n in names if "ingressroute" in n]


def _routes(role: str) -> list[tuple[str, str]]:
    """(host, match) for every IngressRoute rule this role deploys to staging.

    Secrets are stubbed by `make_env`, so the domain is a stand-in — callers compare the
    `<hostname>.local.` prefix rather than the whole name.
    """
    base = host_context()
    entry = next(c for c in base["containers_list"] if c["name"] == role)
    ctx = {**role_defaults(role, base), **base, "container_item": entry}
    env = make_env([K8S_ROLES / role / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)

    out: list[tuple[str, str]] = []
    for template in _ingressroute_templates(role, ctx):
        rendered, err = render_or_error(env, template, ctx)
        assert rendered is not None, f"{role}/{template} failed to render: {err}"
        for doc in yaml_fast.safe_load_all(rendered):
            if not doc or doc.get("kind") != "IngressRoute":
                continue
            for route in doc["spec"].get("routes") or []:
                match = route.get("match", "")
                for host in _HOST_MATCHER.findall(match):
                    out.append((host, match))
    return out


def _admits(match: str, path: str) -> bool:
    """Whether a route's matcher admits `path`. A rule with no path matcher admits everything."""
    matchers = _PATH_MATCHER.findall(match)
    if not matchers:
        return True
    for kind, value in matchers:
        if kind == "Path" and path == value:
            return True
        if kind == "PathPrefix" and path.startswith(value):
            return True
    return False


def _expectations() -> list[tuple[str, str, str, int]]:
    return [
        (entry["name"], want["hostname"], want["path"], int(want["status"]))
        for entry in staging_entries()
        for want in entry.get("staging_expect") or []
    ]


def test_the_census_covers_the_services_whose_routes_this_guards():
    """Non-vacuity. An empty census makes every assertion below pass over nothing."""
    covered = {service for service, _, _, _ in _expectations()}
    assert _MUST_BE_COVERED <= covered, (
        f"{sorted(_MUST_BE_COVERED - covered)} declare no staging_expect — this guard is "
        "checking less than it was written to check"
    )


@pytest.mark.parametrize(
    "service,hostname,path,status",
    _expectations(),
    ids=lambda v: str(v).replace("/", "_"),
)
def test_each_expectation_agrees_with_the_route_that_answers_it(
    service, hostname, path, status
):
    routes = [
        (host, match)
        for host, match in _routes(service)
        if host.startswith(f"{hostname}.local.")
    ]
    admitting = [match for _, match in routes if _admits(match, path)]

    if status == 404:
        assert not admitting, (
            f"{service}: {hostname}{path} expects 404, but a route admits it: {admitting[0]} "
            "— either the route grew or the expectation is now asserting the wrong thing"
        )
    else:
        assert admitting, (
            f"{service}: {hostname}{path} expects {status}, but no route on that host admits "
            f"the path. Rules on {hostname}: {[m for _, m in routes] or 'none'}"
        )
