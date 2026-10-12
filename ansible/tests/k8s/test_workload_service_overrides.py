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

# The rule that maps each kind, by its recorded name.
RECORDS = {
    "homelab:deployment_service:info": "deployment",
    "homelab:daemonset_service:info": "daemonset",
}

# One override entry, in the form the rule file writes each one. The workload label must name
# the kind its metric does, so `kube_deployment_...{daemonset="x"}` is not an entry.
OVERRIDE = re.compile(
    r"label_replace\(kube_(deployment|daemonset)_status_\w+"
    r'\{namespace="([^"]+)", \1="([^"]+)"\}, '
    r'"service", "([^"]+)", "", ""\)'
)

# The resolution steps of one rule, which `or on(namespace, <kind>)` joins in order.
STEP_SEPARATOR = re.compile(r"\bor\s+on\s*\(\s*namespace\s*,\s*\w+\s*\)")

# A step that maps every workload it selects, so no entry after it is ever reached.
FALLBACK = re.compile(r'"service", "(k3s|unmapped)"')
PROMQL_COMMENT = re.compile(r"#.*")

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


def override_table(doc: dict) -> tuple[dict[tuple[str, str, str], str], list[str]]:
    """The override entries Prometheus actually applies, and a problem for each it never does.

    Reads each mapping rule's `expr` in step order, the order `or` resolves in, so an entry
    in a YAML or PromQL comment is not an entry at all. An entry Prometheus never applies is
    a problem rather than a table row: one in the other kind's rule, which that rule's
    trailing `and on(namespace, <kind>)` drops; one after a `k3s` or `unmapped` fallback
    step, which has already mapped the workload; and a second entry for a workload, because
    `or` keeps the first.

    Args:
        doc: The parsed rule file.

    Returns:
        `(kind, namespace, name)` to service for each entry Prometheus applies, and the
        problems.
    """
    table: dict[tuple[str, str, str], str] = {}
    problems = []
    rules = [
        rule
        for group in doc.get("groups") or []
        for rule in group.get("rules") or []
        if rule.get("record") in RECORDS
    ]
    if {rule["record"] for rule in rules} != set(RECORDS):
        problems.append(
            f"{RULES.name} no longer records all of {sorted(RECORDS)}, so this guard reads "
            "a table that is not the one Prometheus evaluates"
        )
    for rule in rules:
        rule_kind = RECORDS[rule["record"]]
        expr = PROMQL_COMMENT.sub("", str(rule.get("expr", "")))
        fallback = None
        for step in STEP_SEPARATOR.split(expr):
            for kind, namespace, name, service in OVERRIDE.findall(step):
                key = (kind, namespace, name)
                where = f"{rule['record']}: the entry for {kind}/{namespace}/{name}"
                if kind != rule_kind:
                    problems.append(
                        f"{where} sits in the {rule_kind} rule, whose "
                        f"`and on(namespace, {rule_kind})` drops it"
                    )
                elif fallback is not None:
                    problems.append(
                        f"{where} comes after the {fallback!r} fallback step, which has "
                        "already mapped it"
                    )
                elif key in table:
                    problems.append(
                        f"{where} appears twice; `or` keeps the first, {table[key]!r}"
                    )
                else:
                    table[key] = service
            found = FALLBACK.search(step)
            if found and fallback is None:
                fallback = found.group(1)
    return table, problems


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
    table, problems = override_table(yaml_fast.safe_load(RULES.read_text()))
    problems += override_problems(table, unlabelled)
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


def _entry(kind: str, name: str, service: str) -> str:
    metric = {
        "deployment": "kube_deployment_status_replicas_unavailable",
        "daemonset": "kube_daemonset_status_number_unavailable",
    }[kind]
    return (
        f'label_replace({metric}{{namespace="homelab", {kind}="{name}"}}, '
        f'"service", "{service}", "", "")'
    )


def _rules(
    deployment_steps: list[str], daemonset_steps: list[str] | None = None
) -> dict:
    """A rule file whose two mapping rules join the given steps the way the real one does."""

    def rule(record: str, kind: str, steps) -> dict:
        joined = f"\n  or on(namespace, {kind})\n  ".join(steps or ["vector(1)"])
        return {
            "record": record,
            "expr": f"group by (namespace, {kind}, service) ({joined})",
        }

    return {
        "groups": [
            {
                "name": "workload-service",
                "rules": [
                    rule(
                        "homelab:deployment_service:info",
                        "deployment",
                        deployment_steps,
                    ),
                    rule(
                        "homelab:daemonset_service:info",
                        "daemonset",
                        daemonset_steps or [],
                    ),
                ],
            }
        ]
    }


UNMAPPED_STEP = 'label_replace(kube_deployment_status_replicas_unavailable, "service", "unmapped", "", "")'
K3S_STEP = (
    'label_replace(kube_deployment_status_replicas_unavailable{namespace=~"kube-system"}, '
    '"service", "k3s", "", "")'
)


def test_the_entries_ahead_of_the_fallbacks_are_clean():
    doc = _rules(
        [_entry("deployment", "nut", "nut"), K3S_STEP, UNMAPPED_STEP],
        [_entry("daemonset", "agent", "agent")],
    )
    assert override_table(doc) == (
        {
            ("deployment", "homelab", "nut"): "nut",
            ("daemonset", "homelab", "agent"): "agent",
        },
        [],
    )


def test_a_second_entry_for_a_workload_is_flagged_and_the_first_wins():
    doc = _rules(
        [
            _entry("deployment", "nut", "wrong"),
            _entry("deployment", "nut", "nut"),
            UNMAPPED_STEP,
        ]
    )
    table, problems = override_table(doc)
    assert table == {("deployment", "homelab", "nut"): "wrong"}
    assert len(problems) == 1 and "appears twice" in problems[0], problems


def test_an_entry_after_a_fallback_step_is_flagged():
    for fallback in (K3S_STEP, UNMAPPED_STEP):
        table, problems = override_table(
            _rules([fallback, _entry("deployment", "nut", "nut")])
        )
        assert table == {}
        assert len(problems) == 1 and "fallback step" in problems[0], problems


def test_an_entry_in_the_other_kinds_rule_is_flagged():
    table, problems = override_table(
        _rules([_entry("daemonset", "agent", "agent"), UNMAPPED_STEP])
    )
    assert table == {}
    assert len(problems) == 1 and "drops it" in problems[0], problems


def test_an_entry_in_a_promql_comment_is_not_read():
    commented = "# " + _entry("deployment", "nut", "nut") + "\n"
    table, problems = override_table(_rules([commented + UNMAPPED_STEP]))
    assert (table, problems) == ({}, [])


def test_a_rule_file_missing_a_mapping_rule_is_flagged():
    doc = _rules([UNMAPPED_STEP])
    doc["groups"][0]["rules"].pop()
    assert any("no longer records" in p for p in override_table(doc)[1])
