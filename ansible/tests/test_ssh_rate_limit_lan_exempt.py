"""The LAN must be exempt from UFW's SSH rate-limiter, and the exemption must sort above it.

WHY THIS IS A TEST AND NOT A COMMENT. `limit` REJECTs a source IP that opens 6 connections in
30 seconds on a rolling window, so retries hold the block open rather than riding it out.
Several Claude sessions work this repo at once and each reaches daniel-server over ssh; twice
now that quorum has crossed the threshold and locked every session out of the host for as long
as anything kept retrying. `roles/setup/initial_setup/tasks/network.yml` carries the reasoning;
this pins the two properties that reasoning depends on.

BOTH properties matter, and the second is the one that fails silently. ufw matches rules in
order and takes the first hit, so an `allow` appended BELOW the `limit` rule is inert — present
in `ufw status`, changing nothing. That is the exact shape #508 already cost this role once (the
staging route-deny that deployed cleanly, listed correctly and fenced nothing), and it is why
the allow rule has to declare a top insert rather than rely on task order alone: every host this
has ever deployed to already carries the `limit` rule, so a plain append lands after it.
"""

import yaml
from _helpers import REPO

_REPO = REPO
_NETWORK = (
    _REPO / "ansible" / "roles" / "setup" / "initial_setup" / "tasks" / "network.yml"
)

# The variable the exemption must be scoped to. Hardcoded rather than read from group_vars on
# purpose: the point is that the rule is scoped to the LAN at all, and a test that read the
# same name from the same tree would pass for a rule scoped to anything.
_LAN_VAR = "lan_subnet"


def _ufw_rules(tasks: list[dict]) -> list[tuple[int, dict]]:
    """Every community.general.ufw rule task, paired with its position in the file."""
    rules = []
    for position, task in enumerate(tasks or []):
        args = task.get("community.general.ufw")
        if isinstance(args, dict) and "rule" in args:
            rules.append((position, args))
    return rules


def ssh_limiter_problems(tasks: list[dict]) -> list[str]:
    """Every reason these tasks would still rate-limit SSH from the LAN.

    Pure and list-returning so the rejecting half of this suite drives the same function the
    accepting half drives, rather than asserting arithmetic of its own. Empty means clean.
    """
    problems = []
    allow_at = limit_at = None
    allow_args: dict = {}

    for position, args in _ufw_rules(tasks):
        if args.get("port") != "ssh" or args.get("delete"):
            continue
        from_ip = str(args.get("from_ip") or "")
        if args.get("rule") == "allow" and _LAN_VAR in from_ip and allow_at is None:
            allow_at, allow_args = position, args
        elif args.get("rule") == "limit" and limit_at is None:
            limit_at = position

    if allow_at is None:
        problems.append(
            f"no UFW `allow` rule on port ssh scoped to {{{{ {_LAN_VAR} }}}}. Without it the "
            f"`limit` rule rate-limits LAN hosts, and concurrent agent sessions livelock the "
            f"host's ssh."
        )
        return problems

    if limit_at is None:
        # Not a defect on its own, but it means the ordering property below is unpinned, so
        # say so rather than passing silently on a file that changed shape.
        problems.append(
            "no UFW `limit` rule on port ssh remains. If the limiter was removed entirely "
            "this guard no longer pins anything -- delete it or rewrite it deliberately."
        )
        return problems

    if allow_at > limit_at:
        problems.append(
            f"the LAN `allow` rule is declared at task {allow_at}, below the `limit` rule at "
            f"task {limit_at}. ufw takes the first matching rule, so on a fresh host this "
            f"allow would be appended underneath the limiter and do nothing."
        )

    if (
        allow_args.get("insert") != 0
        or allow_args.get("insert_relative_to") != "first-ipv4"
    ):
        problems.append(
            "the LAN `allow` rule does not declare `insert: 0` with "
            "`insert_relative_to: first-ipv4`. Every host this deploys to already carries the "
            "`limit` rule, so without a top insert the allow is appended BELOW it and is "
            "inert -- listed in `ufw status`, changing nothing."
        )

    return problems


def _tasks_from_disk() -> list[dict]:
    return yaml.safe_load(_NETWORK.read_text())


def test_the_real_network_tasks_exempt_the_lan():
    assert ssh_limiter_problems(_tasks_from_disk()) == []


# ── the rejecting half ──────────────────────────────────────────────────────────────────────
# Each fixture is the real file with one property broken, driven through the same verdict
# function. A guard only ever observed passing is not evidence it can fail.

_ALLOW = {
    "name": "allow",
    "community.general.ufw": {
        "rule": "allow",
        "port": "ssh",
        "proto": "tcp",
        "from_ip": "{{ lan_subnet }}",
        "insert": 0,
        "insert_relative_to": "first-ipv4",
    },
}
_LIMIT = {
    "name": "limit",
    "community.general.ufw": {"rule": "limit", "port": "ssh", "proto": "tcp"},
}


def test_the_fixture_pair_is_itself_clean():
    """Guards the rejecting tests below: they only prove something if the base fixture passes."""
    assert ssh_limiter_problems([_ALLOW, _LIMIT]) == []


def test_a_missing_lan_allow_is_flagged():
    problems = ssh_limiter_problems([_LIMIT])
    assert any("no UFW `allow` rule" in p for p in problems), problems


def test_an_allow_ordered_below_the_limit_is_flagged():
    problems = ssh_limiter_problems([_LIMIT, _ALLOW])
    assert any("below the `limit` rule" in p for p in problems), problems


def test_an_allow_without_a_top_insert_is_flagged():
    appended = {
        "name": "allow",
        "community.general.ufw": {
            "rule": "allow",
            "port": "ssh",
            "proto": "tcp",
            "from_ip": "{{ lan_subnet }}",
        },
    }
    problems = ssh_limiter_problems([appended, _LIMIT])
    assert any("does not declare `insert: 0`" in p for p in problems), problems


def test_an_allow_scoped_to_something_other_than_the_lan_is_flagged():
    """A rule scoped to a single host, or to `any`, is not the exemption this pins."""
    narrowed = {
        "name": "allow",
        "community.general.ufw": {
            "rule": "allow",
            "port": "ssh",
            "proto": "tcp",
            "from_ip": "10.0.0.215",
            "insert": 0,
            "insert_relative_to": "first-ipv4",
        },
    }
    problems = ssh_limiter_problems([narrowed, _LIMIT])
    assert any("no UFW `allow` rule" in p for p in problems), problems


def test_a_deleted_allow_rule_does_not_count_as_the_exemption():
    """network.yml carries a `delete: true` allow rule; it must not satisfy this guard."""
    deleted = {
        "name": "delete",
        "community.general.ufw": {
            "rule": "allow",
            "port": "ssh",
            "proto": "tcp",
            "from_ip": "{{ lan_subnet }}",
            "delete": True,
        },
    }
    problems = ssh_limiter_problems([deleted, _LIMIT])
    assert any("no UFW `allow` rule" in p for p in problems), problems
