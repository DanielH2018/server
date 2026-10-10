#!/usr/bin/env python3
"""Which playbook and `--tags` value apply each setup role, and whether the tick's host runs it.

Derived from the playbooks that apply the roles: `initial_setup.yml`, `k3s-bringup.yml` and
`bootstrap.yml` (#3734). The deployer used to carry this as hand-written tables, and a test
compared them with the playbooks. A playbook is the one list now; this reads it.

THE BUG THIS EXISTS TO KILL. `--tags` matching no task makes `ansible-playbook` exit 0, so a
wrong playbook, a wrong tag or a role gated off the host records an apply of nothing (PR #702,
#3933). So a role this cannot place is left OUT of the routes, with the reason in `unplaced`,
and the deployer records it for a hand rather than guessing.

For each directory under `ansible/roles/setup/`:
  - the playbook is `initial_setup.yml` when it lists the role, else the one other playbook
    that does, else None (`common`, which no playbook applies). Two other playbooks is
    unplaced. Only a play's `roles:` list counts; an `include_role` task is not an apply.
  - the tag is the entry's one tag. `chezmoi_setup` is tagged `chezmoi`. None or several is
    unplaced.
  - `on_tick_host` reads the entry's `when:` against the tick host's vars with
    `setup_gates.eval_when_strict`. A gate it cannot read is unplaced, never "reached".
  - `host` is the one host the gate admits, for a role the tick's host does not run. The
    printed remediation appends `-e target=<host>` from it.

The deployer runs under `uv run --no-project` and cannot import yaml, so it runs this as a
subprocess over origin's tree (`deploy_setup_roles.derive_routing`). A repo-env reader
imports `routes()` instead.

Usage: setup_routing.py [--ref REF] [--host HOST]
Prints `{"routes": {role: {...}}, "unplaced": {role: reason}}` as JSON. Exit 1 when the
tree at REF cannot be read.
"""

import argparse
import contextlib
import json
import sys
from collections.abc import Generator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.k8s_roles import role_dirs
from lib.repo_paths import GITOPS_DEPLOY_FILES, REPO

import setup_gates
from setup_role_diff import tree_at

sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

import deploy_setup_roles

INITIAL_SETUP = "ansible/initial_setup.yml"
# In precedence order: `sops_setup` is in `bootstrap.yml` for a host that cannot decrypt yet
# and in `initial_setup.yml` for maintenance, and the tick's apply is the second.
PLAYBOOKS = (INITIAL_SETUP, "ansible/k3s-bringup.yml", "ansible/bootstrap.yml")


def _entries(playbook: Path) -> list[tuple[str, object, object]]:
    """(role, tags, when) for every entry in every play's `roles:` list."""
    found = []
    for play in yaml_fast.safe_load(playbook.read_text()) or []:
        for entry in (play or {}).get("roles") or []:
            if isinstance(entry, str):
                found.append((entry, None, None))
            elif isinstance(entry, dict):
                name = entry.get("role", entry.get("name"))
                found.append((name, entry.get("tags"), entry.get("when")))
    return found


def _one_tag(tags: object) -> str | None:
    if isinstance(tags, str):
        tags = [tags]
    if isinstance(tags, list) and len(tags) == 1 and isinstance(tags[0], str):
        return tags[0]
    return None


def tick_host(root: Path = REPO) -> str:
    """The one `has_gitops` host in `root`'s inventory: the host the tick runs on.

    Raises:
        ValueError: no host, or several, set `has_gitops`.
    """
    all_vars, host_vars = _vars_at(root)
    hosts = [
        h
        for h in setup_gates.HOSTS
        if setup_gates._host_vars(h, all_vars, host_vars).get("has_gitops") is True
    ]
    if len(hosts) != 1:
        raise ValueError(f"expected one has_gitops host, found {hosts}")
    return hosts[0]


