"""The ufw arm: the firewall rules a host package declares, and no others.

The design is docs/target-architecture/host/h7-delete-ansible.md, *Reconcilers for state dpkg
does not hold*. A host package's manifest lists "ufw" in `reconcilers` and carries:

    "ufw": {"rules":  [{"id": "ssh-limit", "spec": "limit 22/tcp", "keep": true,
                        "position": "last", "before": null}, ...],
            "legacy": [{"spec": "allow 22/tcp", "comment": "", "replaced_by": "ssh-limit"}, ...]}

The arm labels every rule it writes: `comment 'homelab:<id>'`, or `homelab:keep:<id>` for a rule
it must never delete. A `spec` is the rule exactly as `ufw show added` prints it after `ufw `.

How ufw 0.36.2-6 behaves, measured on Ubuntu 24.04 (docs/host-packages.md, *The UFW arm*):
appending a spec that already exists under another comment relabels that rule in place. `insert`
of an existing spec prints "Skipping inserting existing rule" and keeps the old comment. `delete`
with no comment deletes the rule whatever its comment, and a delete whose comment differs
deletes nothing. So the arm adopts a rule that predates it by appending it again, whatever its
`position`, and it uses `insert 1` only for a spec that is missing.

Each run does these steps in order. A fault fails the run, which holds the host. Drift is counted
and the run succeeds, so that a revert still updates the host.

 1. Set the default policies, when the manifest declares a rule, and only a policy that
    `ufw status verbose` shows differing. On an active firewall `ufw default` stops and starts
    the whole ruleset even when the value is unchanged, and a kube-router sync that lands inside
    that window wipes the before chains (the 2026-10-25 outage on daniel-box). An inactive ufw
    prints no policies, so every policy is set, which loads nothing. The routed default stays
    alone.
 2. Add or adopt each declared rule, then read `ufw show added` again; a rule that is not there
    with its label is a fault.
 3. Check each `before:` relation; a violated one is a fault, and the arm never repairs it.
 4. Only after 1 to 3 succeeded: delete each legacy rule that no declaration shares, once the
    rule that replaces it is present. A legacy rule is named by its exact spec and comment.
 5. Delete the `homelab:` rules no longer declared, under a floor: none when the declared set is
    empty, never a `homelab:keep:` rule, never more than MAX_DELETES in one run. A rule the
    floor keeps is drift.
 6. Enable ufw only when it reads `Status: inactive`, steps 1 to 3 succeeded, and the keep rules
    named in REQUIRED_KEEP are declared and present. Then check the relations again on the
    active ruleset. A failed enable, or a relation violated after it, is a fault.
 7. On an active ufw, read `iptables -S ufw-before-input` and fault when it lacks LOOPBACK, the
    first rule before.rules loads. Its absence means the loaded ruleset diverged from the file,
    whoever caused it, and the host has no loopback DNS. The check only reads, so its fault
    holds the host and restarts nothing; the repair, `ufw reload`, is the operator's. It reads
    once, straight after the arm's own commands, so a restore that lands later is missed.

When nothing declares ufw, and no earlier run did, the arm runs no ufw command at all.
"""

import re

from reconcile_lib.common import run, say

PREFIX = "homelab:"
KEEP_PREFIX = "homelab:keep:"
MAX_DELETES = 2
# The rules without which enabling ufw would lock a remote host out of ssh.
REQUIRED_KEEP = ("ssh-limit", "lan-ssh")
ID = re.compile(r"[a-z0-9][a-z0-9-]*")
SPEC = re.compile(r"(?:allow|deny|limit|reject) [A-Za-z0-9 ./:_-]+")
_ADDED = re.compile(r"ufw (.+?)(?: comment '(.*)')?")
_NUMBERED = re.compile(r"\[\s*\d+\].*#\s*(homelab:\S+)\s*$")
_DEFAULTS = re.compile(
    r"Default: (\w+) \(incoming\), (\w+) \(outgoing\), (\w+) \(routed\)"
)
POLICIES = {"incoming": "deny", "outgoing": "allow"}
LOOPBACK = "-A ufw-before-input -i lo -j ACCEPT"


