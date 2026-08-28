"""Every manifest the staging cluster deploys must have all its variables.

Staging's secrets file holds one key by design (docs/staging-cluster.md, Decision 5), so a
role added to `daniel-stage`'s `containers_list` can easily reference a credential that is
simply not there. Ansible does not fail on that — an undefined variable inside a `stringData`
value templates as an empty string or the literal `AnsibleUndefined`, and the Secret applies.
The workload then fails later, for a reason several steps removed from the missing variable.

Nothing else catches it. `validate_k8s_manifests` renders under daniel-box's variables, so it
never sees staging's overrides at all. `--dry-run` refuses `traefik` (`k8s_dry_run_unsupported`)
and would not check variable resolution anyway. The pre-deploy census this replaces was run by
hand on 2026-08-28 and is exactly the kind of check that stops being run.

The mechanism is a sentinel. Every name the templates could read that is NOT supplied by
group_vars/all.yml, the role's defaults, the host's own vars or staging's secrets file is given
a sentinel value; a sentinel in the output is a variable staging cannot supply. Rendering with
the host's flags applied is what makes this meaningful — the gated branches are the ones
holding the references staging lacks.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

_REPO = Path(__file__).resolve().parents[2]
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

_HOST = "daniel-stage"
_SENTINEL = "UNSUPPLIED-ON-STAGING"
_HOST_VARS = ANSIBLE / "inventory" / "host_vars" / f"{_HOST}.yml"
_STAGING_SECRETS = ANSIBLE / "vars" / "secrets-staging.yml"

# Names the render supplies itself — Jinja loop bindings, macro arguments, and the facts
# BASE_CONTEXT stands in for. A sentinel on any of these would report a false gap.
_SUPPLIED_BY_THE_RENDER = {
    "item",
    "container_item",
    "lookup",
    "playbook_dir",
    "hostvars",
    "inventory_hostname",
    "ansible_facts",
    "range",
    "loop",
}

_REFERENCE = re.compile(r"[a-z_][a-z0-9_]*")


def _staging_secret_keys() -> set[str]:
    """The key names in the SOPS file. Values are encrypted; the keys are plaintext."""
    return {
        m.group(1)
        for m in re.finditer(
            r"^([a-z_][a-z0-9_]*):", _STAGING_SECRETS.read_text(), re.M
        )
    } - {"sops"}


def _base_context() -> dict:
    base = {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        **load_yaml(_HOST_VARS),
        "playbook_dir": str(ANSIBLE),
    }
    return resolve_vars(base, base)


def _staging_roles() -> list[str]:
    entries = _base_context().get("containers_list") or []
    return [c["name"] for c in entries if c.get("platform") == "k8s"]


def _render(role: str, template: str, extra: dict) -> str:
    base = _base_context()
    entry = next(c for c in base["containers_list"] if c["name"] == role)
    # Role defaults FIRST: Ansible ranks host_vars above them, and a staging host exists to
    # override them. validate_k8s_manifests' own order is the other way round, which is
    # harmless only because prod overrides none of them.
    ctx = {**role_defaults(role, base), **base, **extra, "container_item": entry}
    env = make_env([K8S_ROLES / role / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, template, ctx)
    assert err is None, f"{role}/{template} failed to render for {_HOST}: {err}"
    return rendered


def _deployed_templates(role: str) -> list[str]:
    """The manifests the role actually applies, with the host's flags applied.

    Read from the role's own `manifests_files` / `manifests_secret_files`, because that list
    is where a per-cluster flag retires a manifest that has no template branch to gate — the
    ACME PVC, the CrowdSec Secret, the LiveSync gate. Scanning the templates directory
    instead would report those as gaps on every staging cluster, which is the opposite of
    what this file is for.
    """
    base = _base_context()
    entry = next(c for c in base["containers_list"] if c["name"] == role)
    ctx = {**role_defaults(role, base), **base, "container_item": entry}
    # The list expressions use Ansible's `| bool`, so the plain Jinja environment needs the
    # same filter registrations the manifest render uses.
    env = Environment(undefined=StrictUndefined)
    register_ansible_filters(env)

    names: list[str] = []
    for task in yaml.safe_load((K8S_ROLES / role / "tasks" / "main.yml").read_text()):
        include = (
            task.get("ansible.builtin.include_role") or task.get("include_role") or {}
        )
        if include.get("name") != "k8s/manifests":
            continue
        for key in ("manifests_files", "manifests_secret_files"):
            value = (task.get("vars") or {}).get(key)
            if isinstance(value, str):
                value = ast.literal_eval(env.from_string(value).render(ctx))
            names += value or []
    assert names, f"{role} deploys no manifests — the parse of its tasks file is wrong"
    return [f"{n}.j2" for n in dict.fromkeys(names)]


def _unsupplied_names(role: str) -> dict[str, str]:
    """Sentinel values for every name the role's templates read that staging cannot supply."""
    base = _base_context()
    supplied = (
        set(base)
        | set(role_defaults(role, base))
        | _staging_secret_keys()
        | _SUPPLIED_BY_THE_RENDER
    )
    names: set[str] = set()
    for tpl in (K8S_ROLES / role / "templates").glob("*.j2"):
        for expr in re.findall(r"\{\{(.*?)\}\}|\{%(.*?)%\}", tpl.read_text(), re.S):
            names |= set(_REFERENCE.findall("".join(expr)))
    return {n: _SENTINEL for n in names - supplied}


@pytest.mark.parametrize("role", _staging_roles())
def test_staging_renders_no_variable_it_cannot_supply(role: str) -> None:
    unsupplied = _unsupplied_names(role)
    for template in _deployed_templates(role):
        assert _SENTINEL not in _render(role, template, unsupplied), (
            f"{role}/{template} reads a variable {_HOST} cannot supply. Either gate the "
            f"branch that reads it, or add the key to {_STAGING_SECRETS.name}."
        )


@pytest.mark.parametrize("role", _staging_roles())
def test_the_sentinel_reaches_the_role_at_all(role: str) -> None:
    """The rejecting half: without it, a sentinel that stopped substituting passes silently.

    Feeding a name the role certainly reads must produce the sentinel. If this fails, the
    check above is inert and its green says nothing.
    """
    poisoned = {"k8s_namespace": _SENTINEL}
    hit = any(
        _SENTINEL in _render(role, template, poisoned)
        for template in _deployed_templates(role)
    )
    assert hit, (
        f"{role} never rendered a poisoned k8s_namespace — the check is not wired up"
    )


def test_staging_deploys_something() -> None:
    """An empty list would make every parametrised test above vacuous."""
    assert _staging_roles(), f"{_HOST} declares no k8s services, so nothing is checked"


def test_traefik_comes_first() -> None:
    """The play has no toposort — it runs containers_list in order, and Traefik installs the
    CRDs every later IngressRoute needs."""
    roles = _staging_roles()
    assert roles[0] == "traefik", (
        f"traefik must lead {_HOST}'s containers_list, got {roles}"
    )
