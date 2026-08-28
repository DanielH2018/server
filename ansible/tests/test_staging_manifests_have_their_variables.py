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
    # override them. validate_k8s_manifests' own order is the other way round; that is now held
    # harmless by `colliding_default_keys`, which fails the validator if any role default ever
    # shares a key with the inventory, rather than by nobody having done it yet.
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
    # Manifests the role renders to disk and applies by shell-out are just as much ours as the
    # ones k8s/manifests applies, and were covered by nothing until this was added.
    tasks = yaml.safe_load((K8S_ROLES / role / "tasks" / "main.yml").read_text())
    extra = locally_templated_applies(tasks)
    return list(dict.fromkeys([f"{n}.j2" for n in names] + extra))


# A task that puts an object into the cluster by some route other than `k8s/manifests`. The
# `assert names` above only catches a role that applies NOTHING through the counted path; a role
# that applies some manifests through it and the rest another way passes with partial coverage
# and no signal, which is the case these rules exist to make loud.
#
# Two such applies are not gaps, and the rule discriminates rather than allowlisting by name:
#   - a remote URL (traefik's pinned upstream CRDs) has no local template to check;
#   - a local path written by an earlier `template:` task in the same role IS ours, so its `src`
#     is folded into the corpus instead of being excused. That is how rbac.yaml.j2 — rendered to
#     /etc/rancher/k3s/traefik-clusterrole.yaml, then applied by shell-out — became covered.
_UNCOUNTED_APPLY_MODULES = ("kubernetes.core.k8s", "k8s")
_APPLY_SHELL = re.compile(r"kubectl\s+(?:apply|create|replace)\b[^|;]*?-f\s+(\S+)")


def _shell_text(task: dict) -> str:
    for key in ("ansible.builtin.command", "ansible.builtin.shell", "command", "shell"):
        value = task.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("cmd"), str):
            return value["cmd"]
    return ""


def _templated_dests(tasks) -> dict[str, str]:
    """dest -> src for every `template:` task in the role, so an apply of a rendered file can be
    traced back to the .j2 it came from."""
    dests = {}
    for task in _flatten_tasks(tasks):
        for key in ("ansible.builtin.template", "template"):
            spec = task.get(key)
            if isinstance(spec, dict) and spec.get("dest") and spec.get("src"):
                dests[str(spec["dest"])] = str(spec["src"])
    return dests


def _flatten_tasks(tasks) -> list:
    """Every task in a tasks file, including those nested in block/rescue/always."""
    out = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        out.append(task)
        for key in ("block", "rescue", "always"):
            out.extend(_flatten_tasks(task.get(key)))
    return out


def locally_templated_applies(tasks) -> list[str]:
    """The `.j2` sources of manifests this role applies by rendering to disk and shelling out.

    These are ours and are checkable, so they belong in the corpus rather than in an exemption
    list — an exemption would leave them exactly as uncovered as before, just quietly.
    """
    dests = _templated_dests(tasks)
    found = []
    for task in _flatten_tasks(tasks):
        match = _APPLY_SHELL.search(_shell_text(task))
        if match and match.group(1) in dests:
            found.append(dests[match.group(1)])
    return list(dict.fromkeys(found))


def uncounted_manifest_applications(tasks) -> list[str]:
    """Task names that apply cluster objects by a route no check in this file can see.

    `_deployed_templates` derives its corpus from the `manifests_files` of one
    `include_role: k8s/manifests`, so anything applied another way is invisible to every check
    built on it. An apply of a REMOTE url is not such a case (there is no local template to
    check) and neither is an apply of a file an earlier `template:` task rendered — that one is
    folded into the corpus by `locally_templated_applies`. Returns task names rather than a bool
    so the failure says which task to look at.
    """
    dests = _templated_dests(tasks)
    found = []
    for task in _flatten_tasks(tasks):
        name = task.get("name") or "<unnamed task>"
        if any(m in task for m in _UNCOUNTED_APPLY_MODULES):
            found.append(name)
            continue
        match = _APPLY_SHELL.search(_shell_text(task))
        if not match:
            continue
        target = match.group(1)
        if target.startswith(("http://", "https://")) or target in dests:
            continue
        found.append(name)
    return found


def _facts_set_by_the_role(role: str) -> set[str]:
    """Names the role's own tasks compute before rendering, so no cluster has to supply them.

    `authelia_password_hash` is the live case: the role reads it back from the cluster or mints
    it in a one-shot pod, then `set_fact`s it. Sentinelling it would report a gap that no
    secrets file can close — the value is deliberately not stored on either cluster.
    """
    tasks = yaml.safe_load((K8S_ROLES / role / "tasks" / "main.yml").read_text())
    names: set[str] = set()
    for task in _flatten_tasks(tasks):
        for key in ("ansible.builtin.set_fact", "set_fact"):
            spec = task.get(key)
            if isinstance(spec, dict):
                names |= set(spec) - {"cacheable"}
    return names