def label(rule: dict) -> str:
    return f"{KEEP_PREFIX if rule['keep'] else PREFIX}{rule['id']}"


def valid(section: object) -> bool:
    """True when `section` is a well-formed manifest `ufw` object."""
    if not isinstance(section, dict) or set(section) != {"rules", "legacy"}:
        return False
    rules, legacy = section["rules"], section["legacy"]
    if not isinstance(rules, list) or not isinstance(legacy, list):
        return False
    ids = [r.get("id") for r in rules if isinstance(r, dict)]
    return (
        len(ids) == len(rules) == len(set(ids))
        and all(
            set(r) == {"id", "spec", "keep", "position", "before"}
            and isinstance(r["id"], str)
            and ID.fullmatch(r["id"])
            and isinstance(r["spec"], str)
            and SPEC.fullmatch(r["spec"])
            and isinstance(r["keep"], bool)
            and r["position"] in ("first", "last")
            and (r["before"] is None or r["before"] in ids)
            for r in rules
        )
        and all(
            isinstance(e, dict)
            and set(e) == {"spec", "comment", "replaced_by"}
            and isinstance(e["spec"], str)
            and SPEC.fullmatch(e["spec"])
            and isinstance(e["comment"], str)
            and "'" not in e["comment"]
            and e["replaced_by"] in ids
            for e in legacy
        )
    )


def added() -> list[tuple[str, str]] | None:
    """Each added rule as (spec, comment), in ufw's order; None when ufw cannot say."""
    result = run("ufw", "show", "added")
    if result.returncode != 0:
        say(f"ufw show added failed: {result.stderr.strip()}")
        return None
    found = (_ADDED.fullmatch(line) for line in result.stdout.splitlines())
    return [(m.group(1), m.group(2) or "") for m in found if m]


