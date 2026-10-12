"""The workload-service override table names exactly the rendered workloads with no tag label.

observability's `files/rules/workload-service.yaml` resolves a Deployment's or a DaemonSet's
deploy tag from its `homelab/role` label first, and falls back to a literal override table for
the workloads that carry none. A workload the table misses lands in `service="unmapped"`, and
an entry for a workload that is gone or labelled is dead text that still looks like coverage.

So the table is derived here, not trusted. Every k8s role renders through
`_k8s_render.rendered_docs`, and the labelling rule `k8s/manifests` applies decides which
Deployments and DaemonSets carry the label: an armed role's files get it through
`ansible/templates/role-labelled.yaml.j2`, and its deferred files and an unarmed role's files
do not. The table must map exactly the rest, each to its own role.

Run: uv run pytest ansible/tests/k8s/test_workload_service_overrides.py
"""

import re

from _helpers import load_tasks, walk_tasks
from _k8s_render import rendered_docs
from _role_census import role_dirs
from lib import yaml_fast
from lib.k8s_roles import declared_manifest_files
from lib.repo_paths import K8S_ROLES
from role_label import ROLE_LABEL

RULES = K8S_ROLES / "observability" / "files" / "rules" / "workload-service.yaml"
MANIFESTS_DEFAULTS = K8S_ROLES / "manifests" / "defaults" / "main.yml"
WORKLOAD_KINDS = {"Deployment": "deployment", "DaemonSet": "daemonset"}

# One override entry, in the form the rule file writes each one.
OVERRIDE = re.compile(
    r"label_replace\(kube_(deployment|daemonset)_status_\w+"
    r'\{namespace="([^"]+)", (?:deployment|daemonset)="([^"]+)"\}, '
    r'"service", "([^"]+)", "", ""\)'
)

# authelia and nut are unarmed by operator decision; pihole-2 is pihole's deferred file. Each
# reaches the table by a different path, so a derivation that lost one would drop its member.
MUST_FIND = frozenset(
    {
        ("deployment", "homelab", "authelia"),
        ("deployment", "homelab", "nut"),
        ("deployment", "homelab", "pihole-2"),
    }
)


def _armed_roles() -> set[str]:
    """The roles whose `k8s/manifests` include arms the prune, and so renders the label."""
    default = yaml_fast.safe_load(MANIFESTS_DEFAULTS.read_text())["manifests_prune"]
    armed = set()
    for role in role_dirs():
        main = role / "tasks" / "main.yml"
        if not main.is_file():
            continue
        for task in walk_tasks(load_tasks(main)):
            include = task.get("ansible.builtin.include_role") or {}
            if include.get("name") != "k8s/manifests" or "tasks_from" in include:
                continue
            if (task.get("vars") or {}).get("manifests_prune", default) is True:
                armed.add(role.name)
    return armed


def unlabelled_workloads(docs, armed: set[str]) -> dict[tuple[str, str, str], str]:
    """Each rendered Deployment and DaemonSet that reaches the cluster with no tag label.

    Returns:
        `(kind, namespace, name)` keys, with the kind lowercased as kube-state-metrics labels
        it, mapped to the role that renders the workload.
    """
    found = {}
    for role, tpl, doc in docs:
        kind = WORKLOAD_KINDS.get(doc.get("kind"))
        if kind is None:
            continue
        meta = doc["metadata"]
        stamped = role in armed and tpl.removesuffix(".j2") in declared_manifest_files(
            role
        )
        if stamped or ROLE_LABEL in (meta.get("labels") or {}):
            continue
        found[(kind, meta["namespace"], meta["name"])] = role
    return found


def override_table(text: str) -> dict[tuple[str, str, str], str]:
    """The override entries in the rule file's text, as `(kind, namespace, name)` to service."""
    return {
        (kind, namespace, name): service
        for kind, namespace, name, service in OVERRIDE.findall(text)
    }


def override_problems(table: dict, unlabelled: dict) -> list[str]:
    """A problem for each workload the table and the render disagree on."""
    problems = [
        f"{kind}/{namespace}/{name} renders from role {role} with no {ROLE_LABEL} label "
        f'and has no override entry, so it maps to service="unmapped". Add a label_replace '
        f'mapping it to "{role}" in {RULES.name}.'
        for (kind, namespace, name), role in sorted(unlabelled.items())
        if (kind, namespace, name) not in table
    ]
    problems += [
        f"{kind}/{namespace}/{name}: the table maps it to {table[key]!r}, and role "
        f"{unlabelled[key]!r} renders it"
        for key in sorted(table.keys() & unlabelled.keys())
        if table[key] != unlabelled[key]
        for kind, namespace, name in [key]
    ]
    problems += [
        f"{kind}/{namespace}/{name}: the table maps it, and no rendered workload without "
        f"the {ROLE_LABEL} label has that name. Remove the entry."
        for kind, namespace, name in sorted(table.keys() - unlabelled.keys())
    ]
    return problems


def test_the_override_table_is_clean_on_the_render():
    unlabelled = unlabelled_workloads(rendered_docs(), _armed_roles())
    missing = MUST_FIND - unlabelled.keys()
    assert not missing, (
        f"the derivation no longer finds {sorted(missing)}, so it is checking less than the "
        "table it guards"
    )
    table = override_table(RULES.read_text())
    problems = override_problems(table, unlabelled)
    assert not problems, "\n".join(problems)


def test_a_rendered_workload_missing_from_the_table_is_flagged():
    unlabelled = {
        ("deployment", "homelab", "authelia"): "authelia",
        ("deployment", "homelab", "newcomer"): "newcomer",
    }
    table = {("deployment", "homelab", "authelia"): "authelia"}
    problems = override_problems(table, unlabelled)
    assert len(problems) == 1 and "deployment/homelab/newcomer" in problems[0], problems


def test_an_entry_naming_the_wrong_role_or_no_workload_is_flagged():
    unlabelled = {("deployment", "homelab", "pihole-2"): "pihole"}
    table = {
        ("deployment", "homelab", "pihole-2"): "pihole-2",
        ("deployment", "homelab", "retired"): "retired",
    }
    problems = override_problems(table, unlabelled)
    assert any("pihole-2: the table maps it to 'pihole-2'" in p for p in problems), (
        problems
    )
    assert any(
        "deployment/homelab/retired: the table maps it" in p for p in problems
    ), problems


def test_the_table_parser_reads_every_entry_form_the_rule_file_writes():
    text = (
        'label_replace(kube_deployment_status_replicas_unavailable{namespace="homelab", '
        'deployment="nut"}, "service", "nut", "", "")\n'
        'label_replace(kube_daemonset_status_number_unavailable{namespace="homelab", '
        'daemonset="agent"}, "service", "agent", "", "")\n'
        'label_replace(kube_deployment_status_replicas_unavailable{namespace=~"kube-system"}, '
        '"service", "k3s", "", "")'
    )
    assert override_table(text) == {
        ("deployment", "homelab", "nut"): "nut",
        ("daemonset", "homelab", "agent"): "agent",
    }