def _unsupplied_names(role: str) -> dict[str, str]:
    """Sentinel values for every name the role's templates read that staging cannot supply."""
    base = _base_context()
    supplied = (
        set(base)
        | set(role_defaults(role, base))
        | _staging_secret_keys()
        | _SUPPLIED_BY_THE_RENDER
        | _facts_set_by_the_role(role)
    )
    names: set[str] = set()
    # rglob, NOT glob: this repo's convention puts app config a manifest embeds via `lookup()`
    # one level down, in templates/config/ (the root CLAUDE.md says so, and eight roles have
    # one). A non-recursive glob never saw those files, so a name used only there got no
    # sentinel — and make_env defaults to StubUndefined rather than StrictUndefined, so it
    # rendered as the literal STUB and raised nothing. A missing credential passed silently.
    for tpl in (K8S_ROLES / role / "templates").rglob("*.j2"):
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


def _names_read_by_the_tasks(role: str) -> set[str]:
    """Every Jinja name the role's tasks file reads, minus the ones it produces itself.

    The corpus above scans `templates/`, so a credential read only from `tasks/main.yml` is
    invisible to it. `authelia_password` is the live case: the role pipes it into a one-shot
    pod to mint the argon2 hash, and no template mentions it — the check would have reported
    clean while the deploy failed on an undefined variable.
    """
    text = (K8S_ROLES / role / "tasks" / "main.yml").read_text()
    names: set[str] = set()
    for expr in re.findall(r"\{\{(.*?)\}\}|\{%(.*?)%\}", text, re.S):
        names |= set(_REFERENCE.findall("".join(expr)))
    tasks = yaml.safe_load(text)
    produced = _facts_set_by_the_role(role) | {
        str(task["register"]) for task in _flatten_tasks(tasks) if task.get("register")
    }
    # `vars:` blocks are the task's own scratch names, and `when:`/`failed_when:` read them.
    for task in _flatten_tasks(tasks):
        if isinstance(task.get("vars"), dict):
            produced |= set(task["vars"])
    return names - produced


@pytest.mark.parametrize("role", _staging_roles())
def test_staging_supplies_every_variable_the_tasks_file_reads(role: str) -> None:
    base = _base_context()
    supplied = (
        set(base)
        | set(role_defaults(role, base))
        | _staging_secret_keys()
        | _SUPPLIED_BY_THE_RENDER
    )
    # Filter to the role's own namespace plus bare credential names: a tasks file also reads
    # Ansible builtins, filter names and dotted attributes, none of which are variables staging
    # supplies, and none of which this check can tell apart by shape alone.
    missing = sorted(
        n
        for n in _names_read_by_the_tasks(role) - supplied
        if n.startswith(role.replace("-", "_"))
    )
    assert not missing, (
        f"{role}/tasks/main.yml reads {missing}, which {_HOST} cannot supply. Add the key to "
        f"{_STAGING_SECRETS.name}, or gate the task that reads it."
    )


