"""Guards on `dev_tooling_hosts` — which hosts `initial_setup --tags tooling` provisions.

The tag installs uv, the uv-managed CLI tools, the pinned host Python, the /usr/local/bin/uv
symlink and the pinned Vale binary. Those tasks used to be gated on `has_repo_checkout`, or on
nothing at all, and daniel-pi sets `has_repo_checkout: true` — so the tag implied work on a
512 MB Zero 2 W that nobody intended to apply, and issue #1726 settled that the Pi is excluded.

THE MEMBERSHIP IS THE WHOLE GUARD. An allowlist that quietly grew a host provisions it on the
next run of the tag, which is exactly the outcome the operator rejected. So the accepting half
here asserts the exact list, and the rejecting half names daniel-pi, so a re-add fails with the
host that came back rather than with a list that moved.

THE CENSUS GLOBS FOR ITS OWN SUBJECT. Every task carrying the `tooling` tag must gate on the
list, but a rename of the tag would empty the census and pass that assertion over nothing —
so KNOWN_TOOLING_TASKS names the members it must find.

Run: uv run pytest ansible/tests/setup/test_dev_tooling_hosts.py
"""

from _helpers import ANSIBLE, HOST_VARS
from lib import yaml_fast

VAR = "dev_tooling_hosts"

GROUP_VARS = yaml_fast.safe_load(
    (ANSIBLE / "inventory" / "group_vars" / "all.yml").read_text()
)
TASKS_DIR = ANSIBLE / "roles" / "setup" / "initial_setup" / "tasks"
GATE = f"inventory_hostname in {VAR}"

# The one `tooling`-tagged task that is deliberately ungated: it registers the connecting user's
# home directory, which the `git-hooks` task reads too, so a tag-scoped run of either needs it.
# It installs nothing.
SHARED_FACT_TASK = "Resolve the deploy user's home directory"

# The census must find these. Named rather than counted, so a rename fails with the member that
# went missing instead of leaving an assertion to pass over an empty set.
KNOWN_TOOLING_TASKS = frozenset(
    {
        "Install uv (per-user; PEP 668-safe on 24.04+)",
        "Install Python CLI tooling as uv tools",
        "Install the pinned host Python",
        "Publish uv at a stable path for systemd and cron",
        "Pin the Vale version this host installs",
        "Read the Vale version this host already has",
        "Install the pinned Vale binary",
    }
)


def _tooling_tasks() -> list[tuple[str, dict]]:
    """(file name, task) for every task in the role carrying the `tooling` tag."""
    found = []
    for tasks_file in sorted(TASKS_DIR.glob("*.yml")):
        for task in yaml_fast.safe_load(tasks_file.read_text()) or []:
            if not isinstance(task, dict):
                continue
            tags = task.get("tags") or []
            tags = tags if isinstance(tags, list) else [tags]
            if "tooling" in tags:
                found.append((tasks_file.name, task))
    return found


def _gated_on_the_allowlist(when: object) -> bool:
    clauses = when if isinstance(when, list) else [when]
    return any(GATE in str(clause) for clause in clauses)


def test_the_allowlist_names_the_two_hosts_somebody_commits_from():
    assert GROUP_VARS[VAR] == ["daniel-box", "daniel-server"], (
        f"{VAR} is {GROUP_VARS[VAR]!r}. The two members are the hosts somebody commits from. "
        "Adding one provisions uv, a pinned Python 3.14 and Vale there on the next "
        "`initial_setup --tags tooling` run."
    )


def test_daniel_pi_is_excluded():
    """The rejecting half. The operator settled this on 2026-09-10 (issue #1726): the Pi holds a
    checkout but is driven remotely over ssh and nobody commits from it, so provisioning a
    pinned Python 3.14 on a 512 MB Zero 2 W was rejected in favour of narrowing the gate."""
    assert "daniel-pi" not in GROUP_VARS[VAR], (
        "daniel-pi is back in the allowlist. Re-adding it provisions the Pi, which is the "
        "outcome issue #1726 rejected — the Pi's `has_repo_checkout: true` is a proxy for "
        "'somebody commits here', which is not true of it."
    )


def test_daniel_stage_is_excluded():
    """`initial_setup.yml` has never completed against the staging guest at all — its git-hooks
    task stopped the play there (docs/staging-cluster.md). The list says so rather than leaving
    a future reader to wonder whether the guest was considered."""
    assert "daniel-stage" not in GROUP_VARS[VAR]


def test_every_member_is_a_real_host():
    """A typo would exclude the host it meant to admit, silently — `inventory_hostname in` never
    errors on a name nothing matches."""
    known = {path.stem for path in HOST_VARS.glob("*.yml")} - {"_example"}
    assert set(GROUP_VARS[VAR]) <= known, (
        f"{VAR} names hosts with no host_vars file: {sorted(set(GROUP_VARS[VAR]) - known)}"
    )


def test_every_member_holds_a_repo_checkout():
    """test_repo_checkout_gate.py accepts membership of this list as a checkout gate, on the
    grounds that both members hold one. That is only true while no member sets the flag false,
    and nothing else would notice if one did."""
    without = [
        host
        for host in GROUP_VARS[VAR]
        if yaml_fast.safe_load((HOST_VARS / f"{host}.yml").read_text()).get(
            "has_repo_checkout", True
        )
        is False
    ]
    assert not without, (
        f"{without} are in {VAR} but declare has_repo_checkout: false. "
        "test_repo_checkout_gate.py accepts this list as a checkout gate — fix one or the other."
    )


def test_the_census_finds_the_tooling_tasks():
    """Non-vacuity for the assertion below, which globs for its own subject."""
    names = {task.get("name") for _f, task in _tooling_tasks()}
    missing = KNOWN_TOOLING_TASKS - names
    assert not missing, f"no `tooling`-tagged task named: {sorted(missing)}"


def test_every_tooling_task_gates_on_the_allowlist():
    ungated = [
        f"{name}: {task.get('name')!r}"
        for name, task in _tooling_tasks()
        if task.get("name") != SHARED_FACT_TASK
        and not _gated_on_the_allowlist(task.get("when"))
    ]
    assert not ungated, (
        f"these install the tooling on every target, not just {VAR}:\n  "
        + "\n  ".join(ungated)
    )


def test_the_shared_fact_task_is_still_the_only_exemption():
    """The rejecting half of the exemption: it holds only while that task installs nothing. If it
    grows a command that writes to the host, the exemption has to go rather than be inherited."""
    task = next(t for _f, t in _tooling_tasks() if t.get("name") == SHARED_FACT_TASK)
    assert task.get("changed_when") is False, (
        f"{SHARED_FACT_TASK!r} is exempt from the gate because it only registers a fact. It now "
        "reports changes, so it does something to the target — gate it or drop the exemption."
    )
    assert sorted(task.get("tags") or []) == ["git-hooks", "tooling"], (
        "the exemption exists because `git-hooks` needs this fact too; if that is no longer "
        "true, gate the task."
    )


def test_the_git_hooks_task_gates_on_the_allowlist():
    """`prek install` needs prek, which arrives with this tooling. Gating the two differently
    is what left daniel-pi with a task that can only fail."""
    tasks = yaml_fast.safe_load((TASKS_DIR / "system-tuning.yml").read_text())
    task = next(t for t in tasks if t.get("name") == "Install Git hooks")
    assert _gated_on_the_allowlist(task.get("when")), (
        f"Install Git hooks runs `prek install` but is not gated on {VAR}, which is what "
        f"installs prek. Its `when` is {task.get('when')!r}."
    )
