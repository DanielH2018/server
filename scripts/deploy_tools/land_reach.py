#!/usr/bin/env python3
"""Which hosts a self-applied setup-role change still owes a hand, beyond the tick's own host.

The tick runs on ONE host (`has_gitops`, daniel-box), and `initial_setup.yml` runs against
one host per invocation, so a role it reaches on more hosts than that leaves the others
unconverged after a green tick (issue #1009). `setup_role_hosts` reads the role's own gate
in the playbook, or, for a role the playbook does not gate, the gates on its own tasks
(issue #2073: `deploy_ui` is one `block:` under `when: has_gitops`); `setup_file_hosts`
reads the gate on the task that ships a changed file, which is what decides where a FILE
lands when the role reaches more hosts than the file does (PR #1241's shape: box-only cron
templates under the ungated `initial_setup` role read as reaching every host).
`remaining_setup_hosts_note` is the string land.sh prints and the verdict hangs on.

Split out of `land_tags.py` at the module-length cap; the path-to-tag mappers stay there.
The traversal that reads a role's `tasks/` tree for those gates is `setup_role_chains.py`,
split out of this module at the same cap; this one evaluates the chains it returns.
"""

import functools
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.repo_paths import ALL_VARS, ANSIBLE, GITOPS_DEPLOY_FILES, HOST_VARS

sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

from deploy_logic import setup_role_playbook, setup_role_tag

from land_changes import changes_for
from setup_role_chains import (
    SHIPPED_DIRS,
    VARS_DIRS,
    handler_notifier_chains,
    task_chains,
    task_gates_naming,
    var_consumer_chains,
)

# The hosts land.sh's setup-role remediation ever names. daniel-stage is excluded on
# purpose -- it is not land.sh's business (HOSTS_LAND_SH_NEVER_DEPLOYS in deploy_tags.py is
# the same exclusion for a deploy tag), and initial_setup.yml is never run against it from
# here.
_HOSTS = ("daniel-box", "daniel-server", "daniel-pi")

# `ansible_connection=local` in hosts.ini -- selecting one of these with `-e target=` from
# elsewhere only picks its VARIABLES; the play still runs on whichever host you typed the
# command on (hosts.ini's own comment, ENFORCED by
# ansible/tests/deploy/test_local_connection_target.py). So a remaining host in this set
# must be reached by sshing to it first. daniel-pi is the one host actually driven remotely
# with `-e target=daniel-pi`, from wherever the play runs.
_LOCAL_CONNECTION_HOSTS = frozenset({"daniel-box", "daniel-server"})

_INITIAL_SETUP_YML = ANSIBLE / "initial_setup.yml"


def _initial_setup_roles(playbook: Path = _INITIAL_SETUP_YML) -> dict[str, object]:
    """{role name: its `when:` value (a string, a list, a bool, or None)}.

    Read from `playbook`'s own `roles:` list -- the same source Ansible itself resolves
    against, so this can never disagree with what a real run does. `playbook` defaults to
    this repo's `initial_setup.yml`; a test passes a synthetic one so the derivation it pins
    cannot drift when this repo's own gates change (mirrors `deploy_tags.py`'s
    `host_vars: Path = HOST_VARS` pattern).
    """
    play = yaml_fast.safe_load(playbook.read_text())[0]
    roles: dict[str, object] = {}
    for entry in play["roles"]:
        name = entry if isinstance(entry, str) else entry["role"]
        when = None if isinstance(entry, str) else entry.get("when")
        roles[name] = when
    return roles


def _host_vars(
    host: str, all_vars: Path = ALL_VARS, host_vars_dir: Path = HOST_VARS
) -> dict:
    """`all_vars` overridden by `host_vars_dir`/<host>.yml.

    The same precedence Ansible resolves a `when:` variable through (a host_vars key always
    wins over the group default). Defaults to this repo's group_vars/all.yml and host_vars/.
    """
    merged = dict(_vars_file(all_vars))
    hv = host_vars_dir / f"{host}.yml"
    if hv.exists():
        merged.update(_vars_file(hv))
    return merged


def _vars_file(path: Path) -> dict:
    """`path` parsed once per content: the cache key carries its mtime, so a rewrite misses.

    `_eval_when` reads the merged vars per gate per host, and group_vars/all.yml is the
    largest YAML in the tree; without this, a 40-path setup-role note re-parsed it several
    hundred times.
    """
    return _parse_vars_file(path, path.stat().st_mtime_ns)