def test_the_tasks_scan_rejects_a_name_no_cluster_supplies() -> None:
    """The rejecting half. A name the tasks file reads and nothing defines must be reported.

    Drives `_names_read_by_the_tasks` — the same function the check above drives — rather than
    asserting set arithmetic of its own, so a scan that stopped collecting fails here too.
    """
    assert "authelia_password" in _names_read_by_the_tasks("authelia"), (
        "authelia_password is no longer collected from the tasks file — the scan the check "
        "above depends on is not reaching it, and a credential read only from tasks/ is "
        "unchecked again."
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


# Named directly rather than parametrised over _staging_roles(). That list is [traefik] today
# and traefik has no templates/config/, so "add a role with a config template" would bind to
# nothing and the obvious red-proof would be vacuous. configarr's radarr_api_key is read ONLY
# from templates/config/config.yml.j2 — exactly the shape the non-recursive glob missed.
_CONFIG_DIR_ROLE = "configarr"
_CONFIG_DIR_NAME = "radarr_api_key"


def test_the_corpus_reaches_a_name_used_only_under_templates_config() -> None:
    """The rejecting half for the WIDENING, not for the check as a whole.

    Under the non-recursive glob this name was absent from the corpus, got no sentinel, and
    rendered as the literal STUB with no error — so the check reported clean on a template
    reading a credential staging does not have. Asserting the name is collected is what
    demonstrates the widening bites.
    """
    assert _CONFIG_DIR_NAME in _unsupplied_names(_CONFIG_DIR_ROLE), (
        f"{_CONFIG_DIR_NAME} is missing from {_CONFIG_DIR_ROLE}'s sentinel corpus — the "
        f"template scan is not reaching templates/config/, so any credential read only from "
        f"there is unchecked."
    )


def test_the_name_really_is_only_under_templates_config() -> None:
    """Pins the premise of the test above. If configarr moves that reference up into
    templates/, the widening test keeps passing while proving nothing."""
    top = sorted(
        t.name
        for t in (K8S_ROLES / _CONFIG_DIR_ROLE / "templates").glob("*.j2")
        if _CONFIG_DIR_NAME in t.read_text()
    )
    assert not top, (
        f"{_CONFIG_DIR_NAME} now also appears in {top}, so it no longer demonstrates the "
        f"templates/config/ gap — pick another name or another role."
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


# ── every manifest must reach the cluster through the path this file can see ───────────────────


@pytest.mark.parametrize("role", _staging_roles())
def test_the_role_applies_nothing_outside_the_counted_path(role: str) -> None:
    """The regression guard. Every check in this file is built on `_deployed_templates`, which
    reads only the `manifests_files` of `include_role: k8s/manifests` — so a manifest applied by
    `kubernetes.core.k8s` or a `kubectl apply` shell-out is covered by nothing and says so
    nowhere. Zero staging roles do this when the guard landed; the day one does, it fails here
    instead of quietly shrinking the corpus."""
    tasks = yaml.safe_load((K8S_ROLES / role / "tasks" / "main.yml").read_text())
    uncounted = uncounted_manifest_applications(tasks)
    assert not uncounted, (
        f"{role} applies cluster objects outside `include_role: k8s/manifests` in "
        f"{uncounted} — those manifests are invisible to _deployed_templates and to every "
        f"check in this file. Route them through k8s/manifests, or teach the helper about them."
    )


def test_an_uncounted_apply_is_flagged():
    """The rejecting half — the module form and the shell form, plus a task nested in a block."""
    tasks = [
        {"name": "counted", "include_role": {"name": "k8s/manifests"}},
        {"name": "module apply", "kubernetes.core.k8s": {"state": "present"}},
        {
            "name": "wrapper",
            "block": [
                {
                    "name": "shell apply",
                    "ansible.builtin.command": "kubectl apply -f /tmp/x.yaml",
                }
            ],
        },
    ]
    assert uncounted_manifest_applications(tasks) == ["module apply", "shell apply"]


def test_a_role_that_only_uses_the_counted_path_is_clean():
    """The accepting half. A guard that fired on everything would pass the test above too — and
    `kubectl rollout status`/`get` must NOT trip it, since read-only calls apply nothing."""
    tasks = [
        {"name": "apply", "include_role": {"name": "k8s/manifests"}},
        {"name": "wait", "ansible.builtin.command": "kubectl rollout status deploy/x"},
        {"name": "read", "ansible.builtin.shell": {"cmd": "kubectl get pods -o name"}},
    ]
    assert uncounted_manifest_applications(tasks) == []


def test_a_remote_url_apply_is_not_a_gap():
    """Traefik's pinned upstream CRDs. There is no local template behind a URL, so flagging it
    would be a finding no edit could ever clear."""
    tasks = [
        {
            "name": "crds",
            "ansible.builtin.command": {"cmd": "k3s kubectl apply -f https://x/y.yml"},
        }
    ]
    assert uncounted_manifest_applications(tasks) == []


def test_a_rendered_then_applied_template_is_covered_not_excused():
    """The real traefik shape: `template:` writes rbac.yaml.j2 to disk, a shell-out applies it.
    It must drop OUT of the uncounted list and INTO the corpus — an exemption alone would leave
    it as uncovered as before."""
    tasks = [
        {
            "name": "render",
            "ansible.builtin.template": {
                "src": "rbac.yaml.j2",
                "dest": "/etc/x/rbac.yaml",
            },
        },
        {
            "name": "apply",
            "ansible.builtin.command": {"cmd": "k3s kubectl apply -f /etc/x/rbac.yaml"},
        },
    ]
    assert uncounted_manifest_applications(tasks) == []
    assert locally_templated_applies(tasks) == ["rbac.yaml.j2"]


def test_traefik_rbac_is_in_the_real_corpus():
    """The regression guard for the instance that motivated this: rbac.yaml.j2 reaches the
    corpus, so the staging variable checks above now cover it."""
    assert "rbac.yaml.j2" in _deployed_templates("traefik")
