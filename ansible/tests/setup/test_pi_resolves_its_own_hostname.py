#!/usr/bin/env python3
"""daniel-pi's /etc/hosts carries its own hostname, so `sudo` never reaches the resolver.

WHY THIS EXISTS. `sudo` resolves the local hostname on every invocation. With no line for it in
/etc/hosts the lookup falls through to /etc/resolv.conf, whose first nameserver is the cluster's
Pi-hole VIP -- so every `sudo` on the Pi prints `unable to resolve host daniel-pi: Name or
service not known`, and a `sudo` during a cluster or Pi-hole outage waits on
the resolver timeout before running anything. That is exactly when an operator needs the Pi.

WHY IT NEEDS A GUARD. Both ways to break it are silent on the run that breaks them:

1. Drop the task and the Pi is back to resolving its own name over the network. Nothing fails, no
   monitor fires, and the symptom only shows up as a slow `sudo` during an unrelated outage.
2. Match the regexp on the HOSTNAME instead of the address and `lineinfile` stops recognising an
   existing `127.0.1.1` line that carries a stale name -- it appends a second one beside it. The
   task still reports `changed` and reads correct.

Run: uv run pytest ansible/tests/setup/test_pi_resolves_its_own_hostname.py
"""

import re

import pytest
from _helpers import SETUP_ROLES, load_tasks, walk_tasks

TASKS = SETUP_ROLES / "optimize_pi" / "tasks" / "main.yml"
HOSTS = "/etc/hosts"

# The loopback address the Debian/Ubuntu convention reserves for the host's own name, and the one
# daniel-server and daniel-box already carry. Pinned as a literal: the whole point is that the
# entry is local, so an address that resolved anywhere else would defeat it.
LOOPBACK = "127.0.1.1"


def hosts_lineinfile_specs() -> list[dict]:
    """Every `lineinfile` spec in the role that writes /etc/hosts."""
    return [
        spec
        for task in walk_tasks(load_tasks(TASKS))
        if isinstance(spec := task.get("ansible.builtin.lineinfile"), dict)
        and spec.get("path") == HOSTS
    ]


def entry_problems(spec: dict) -> list[str]:
    """Every way one /etc/hosts entry can be silently wrong. Empty list means correct."""
    problems = []
    line = str(spec.get("line", ""))
    regexp = str(spec.get("regexp", ""))
    if not line.startswith(f"{LOOPBACK} "):
        problems.append(f"line {line!r} does not map the hostname to {LOOPBACK}")
    if "{{" not in line:
        problems.append(
            f"line {line!r} hard-codes the hostname instead of rendering a fact"
        )
    if not re.match(r"^\^127\\?\.0\\?\.1\\?\.1", regexp):
        problems.append(
            f"regexp {regexp!r} is not anchored on {LOOPBACK}; matching the hostname instead "
            "leaves a stale 127.0.1.1 line in place and appends a duplicate"
        )
    return problems


def test_the_role_declares_the_pis_own_hostname_entry():
    # fact: ansible/roles/setup/optimize_pi/CLAUDE.md#What it does (`tasks/main.yml`)
    specs = hosts_lineinfile_specs()
    assert specs, (
        f"no lineinfile in {TASKS.name} writes {HOSTS}. Without it `sudo` on the Pi resolves its "
        "own hostname over DNS and waits on the resolver during a cluster outage (#2724)"
    )
    for spec in specs:
        problems = entry_problems(spec)
        assert not problems, f"the {HOSTS} entry is wrong: " + "; ".join(problems)


def test_the_entry_runs_under_the_pi_dns_tag():
    """A `--tags pi-dns` run rewrites resolv.conf; it has to install the local entry too."""
    tagged = [
        task
        for task in walk_tasks(load_tasks(TASKS))
        if isinstance(spec := task.get("ansible.builtin.lineinfile"), dict)
        and spec.get("path") == HOSTS
        and "pi-dns" in (task.get("tags") or [])
    ]
    assert tagged, (
        f"the {HOSTS} task carries no `pi-dns` tag, so the tag-scoped run that repoints the "
        "resolver leaves the host still resolving its own name over it"
    )


def test_a_loopback_entry_rendering_a_fact_is_clean():
    assert (
        entry_problems(
            {"regexp": r"^127\.0\.1\.1\s", "line": "127.0.1.1 {{ ansible_hostname }}"}
        )
        == []
    )


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        pytest.param(
            {
                "regexp": r"^{{ ansible_hostname }}",
                "line": "127.0.1.1 {{ ansible_hostname }}",
            },
            "regexp",
            id="regexp-matches-the-hostname",
        ),
        pytest.param(
            {"regexp": r"^127\.0\.1\.1\s", "line": "127.0.1.1 daniel-pi"},
            "hard-codes",
            id="hostname-hard-coded",
        ),
        pytest.param(
            {"regexp": r"^127\.0\.1\.1\s", "line": "10.0.0.139 {{ ansible_hostname }}"},
            f"does not map the hostname to {LOOPBACK}",
            id="not-loopback",
        ),
    ],
)
def test_a_broken_entry_is_flagged(spec, expected):
    """The RED half: each of these reads correct and leaves the defect in place."""
    problems = entry_problems(spec)
    assert any(expected in problem for problem in problems), problems