@functools.lru_cache(maxsize=32)
def _parse_vars_file(path: Path, _mtime_ns: int) -> dict:
    return yaml_fast.safe_load(path.read_text()) or {}


def _eval_when(
    expr: object, host: str, all_vars: Path = ALL_VARS, host_vars_dir: Path = HOST_VARS
) -> bool:
    """Best-effort read of a `when:` value for one host.

    Every gate `initial_setup.yml` uses today is a bare var, an `or`/`and` of them, an
    `inventory_hostname == <var-or-literal>` comparison, or one of those with a trailing
    `| bool` filter -- all valid Python once `| bool` is stripped, so `eval` against the
    host's merged vars reads them exactly as Ansible would. A YAML list is Ansible's
    implicit AND (`when: [a, b]` means `a and b`), so it is joined before evaluating rather
    than rejected.

    Returns True -- host REACHED -- whenever evaluation cannot be trusted: a non-string,
    non-list value (a YAML `when: true`), an unresolved name, or a Jinja construct `eval`
    cannot parse. Wider than the truth is recoverable (an extra command an operator can
    no-op past); narrower silently hides a real gap, which is the failure this function
    exists to close. Same asymmetry `quiet_paths` already applies to a broad path it cannot
    read.
    """
    if isinstance(expr, list):
        expr = " and ".join(f"({e})" for e in expr)
    if not isinstance(expr, str):
        return True
    ns = dict(_host_vars(host, all_vars, host_vars_dir))
    ns["inventory_hostname"] = host
    py_expr = expr.replace("| bool", "").replace("|bool", "")
    try:
        return bool(eval(py_expr, {"__builtins__": {}}, ns))
    except Exception:
        return True


_SETUP_ROLES_DIR = ANSIBLE / "roles" / "setup"


def setup_role_hosts(
    role: str,
    playbook: Path = _INITIAL_SETUP_YML,
    all_vars: Path = ALL_VARS,
    host_vars_dir: Path = HOST_VARS,
    roles_dir: Path = _SETUP_ROLES_DIR,
) -> frozenset[str]:
    """Which of `_HOSTS` `initial_setup.yml` runs at least one task of `role` on.

    THE HOLE THIS CLOSES. `self_applied()` says a setup role is the tick's to apply, but the
    tick only ever runs on ONE host -- the one `gitops_deploy` is armed on (`has_gitops`,
    daniel-box only: `roles: [{role: gitops_deploy}, ...]` in initial_setup.yml gates no
    host at the playbook level -- the role dispatches internally, and `has_gitops` is true
    only in daniel-box's host_vars).
    `initial_setup.yml`'s own `hosts:` is `{{ target | default(lookup('pipe','hostname')) }}`
    -- one host per run -- so a role with NO `when:` gate (`initial_setup` itself among them)
    reaches every host the playbook is EVER run on, and the tick converging on daniel-box says
    nothing about the other two. Issue #1009: PR #1002 changed
    `roles/setup/initial_setup/files/kuma-push-lib.sh`, the tick converged, and land.sh read
    `settled` while daniel-server and daniel-pi kept running the old library.

    A role with no playbook gate is read from its own tasks instead of assumed to reach
    every host. `deploy_ui` wraps its whole `tasks/main.yml` in one `block:` under `when:
    has_gitops`, so the play visits all three hosts and every task skips on two of them; a
    change to its `defaults/`, `handlers/` or `tasks/main.yml` -- none of which names a
    shipped file -- read as owing those two a hand-run, and landing PR #2071 ended
    `needs-manual-apply` over four commands that would each run a play in which nothing
    fires (issue #2073). The gate that decides is the union over the role's leaf tasks:
    a host is reached when at least one task's `when:` chain -- the block and static-import
    gates above it included -- passes there. `gitops_deploy` keeps all three this way, since
    its `not has_gitops` branch tears the deployer down on the other two. A tasks tree that
    cannot be read stays wide, the same asymmetry `_eval_when` applies inside one gate.

    Returns an empty set for a role `initial_setup.yml` does not reach at all (its playbook
    is not `ansible/initial_setup.yml`, or it is not in that playbook's `roles:` list) --
    that is `plane_note`'s `unroutable` territory, not this function's to guess at.
    """
    if setup_role_playbook(role) != "ansible/initial_setup.yml":
        return frozenset()
    roles = _initial_setup_roles(playbook)
    if role not in roles:
        return frozenset()
    when = roles[role]
    if when is None:
        chains = task_chains(roles_dir / role, lambda task, task_file: True)
        if not chains:
            return frozenset(_HOSTS)
        return _hosts_passing(chains, _HOSTS, all_vars, host_vars_dir)
    return frozenset(h for h in _HOSTS if _eval_when(when, h, all_vars, host_vars_dir))