def _vars_at(root: Path) -> tuple[Path, Path]:
    inventory = root / "ansible" / "inventory"
    return inventory / "group_vars" / "all.yml", inventory / "host_vars"


def routes(root: Path = REPO, host: str | None = None) -> dict:
    """`{"routes": {role: route}, "unplaced": {role: reason}}` for the tree at `root`.

    Args:
        root: a checkout, or a tree `tree_at` wrote, holding `ansible/`.
        host: the tick's host; the one `has_gitops` host when None.
    """
    host = host or tick_host(root)
    all_vars, host_vars = _vars_at(root)
    lists = {pb: _entries(root / pb) for pb in PLAYBOOKS if (root / pb).is_file()}
    out: dict = {"routes": {}, "unplaced": {}}
    for role_dir in role_dirs(root / "ansible" / "roles" / "setup"):
        role = role_dir.name
        listing = {pb: [e for e in es if e[0] == role] for pb, es in lists.items()}
        listing = {pb: es for pb, es in listing.items() if es}
        if INITIAL_SETUP in listing:
            playbook = INITIAL_SETUP
        elif len(listing) == 1:
            playbook = next(iter(listing))
        elif not listing:
            out["routes"][role] = {
                "playbook": None,
                "tag": role,
                "on_tick_host": False,
                "host": None,
            }
            continue
        else:
            out["unplaced"][role] = f"listed in {sorted(listing)}"
            continue
        if len(listing[playbook]) != 1:
            out["unplaced"][role] = (
                f"listed {len(listing[playbook])} times in {playbook}"
            )
            continue
        _, tags, when = listing[playbook][0]
        tag = _one_tag(tags)
        if tag is None:
            out["unplaced"][role] = f"{playbook} gives it tags {tags!r}, not one tag"
            continue
        route = {"playbook": playbook, "tag": tag, "on_tick_host": False, "host": None}
        if playbook == INITIAL_SETUP:
            try:
                admitted = [
                    h
                    for h in setup_gates.HOSTS
                    if when is None
                    or setup_gates.eval_when_strict(when, h, all_vars, host_vars)
                ]
            except setup_gates.GateUnreadable as exc:
                out["unplaced"][role] = f"its gate cannot be read: {exc}"
                continue
            route["on_tick_host"] = host in admitted
            if not route["on_tick_host"] and len(admitted) == 1:
                route["host"] = admitted[0]
        out["routes"][role] = route
    return out


@contextlib.contextmanager
def routed_by(root: Path, host: str | None = None) -> Generator[None]:
    """Route setup roles by the tree at `root` inside the block, then restore the routing.

    For a reader that evaluates a tree other than its own checkout: `land_reach` reads the
    merge commit's tree, and the roles it asks about must route by that tree's playbooks. A
    tree it cannot read keeps the routing already in force, and says so on stderr. The
    reader only prints a note, so the checkout's routing is better than failing the landing.
    """
    saved = deploy_setup_roles.current_routing()
    try:
        derived = routes(root, host)
    except (OSError, ValueError) as exc:
        print(
            f"setup_routing: cannot read {root} ({exc}); routing by the checkout",
            file=sys.stderr,
        )
    else:
        deploy_setup_roles.use_routing(
            deploy_setup_roles.routes_from_json(json.dumps(derived))[0]
        )
    try:
        yield
    finally:
        deploy_setup_roles.use_routing(saved)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ref", default="", help="read the ansible/ tree at this commit")
    ap.add_argument(
        "--host", default=None, help="the tick's host (default: has_gitops)"
    )
    args = ap.parse_args(argv)
    with tree_at(args.ref, REPO) if args.ref else contextlib.nullcontext(REPO) as root:
        if root is None:
            print(f"cannot read {args.ref}'s ansible/ tree", file=sys.stderr)
            return 1
        try:
            result = routes(root, args.host)
        except (OSError, ValueError) as exc:
            print(f"cannot derive the setup routing: {exc}", file=sys.stderr)
            return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