def ufw(*args: str) -> bool:
    result = run("ufw", *args)
    if result.returncode != 0:
        say(
            f"ufw {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.returncode == 0


def defaults() -> dict[str, str]:
    """The live default policy per direction, or {} when ufw is inactive or cannot say."""
    result = run("ufw", "status", "verbose")
    match = _DEFAULTS.search(result.stdout) if result.returncode == 0 else None
    if not match:
        return {}
    return dict(zip(("incoming", "outgoing", "routed"), match.groups(), strict=True))


def set_defaults() -> bool:
    """Step 1: set each policy in POLICIES that the live firewall does not already have."""
    live = defaults()
    ok = True
    for direction, policy in POLICIES.items():
        if live.get(direction) != policy:
            ok = ufw("default", policy, direction) and ok
    return ok


def before_chain_intact() -> bool:
    """Step 7: False when an active ufw's ufw-before-input lacks the loopback accept."""
    status = run("ufw", "status")
    if status.returncode != 0 or not status.stdout.startswith("Status: active"):
        return True
    chain = run("iptables", "-S", "ufw-before-input")
    if LOOPBACK in chain.stdout.splitlines():
        return True
    say(
        "ufw-before-input lacks the loopback accept, so the loaded rules differ from "
        "/etc/ufw/before.rules; run `ufw reload` and check `iptables -S ufw-before-input`"
    )
    return False


def crossed(order: list[str], rules: list[dict]) -> list[str]:
    """The `before:` relations the ordered labels break: a rule below the one it must precede."""
    by_id = {r["id"]: label(r) for r in rules}
    return [
        f"{r['id']} must sort above {r['before']}"
        for r in rules
        if r["before"]
        and (
            by_id[r["id"]] not in order
            or by_id[r["before"]] not in order
            or order.index(by_id[r["id"]]) > order.index(by_id[r["before"]])
        )
    ]


def put(rule: dict, current: list[tuple[str, str]]) -> bool:
    """Make `rule` present with its label: nothing, a relabel, or an add."""
    want = (rule["spec"], label(rule))
    if want in current:
        return True
    comment = ("comment", want[1])
    if any(spec == rule["spec"] for spec, _ in current) or not current:
        return ufw(
            *rule["spec"].split(), *comment
        )  # appending an existing spec relabels it
    if rule["position"] == "first":
        return ufw("insert", "1", *rule["spec"].split(), *comment)
    return ufw(*rule["spec"].split(), *comment)


def delete(spec: str, comment: str) -> bool:
    return ufw("delete", *spec.split(), *(("comment", comment) if comment else ()))


def reconcile(section: dict | None, prev: dict | None) -> tuple[bool, dict | None, int]:
    """Run the arm. Return whether it succeeded, its record for applied.json, and its drift."""
    if section is None and prev is None:
        return True, None, 0
    rules = section["rules"] if section else []
    legacy = section["legacy"] if section else []
    record = {"ids": sorted(r["id"] for r in rules)}
    ok = True
    if rules:
        ok = set_defaults()
        for rule in rules:
            ok = put(rule, added() or []) and ok
    current = added()
    if current is None:
        return False, record, 0
    present = [c for _, c in current]
    missing = [r["id"] for r in rules if (r["spec"], label(r)) not in current]
    for rule_id in missing:
        say(f"rule {rule_id} is not present with its label after the add")
    problems = crossed(present, rules)
    for problem in problems:
        say(f"ufw order: {problem}")
    ok = ok and not missing and not problems
    if ok:
        ok = remove_legacy(rules, legacy, current)
    drift = 0
    if ok:
        drift, ok = remove_undeclared(rules, current)
    if ok and rules:
        ok = enable(rules, present)
    if ok:
        ok = before_chain_intact()
    return ok, record, drift


def remove_legacy(rules: list[dict], legacy: list[dict], current: list) -> bool:
    """Step 4. A spec a declaration shares is the adopted rule itself, so it is never deleted."""
    declared = {r["spec"] for r in rules}
    ok = True
    for entry in legacy:
        if (
            entry["spec"] in declared
            or (entry["spec"], entry["comment"]) not in current
        ):
            continue
        replacement = next(r for r in rules if r["id"] == entry["replaced_by"])
        if (replacement["spec"], label(replacement)) in current:
            ok = delete(entry["spec"], entry["comment"]) and ok
    return ok


def remove_undeclared(rules: list[dict], current: list) -> tuple[int, bool]:
    """Step 5. Return the rules the floor kept, and whether every delete succeeded."""
    declared = {label(r) for r in rules}
    stale = [
        (spec, comment)
        for spec, comment in current
        if comment.startswith(PREFIX) and comment not in declared
    ]
    deletable = (
        [] if not rules else [s for s in stale if not s[1].startswith(KEEP_PREFIX)]
    )
    deleted = deletable[:MAX_DELETES]
    ok = all([delete(spec, comment) for spec, comment in deleted])
    return len(stale) - len(deleted), ok


def enable(rules: list[dict], present: list[str]) -> bool:
    """Step 6: enable an inactive ufw once the keep rules that keep ssh reachable are in place."""
    status = run("ufw", "status")
    if status.returncode != 0:
        say(f"ufw status failed: {status.stderr.strip()}")
        return False
    if not status.stdout.startswith("Status: inactive"):
        return True
    keep = {r["id"] for r in rules if r["keep"]}
    absent = [i for i in REQUIRED_KEEP if i not in keep]
    absent += [label(r) for r in rules if r["keep"] and label(r) not in present]
    if absent:
        say(f"not enabling ufw: keep rules {absent} are not declared and present")
        return False
    if not ufw("--force", "enable"):
        return False
    numbered = run("ufw", "status", "numbered").stdout.splitlines()
    order = [m.group(1) for m in map(_NUMBERED.match, numbered) if m]
    problems = crossed(order, rules)
    for problem in problems:
        say(f"ufw order after enable: {problem}")
    return not problems
