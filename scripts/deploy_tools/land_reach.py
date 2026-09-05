#!/usr/bin/env python3
"""Which hosts a self-applied setup-role change still owes a hand, beyond the tick's own host.

The tick runs on ONE host (`has_gitops`, daniel-box), and `initial_setup.yml` runs against
one host per invocation, so a role it reaches on more hosts than that leaves the others
unconverged after a green tick (issue #1009). `setup_role_hosts` reads the role's own gate
in the playbook; `setup_file_hosts` reads the gate on the task that ships a changed file,
which is what decides where a FILE lands when the role itself is ungated (PR #1241's
shape: box-only cron templates under the ungated `initial_setup` role read as reaching every
host). `remaining_setup_hosts_note` is the string land.sh prints and the verdict hangs on.

Split out of `land_tags.py` at the module-length cap; the path-to-tag mappers stay there.
"""

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.repo_paths import ALL_VARS, ANSIBLE, GITOPS_DEPLOY_FILES, HOST_VARS

sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

from deploy_logic import (
    services_from_changed_paths,
    setup_role_playbook,
    setup_role_tag,
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
    merged = dict(yaml_fast.safe_load(all_vars.read_text()) or {})
    hv = host_vars_dir / f"{host}.yml"
    if hv.exists():
        merged.update(yaml_fast.safe_load(hv.read_text()) or {})
    return merged


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


def setup_role_hosts(
    role: str,
    playbook: Path = _INITIAL_SETUP_YML,
    all_vars: Path = ALL_VARS,
    host_vars_dir: Path = HOST_VARS,
) -> frozenset[str]:
    """Which of `_HOSTS` `initial_setup.yml` applies `role` to.

    THE HOLE THIS CLOSES. `self_applied()` says a setup role is the tick's to apply, but the
    tick only ever runs on ONE host -- the one `gitops_deploy` is armed on (`has_gitops`,
    daniel-box only: `roles: [{role: gitops_deploy, when: has_gitops}, ...]` in
    initial_setup.yml, and `has_gitops` is true only in daniel-box's host_vars).
    `initial_setup.yml`'s own `hosts:` is `{{ target | default(lookup('pipe','hostname')) }}`
    -- one host per run -- so a role with NO `when:` gate (`initial_setup` itself among them)
    reaches every host the playbook is EVER run on, and the tick converging on daniel-box says
    nothing about the other two. Issue #1009: PR #1002 changed
    `roles/setup/initial_setup/files/kuma-push-lib.sh`, the tick converged, and land.sh read
    `settled` while daniel-server and daniel-pi kept running the old library.

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
        return frozenset(_HOSTS)
    return frozenset(h for h in _HOSTS if _eval_when(when, h, all_vars, host_vars_dir))


_SETUP_ROLES_DIR = ANSIBLE / "roles" / "setup"
_IMPORT_KEYS = ("ansible.builtin.import_tasks", "import_tasks")
_SHIPPED_DIRS = ("templates", "files")


def _task_gates_naming(
    role_dir: Path, basename: str, task_file: str = "main.yml", inherited: tuple = ()
) -> list[tuple] | None:
    """Every `when:` chain (import gates, then the task's own) on a task naming `basename`.

    Reads the role's `tasks/` tree through its static imports, and `block:` bodies. A
    task names the file when the string appears anywhere in its body -- `src:`, a
    `loop:` item, a `lookup('file', ...)` -- matched by basename, which is how every
    `template`/`copy` task in this tree refers to what it ships. Returns None when the
    task file cannot be read, so the caller falls back rather than narrows.
    """
    path = role_dir / "tasks" / task_file
    try:
        tasks = yaml_fast.safe_load(path.read_text()) or []
    except OSError, yaml.YAMLError:
        return None
    return _gates_in(tasks, role_dir, basename, inherited)


def _gates_in(tasks, role_dir: Path, basename: str, inherited: tuple) -> list[tuple]:
    found: list[tuple] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        chain = inherited + ((task["when"],) if "when" in task else ())
        target = next((task[k] for k in _IMPORT_KEYS if k in task), None)
        if isinstance(target, str):
            if "{{" in target:  # a cross-role import; nothing it ships is this role's
                continue
            found.extend(
                _task_gates_naming(role_dir, basename, Path(target).name, chain) or []
            )
        elif "block" in task:
            found.extend(_gates_in(task["block"], role_dir, basename, chain))
        elif basename in json.dumps(task):
            found.append(chain)
    return found


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

    Narrows only on evidence. A path outside `templates/` or `files/`, a file no task
    names, or a tasks tree that cannot be read all return the role-level answer, the same
    "unknown stays wide" asymmetry `_eval_when` applies inside one gate.
    """
    role_hosts = setup_role_hosts(role, playbook, all_vars, host_vars_dir)
    if path.endswith(".md"):
        # Docs ship nowhere: no task under roles/setup/*/tasks names a .md file, and the
        # deployer's k8s branch already reads *.md as docs. PR #1079 was three box-only
        # templates plus the role CLAUDE.md, and the CLAUDE.md alone reached every host.
        return frozenset()
    parts = Path(path).parts
    prefix = ("ansible", "roles", "setup", role)
    if not role_hosts or parts[: len(prefix)] != prefix or len(parts) < 6:
        return role_hosts
    if parts[4] not in _SHIPPED_DIRS:
        return role_hosts
    chains = _task_gates_naming(roles_dir / role, parts[-1])
    if not chains:
        return role_hosts
    return frozenset(
        h
        for h in role_hosts
        if any(
            all(_eval_when(g, h, all_vars, host_vars_dir) for g in chain)
            for chain in chains
        )
    )


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
    quiet = set(quiet)
    cs = services_from_changed_paths([p for p in files if p not in quiet])
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