def _hosts_passing(
    chains, hosts, all_vars: Path, host_vars_dir: Path
) -> frozenset[str]:
    """The hosts of `hosts` on which at least one `when:` chain in `chains` passes whole.

    An empty chain -- an ungated leaf -- passes everywhere, so its presence settles every
    host before a single gate is read. The rest are deduplicated first: every leaf under
    deploy_ui's one block carries the same `("has_gitops",)`, and `_eval_when` re-parses
    group_vars/all.yml per gate per host, which put a 40-path `initial_setup` note at 4.7s
    against 0.24s before the role-level read existed.
    """
    if () in chains:
        return frozenset(hosts)
    distinct = {json.dumps(chain, sort_keys=True): chain for chain in chains}.values()
    return frozenset(
        h
        for h in hosts
        if any(
            all(_eval_when(g, h, all_vars, host_vars_dir) for g in chain)
            for chain in distinct
        )
    )


def setup_file_hosts(
    role: str,
    path: str,
    playbook: Path = _INITIAL_SETUP_YML,
    all_vars: Path = ALL_VARS,
    host_vars_dir: Path = HOST_VARS,
    roles_dir: Path = _SETUP_ROLES_DIR,
) -> frozenset[str]:
    """Which hosts a change to `path` (one file of setup role `role`) actually lands on.

    `setup_role_hosts` answers at role level, and `initial_setup` has no role gate, so
    every change to it read as reaching all three hosts. The files that change are cron
    templates its `tasks/crons.yml` ships under `when: has_gitops` or `inventory_hostname
    == 'daniel-box'`: 7 of the 18 `needs-manual-apply` verdicts in the two days to
    2026-09-05 (PRs 1049, 1079, 1093, 1187, 1207, 1241, 1244) prescribed playbook runs on
    daniel-pi and daniel-server for a file neither host installs. The gate that decides
    where a FILE lands is the one on the task that ships it, so this reads that gate --
    an `import_tasks` `when:` above it included -- and keeps only the role's hosts that
    pass at least one shipping task's chain.

    A `tasks/<file>.yml` path reaches the hosts that run a task IN that file: the leaf
    tasks it holds, each under the include chain that pulls the file in. PR #2071 changed
    `gitops_deploy/tasks/install.yml`, which `tasks/main.yml` includes under `when:
    has_gitops`, and the path read as every host (issue #2073); `tasks/teardown.yml`, the
    `not has_gitops` half of the same dispatcher, reaches the other two the same way. A
    task file holding only includes -- `main.yml` of a dispatcher -- has no leaf of its
    own and returns the role-level answer, which for a dispatcher is the union.

    A `defaults/` or `vars/` file ships nowhere, so its reach is read from the tasks that
    consume the vars it defines (`_var_consumer_chains`), and stays at the role level unless
    every var has a consumer.

    A `handlers/` file ships nowhere either, so its reach is read from the tasks that
    `notify:` the handlers it defines (`_handler_notifier_chains`), under the same
    every-handler-has-a-notifier rule. `meta/main.yml` carries no task-level evidence to
    read and keeps the role-level answer.

    Narrows only on evidence. A path outside `templates/`, `files/`, `tasks/`, `defaults/`,
    `vars/` or `handlers/`, a file no
    task names, or a tasks tree that cannot be read all return the role-level answer, the
    same "unknown stays wide" asymmetry `_eval_when` applies inside one gate.
    """
    role_hosts = setup_role_hosts(role, playbook, all_vars, host_vars_dir, roles_dir)
    if path.endswith(".md"):
        # Docs ship nowhere: no task under roles/setup/*/tasks names a .md file, and the
        # deployer's k8s branch already reads *.md as docs. PR #1079 was three box-only
        # templates plus the role CLAUDE.md, and the CLAUDE.md alone reached every host.
        return frozenset()
    parts = Path(path).parts
    if parts[4:5] == ("tests",):
        # A role's own pytest guards ship nowhere either: nothing stages a `tests/` file
        # (`ansible/tests/repo/test_no_role_ships_a_test_file.py` holds that tree-wide), so
        # no host runs the old copy. `land_tags.is_role_test_path` is the same predicate,
        # inlined because land_tags imports this module. Without it a `tests/` path fell
        # through to the ROLE-level reach, and the union over a PR's files widened a
        # box-only `files/` change back out to every host: PR #1884 touched
        # gitops_deploy's `files/*.py` (daniel-box only) and its `tests/*.py`, and land.sh
        # prescribed initial_setup.yml runs on daniel-server and daniel-pi (issue #1885).
        return frozenset()
    prefix = ("ansible", "roles", "setup", role)
    if not role_hosts or parts[: len(prefix)] != prefix or len(parts) < 6:
        return role_hosts
    if parts[4] == "tasks":
        task_file = parts[-1]
        chains = task_chains(roles_dir / role, lambda task, f: f == task_file)
    elif parts[4] in SHIPPED_DIRS:
        chains = task_gates_naming(roles_dir / role, parts[-1])
    elif parts[4] in VARS_DIRS:
        chains = var_consumer_chains(
            roles_dir / role, roles_dir / role / Path(*parts[4:])
        )
    elif parts[4] == "handlers":
        chains = handler_notifier_chains(
            roles_dir / role, roles_dir / role / Path(*parts[4:])
        )
    else:
        return role_hosts
    if not chains:
        return role_hosts
    return _hosts_passing(chains, role_hosts, all_vars, host_vars_dir)


