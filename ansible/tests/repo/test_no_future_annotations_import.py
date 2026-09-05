"""No uv-run first-party module carries `from __future__ import annotations`.

`docs/python-code-organization.md` states the rule: on 3.14 PEP 649 defers annotation
evaluation by default, so the import buys nothing. The rule governed NEW modules only, so
nothing removed the line from the modules that already had it — half of `scripts/infra_map/`
carried it and half did not, and the next author adding a module there copies a sibling.
A convention only holds when the tree agrees with it, which is what this guard checks
(issue #1112).

The cost of the import is not runtime. It is that a reader cannot tell the convention from
the exceptions by looking.

Run: uv run pytest ansible/tests/repo/test_no_future_annotations_import.py
"""

import re
import subprocess

from _helpers import REPO

SELF = "ansible/tests/repo/test_no_future_annotations_import.py"

# A line that is exactly the future import, allowing a trailing comment.
FUTURE_ANNOTATIONS = re.compile(r"^from __future__ import annotations\b", re.MULTILINE)

# DECIDED: host-shipped `ansible/roles/*/*/files/*.py` is EXEMPT, and the exemption is not
# cosmetic. Those programs run under the hosts' own interpreter, not the repo's uv env:
# `python3 -V` on daniel-server and daniel-pi both report 3.12.3 (measured 2026-09-05), and the
# k8s `files/*.py` run under whatever python their pod image ships. Below 3.14 there is no PEP
# 649, so annotations evaluate EAGERLY at def/class time and the import is load-bearing
# insurance against a forward reference — `def merge(self, other: Foo) -> Foo:` inside
# `class Foo` raises NameError without it. The repo suite cannot observe that: it runs on 3.14,
# where every such annotation is lazy either way. So the exemption is what keeps a green suite
# from authorising a cron-time NameError on a host.
HOST_SHIPPED = re.compile(r"^ansible/roles/[^/]+/[^/]+/files/")

# `ansible/collections/` is the vendored third-party tree — never linted, never ours.
VENDORED = "ansible/collections/"

# Named members the census must contain, so it cannot pass by finding nothing. One per
# execution context that reaches the repo's uv 3.14 interpreter: an Ansible filter plugin
# (deploys run through `uv run ansible-playbook`), a Claude hook (its `.sh` wrapper execs
# `uv run --no-sync python`), an eval script, and the suite's own shared helper.
#
# DECIDED: no path literal under the scripts tree belongs in this set, which is why the
# largest of those contexts is held by the floor below rather than by a named member.
# `lib/script_coverage.py` reads such a mention inside a test as a COVERAGE credit, and a
# census names modules without exercising them. A literal here therefore credits someone
# else's module to this guard: it took the infra_map facade's `model` off its importer's
# suite and failed `test_the_infra_map_facade_members_inherit_the_facades_suite`. The
# 200-module floor already carries that context, since it is most of the 200.
KNOWN_UV_RUN_MODULES = frozenset(
    {
        "ansible/filter_plugins/toposort.py",
        ".claude/hooks/block-footguns.py",
        "evals/trend.py",
        "ansible/tests/_helpers.py",
    }
)

# Named members the EXEMPTION must contain, for the same reason: an exemption that silently
# matched nothing would leave the host scripts gated by a rule their interpreter cannot take.
KNOWN_HOST_SHIPPED_MODULES = frozenset(
    {
        "ansible/roles/setup/common/files/host_lib.py",
        "ansible/roles/setup/gitops_deploy/files/deploy_changes.py",
    }
)


def _tracked_python_files() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [rel for rel in listed.split("\0") if rel]


def _uv_run_modules() -> list[str]:
    return [
        rel
        for rel in _tracked_python_files()
        if not rel.startswith(VENDORED) and not HOST_SHIPPED.match(rel)
    ]


def _host_shipped_modules() -> list[str]:
    return [rel for rel in _tracked_python_files() if HOST_SHIPPED.match(rel)]


def test_the_census_finds_the_modules_it_governs():
    """Without this, the test below passes vacuously on an empty file list."""
    censused = set(_uv_run_modules())
    assert len(censused) >= 200
    assert KNOWN_UV_RUN_MODULES <= censused, KNOWN_UV_RUN_MODULES - censused


def test_the_exemption_finds_the_host_shipped_modules_it_covers():
    """Without this, the exemption could match nothing and nobody would notice."""
    exempt = set(_host_shipped_modules())
    assert len(exempt) >= 20
    assert KNOWN_HOST_SHIPPED_MODULES <= exempt, KNOWN_HOST_SHIPPED_MODULES - exempt


def test_no_uv_run_module_imports_future_annotations():
    # A scanner must not scan itself: the red-proof fixtures below quote the exact line this
    # guard looks for, as strings rather than as code.
    offenders = [
        rel
        for rel in _uv_run_modules()
        if rel != SELF
        and FUTURE_ANNOTATIONS.search((REPO / rel).read_text(errors="replace"))
    ]
    assert not offenders, (
        f"{offenders} carry `from __future__ import annotations`. The repo's uv interpreter is "
        f"3.14, where PEP 649 already defers annotation evaluation, so the line is dead weight "
        f"and half a directory carrying it is how the convention drifts. Delete it — see "
        f"docs/python-code-organization.md. A program that runs under a HOST interpreter "
        f"(3.12) belongs in `ansible/roles/*/*/files/`, which this guard exempts."
    )


CLEAN = '"""A module."""\n\nimport os\n\nprint(os)\n'
FLAGGED = (
    '"""A module."""\n\nfrom __future__ import annotations\n\nimport os\n\nprint(os)\n'
)


def test_a_module_without_the_import_is_clean():
    assert not FUTURE_ANNOTATIONS.search(CLEAN)


def test_a_module_with_the_import_is_flagged():
    assert FUTURE_ANNOTATIONS.search(FLAGGED)


def test_an_unrelated_future_import_is_clean():
    """Only `annotations` is dead on 3.14; the pattern must not swallow the whole family."""
    assert not FUTURE_ANNOTATIONS.search("from __future__ import division\n")
