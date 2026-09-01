"""Traefik's two namespace lists must agree, and must be a list.

`rbac.yaml.j2` renders one namespaced Role per watched namespace; `static-config.yaml.j2`
lists the same set as `providers.kubernetesCRD.namespaces`. Both now read one variable,
`traefik_k8s_watched_namespaces`, so they cannot drift — this file is what holds that true
once someone wraps one side in a filter or reintroduces a literal list.

The agreement is checked on the RENDERED manifests, not on the templates' text. A test that
greps both templates for the same variable name passes the moment one side changes shape
while still naming it, which is the indirection trap `textual-guard-checks-break-on-indirection`
records. Comparing Role `metadata.namespace` values against the provider list compares the
thing that actually has to match.

Why the two failure modes are asymmetric, and why the list is per-cluster:

- A namespace Traefik watches but has no Role in fails SILENTLY. Its informers never sync,
  so an IngressRoute there applies cleanly, reports nothing wrong, and the host 404s.
- A Role for a namespace that does not exist fails LOUDLY, at apply time. That is how this
  surfaced: the first staging Traefik deploy aborted with
  `namespaces "observability" not found`, because claude-otel is not in that cluster's
  subset and nothing else creates the namespace.

Neither is something a render can see on its own, which is the point of checking the pair.
"""

from __future__ import annotations

import sys

import pytest
import yaml
from _helpers import REPO

_REPO = REPO
sys.path.insert(0, str(_REPO / "scripts"))

from validate_k8s_manifests import (  # noqa: E402 — needs the path insert above
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
_HOSTS = ("daniel-box", "daniel-stage")


def _context(host: str) -> dict:
    host_vars = ANSIBLE / "inventory" / "host_vars" / f"{host}.yml"
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **load_yaml(host_vars)}
    base["playbook_dir"] = str(ANSIBLE)
    base = resolve_vars(base, base)
    entry = next(c for c in base["containers_list"] if c["name"] == _ROLE)
    # Role defaults FIRST: Ansible ranks host_vars above them, and a staging host exists to
    # override them. See test_staging_manifests_have_their_variables for the same ordering.
    return {**role_defaults(_ROLE, base), **base, "container_item": entry}


def _render(host: str, template: str) -> str:
    ctx = _context(host)
    env = make_env([K8S_ROLES / _ROLE / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, template, ctx)
    assert err is None, f"{_ROLE}/{template} failed to render for {host}: {err}"
    return rendered


def _role_namespaces(host: str) -> list[str]:
    """Every namespace rbac.yaml.j2 renders a Role for, in order."""
    docs = yaml.safe_load_all(_render(host, "rbac.yaml.j2"))
    return [
        d["metadata"]["namespace"]
        for d in docs
        if isinstance(d, dict) and d.get("kind") == "Role"
    ]


def _provider_namespaces(host: str) -> list[str]:
    """The provider list, read out of the ConfigMap's embedded Traefik config.

    static-config.yaml.j2 is a ConfigMap whose data value is a block scalar, so the config is
    a STRING at the manifest level and has to be parsed a second time. Parsing the manifest
    alone compares comments, not configuration.
    """
    doc = yaml.safe_load(_render(host, "static-config.yaml.j2"))
    config = yaml.safe_load(doc["data"]["traefik.yml"])
    return config["providers"]["kubernetesCRD"]["namespaces"]


def disagreements(watched: list[str], granted: list[str]) -> list[str]:
    """The comparison itself, taking its two lists as arguments so the rejecting test below
    can drive it directly. Returns one message per way the pair disagrees; empty means they
    match."""
    unwatched = set(watched) - set(granted)
    ungranted = set(granted) - set(watched)
    out = []
    if unwatched:
        out.append(
            f"Traefik watches {sorted(unwatched)} but rbac.yaml.j2 renders no Role there — "
            f"its IngressRoutes will 404 with nothing reported wrong."
        )
    if ungranted:
        out.append(
            f"rbac.yaml.j2 renders a Role in {sorted(ungranted)}, which Traefik does not "
            f"watch — an unused grant, and an apply failure if the namespace is absent."
        )
    return out


@pytest.mark.parametrize("host", _HOSTS)
def test_the_two_namespace_lists_agree(host: str) -> None:
    """Both halves at once, on the rendered manifests."""
    problems = disagreements(_provider_namespaces(host), _role_namespaces(host))
    assert not problems, f"{host}: " + " ".join(problems)


@pytest.mark.parametrize("host", _HOSTS)
def test_the_variable_renders_as_a_list_not_a_string(host: str) -> None:
    """A single-string default would iterate CHARACTERS, one Role per letter.

    `{% for ns in traefik_k8s_watched_namespaces %}` is happy to walk a string, so a default
    written as `"{{ [a, b, c] }}"` renders plausibly-shaped YAML with dozens of one-letter
    namespaces. Counting the Roles is what distinguishes the two; eyeballing the render does
    not.
    """
    namespaces = _role_namespaces(host)
    assert namespaces, f"{host} renders no Roles at all"
    assert len(namespaces) == len(set(namespaces)), (
        f"{host} renders duplicate Role namespaces: {namespaces}"
    )
    assert all(len(ns) > 1 for ns in namespaces), (
        f"{host}: single-character namespaces mean the variable is a string being iterated "
        f"character by character, not a list: {namespaces}"
    )


def test_prod_still_watches_the_three_namespaces() -> None:
    """Pins the prod set, so narrowing it needs an explicit edit here."""
    assert _provider_namespaces("daniel-box") == [
        "homelab",
        "observability",
        "longhorn-system",
    ]


def test_staging_does_not_watch_observability() -> None:
    """The cluster has no claude-otel to create it; naming it aborts the RBAC apply."""
    assert "observability" not in _provider_namespaces("daniel-stage")
    assert "observability" not in _role_namespaces("daniel-stage")


def test_the_comparison_accepts_a_matching_pair() -> None:
    """The accepting half, so a comparison that flagged everything would fail here."""
    assert (
        disagreements(["homelab", "longhorn-system"], ["longhorn-system", "homelab"])
        == []
    )


@pytest.mark.parametrize(
    ("watched", "granted", "expect"),
    [
        (["homelab", "observability"], ["homelab"], "renders no Role there"),
        (["homelab"], ["homelab", "observability"], "does not watch"),
    ],
)
def test_the_comparison_rejects_a_mismatched_pair(
    watched: list[str], granted: list[str], expect: str
) -> None:
    """The rejecting half, driving the SAME function the real test drives.

    A set difference against a list that had silently stopped being read compares two empty
    sets and passes, so the passing side alone cannot tell a working comparison from an inert
    one. Each direction is asserted separately: a check that caught only the loud failure
    would still miss the silent one, which is the expensive one.
    """
    problems = disagreements(watched, granted)
    assert len(problems) == 1
    assert expect in problems[0]
