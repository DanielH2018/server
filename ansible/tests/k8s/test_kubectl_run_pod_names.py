"""Every pod a k8s role's task creates with `kubectl run` gets a per-run name.

`kubectl run <name>` refuses a name that already exists, and the task then fails after
everything before it succeeded. Two cases leave a pod by that name behind. One is an
overlapping run of the same task: the 2026-10-12 landing of #4552 failed this way on
qbittorrent's reach probe. The other is a pod orphaned when the Ansible controller died while
the pod ran. `--rm` cannot clean that one up, because the kubectl client that deletes the pod
died with the controller, and the read-only ServiceAccount cannot delete it (#4556, #4566).

Each command is templated through Ansible twice, as two runs would template it. The two pod
names must differ, and the pod's labels must not: a NetworkPolicy admits a probe by its `app`
label, so a label that varies per run stops matching.

Run: uv run pytest ansible/tests/k8s/test_kubectl_run_pod_names.py
"""

import re

import pytest
from ansible.template import Templar, trust_as_template
from _helpers import K8S_ROLES, load_defaults, load_tasks, walk_tasks
from _role_census import task_files_by_role

# `kubectl run`, optionally after `-n <namespace>`, where the namespace may be a `{{ }}`
# expression with spaces in it. A run spelled another way (`--context`, flags first) escapes
# this pattern, and KNOWN_RUNS is what catches the pattern no longer matching the known tasks.
_KUBECTL_RUN = re.compile(
    r"\bkubectl\s+(?:(?:-n|--namespace)\s+(?:\{\{[^}]*\}\}|\S+)\s+)?run\s+([a-z0-9-]*[a-z0-9])"
)

# Non-vacuity, by (role, the pod name's fixed prefix). The census globs for its subjects, so a
# rename or a reworded command would empty it and every check below would pass on nothing.
KNOWN_RUNS = frozenset(
    {
        ("qbittorrent", "qbittorrent-reach-probe"),
        ("authelia", "authelia-hash"),
        ("authelia", "authelia-hash-claude"),
        ("authelia", "authelia-hash-agent"),
    }
)

# A DNS-1123 label. Kubernetes derives the pod's hostname from its name, so this is the
# stricter of the two shapes a pod name can take.
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _census() -> dict[tuple[str, str], str]:
    """{(role, fixed name prefix): untemplated cmd} for every `kubectl run` task."""
    found = {}
    for role, path in task_files_by_role(K8S_ROLES):
        for task in walk_tasks(load_tasks(path)):
            cmd = (task.get("ansible.builtin.command") or {}).get("cmd", "")
            if isinstance(cmd, str) and (match := _KUBECTL_RUN.search(cmd)):
                found[(role.name, match.group(1))] = cmd
    return found


CENSUS = _census()


def _render(role: str, cmd: str) -> str:
    """The command templated through Ansible as one deploy would template it."""
    templar = Templar(
        loader=None,
        variables={
            **load_defaults(K8S_ROLES / role),
            "k8s_namespace": "homelab",
            "container_item": {"port": 8080},
        },
    )
    return str(templar.template(trust_as_template(cmd)))


def _pod_name(rendered: str) -> str:
    match = re.search(r"\brun\s+(\S+)", rendered)
    assert match, f"no `kubectl run <name>` in {rendered!r}"
    return match.group(1)


def _labels(rendered: str) -> str | None:
    match = re.search(r"--labels[=\s]+(\S+)", rendered)
    return match and match.group(1)


def test_the_census_finds_every_known_kubectl_run():
    assert KNOWN_RUNS <= CENSUS.keys(), sorted(KNOWN_RUNS - CENSUS.keys())


@pytest.mark.parametrize("key", sorted(CENSUS), ids="/".join)
def test_each_run_names_its_pod_afresh(key):
    first, second = (_pod_name(_render(key[0], CENSUS[key])) for _ in range(2))
    assert first != second, (
        f"two runs both named the pod {first!r}, so the second run fails AlreadyExists "
        "against the first run's pod, or against a pod a dead controller left behind"
    )


@pytest.mark.parametrize("key", sorted(CENSUS), ids="/".join)
def test_each_pod_name_is_one_kubernetes_accepts(key):
    name = _pod_name(_render(key[0], CENSUS[key]))
    assert name.startswith(f"{key[1]}-"), name
    assert DNS_LABEL.match(name) and len(name) <= 63, (
        f"{name!r} is not a DNS-1123 label of at most 63 bytes, so kubectl refuses the pod"
    )


@pytest.mark.parametrize("key", sorted(CENSUS), ids="/".join)
def test_each_pod_keeps_the_same_labels_across_runs(key):
    # Rendered, not read as text. test_netpol_from.py checks for the substring
    # `--labels app=qbittorrent-reach-probe`, which a label carrying the per-run suffix still
    # contains, so that guard alone would pass a label the NetworkPolicy no longer matches.
    first, second = (_labels(_render(key[0], CENSUS[key])) for _ in range(2))
    assert first == second, (
        f"the pod's labels changed between runs ({first!r}, {second!r}), so a selector "
        "keyed on them matches only one run's pod"
    )


def test_the_reach_probe_keeps_the_label_the_fence_admits():
    rendered = _render(
        "qbittorrent", CENSUS[("qbittorrent", "qbittorrent-reach-probe")]
    )
    assert _labels(rendered) == "app=qbittorrent-reach-probe", (
        "the reach probe's label must be exactly app=qbittorrent-reach-probe, the caller the "
        f"qbittorrent NetworkPolicy admits; got {_labels(rendered)!r}"
    )