def _setup_apply_command(role: str, host: str) -> str:
    """The exact command that applies `role` on `host` via initial_setup.yml.

    Mirrors `deploy_remediation._setup_commands`'s hand-written pair for the `common` role
    (ssh-then-run for a local-connection host, `-e target=` for daniel-pi) -- generalised to
    any role/host pair rather than that one role's two consumers.

    A `_LOCAL_CONNECTION_HOSTS` remote host (daniel-server, when it is not `local_host`)
    renders from ITS OWN checkout: `ansible_connection=local` means the play there runs as
    its own controller against its own `/home/ubuntu/server`, and nothing keeps that current
    -- the crons that `git pull` (secret-rotate.sh.j2, docs-refresh.sh.j2) are both `when:
    has_gitops`, daniel-box only. Skipping the pull renders the PRE-merge tree and reports
    `changed=0`, the exact trap `broad_remediation`'s docstring records an operator hitting
    on 2026-09-01 -- so the pull is folded into the same command rather than left as a
    separate step a copy-paste can drop. daniel-pi has no such hazard: `-e target=daniel-pi`
    renders on THIS host's already-current checkout and only executes remotely over SSH.
    """
    tag = setup_role_tag(role)
    if host in _LOCAL_CONNECTION_HOSTS:
        return (
            f'`ssh {host} "cd /home/ubuntu/server && git pull --ff-only && '
            f'ansible-playbook ansible/initial_setup.yml --tags {tag}"`'
        )
    return f"`ansible-playbook ansible/initial_setup.yml --tags {tag} -e target={host}`"


def remaining_setup_hosts_note(
    files,
    local_host: str,
    quiet=(),
    playbook: Path = _INITIAL_SETUP_YML,
    all_vars: Path = ALL_VARS,
    host_vars_dir: Path = HOST_VARS,
    roles_dir: Path = _SETUP_ROLES_DIR,
) -> str:
    """What a self-applied setup-role change still needs beyond `local_host`, or "" if nothing does.

    `local_host` is the host the tick just ran on. Additive to `plane_note`'s `unroutable`
    case, not a duplicate of it: that flags a role no playbook ever reaches; this flags a
    role `initial_setup.yml` DOES reach, on hosts the tick's single run never touches.

    Empty for the #723 shape -- `gitops_deploy` is `when: has_gitops`, true only on
    daniel-box, so a PR touching only a role whose sole reached host is `local_host` stays
    unowed to a hand, exactly as `plane_note` already keeps it.
    """
    cs = changes_for(files, quiet).changes
    remaining: dict[str, frozenset[str]] = {}
    role_files = {
        r: [p for p in files if p.startswith(f"ansible/roles/setup/{r}/")]
        for r in cs.setup_roles
    }
    for role in cs.setup_roles:
        # Per file, not per role: the gate that decides where a file lands is on the task
        # that ships it (`setup_file_hosts`), and a role-level read said every host for
        # any change to the ungated `initial_setup` role.
        hosts = frozenset().union(
            *(
                setup_file_hosts(role, p, playbook, all_vars, host_vars_dir, roles_dir)
                for p in role_files[role] or [""]
            )
        ) - {local_host}
        if hosts:
            remaining[role] = hosts
    if not remaining:
        return ""
    return "; ".join(
        f"`{role}` also reaches {host} (not applied by this tick): "
        f"{_setup_apply_command(role, host)}"
        for role in sorted(remaining)
        for host in sorted(remaining[role])
    )
