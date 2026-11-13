"""The repo path anchors a script under ``scripts/`` reads the Ansible tree through.

WHY A MODULE WITH NO IMPORTS. ``render_guard.py`` was meant to be the one definition of
these, but importing it costs a ``jinja2`` and a ``yaml`` import, so a script that only
needed a path kept computing ``Path(__file__).resolve().parents[2]`` itself -- twenty of
them by 2026-09-01, under three different names (``REPO``, ``REPO_ROOT``, ``_REPO``). This
module imports nothing, so there is no reason left to inline the arithmetic.

Every path is absolute and resolved, so a script run from any cwd -- a cron, a prek hook,
a worktree under ``.claude/worktrees/`` -- reads the checkout it lives in, never the one
the shell happens to be in. That is the property a landing relies on when it runs
from the worktree it was invoked from.

Import it through the same bootstrap as any other ``lib`` module::

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from lib.repo_paths import REPO, HOST_VARS  # noqa: E402

A test that needs to point a reader at a ``tmp_path`` passes the path as an argument; these
constants are defaults, never the only way in.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def primary_checkout_of(repo: Path) -> Path:
    """The checkout the worktrees hang off, read from ``repo``'s ``.git`` file.

    A linked worktree's ``.git`` is a file reading ``gitdir: <primary>/.git/worktrees/<name>``,
    and the primary is three parents up. Any other shape (a primary checkout, a separated
    metadata directory, no ``.git`` at all) answers ``repo`` itself. It starts no subprocess,
    so a module that needs the path at import time pays one file read.
    """
    dotgit = repo / ".git"
    try:
        text = dotgit.read_text(encoding="utf-8").strip() if dotgit.is_file() else ""
    except OSError:
        return repo
    if not text.startswith("gitdir:"):
        return repo
    gitdir = repo / text.removeprefix("gitdir:").strip()
    if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
        return gitdir.parent.parent.parent
    return repo


# The checkout every landing, deploy and fan-out runs its commands in, even when the script that
# asks lives in a worktree under it. It is /home/ubuntu/server for the operator and the clone
# under its own home for the `claude` user.
PRIMARY_CHECKOUT = primary_checkout_of(REPO)
SCRIPTS = REPO / "scripts"
DOCS = REPO / "docs"

ANSIBLE = REPO / "ansible"
ROLES = ANSIBLE / "roles"
K8S_ROLES = ROLES / "k8s"
JOB_FILES = SCRIPTS / "renderer" / "job_files"
# `longhorn_robustness.py`'s one copy lives in monitor-bridge's `files/` without shipping in its
# pod; the runbook gates import it from here (#3668).
MONITOR_BRIDGE_FILES = K8S_ROLES / "monitor-bridge" / "files"
# `host_lib.py` and `ci_verdict.py`, the modules the host packages ship beside a host script (H7.2e moved them out of `setup/common/files/`). A tool that
# imports one of the deployer's IMPURE modules (`deploy_state`, `deploy_config`) puts this on
# `sys.path` for `host_lib`. A tool that imports `deploy_logic` puts it there for `ci_verdict`,
# which `deploy_git` imports. `deploy_logic` itself must not load `host_lib`, which
# `test_deploy_logic_imports_without_host_lib` enforces by `sys.modules`.
HOST_LIB_FILES = REPO / "hosts" / "lib"
SHARED_TPL = ANSIBLE / "templates"  # shared macros

INVENTORY = ANSIBLE / "inventory"
ALL_VARS = INVENTORY / "group_vars" / "all.yml"
HOST_VARS = INVENTORY / "host_vars"
# The k3s service list, every template's `services` (render_guard.k8s_services).
K8S_SERVICES = REPO / "cluster" / "services.yml"
# The k3s server component's defaults hold the node keys both k3s components render: the join
# ports, the read-only ServiceAccount, the address pin. The Longhorn backup schedule and arm flags
# are `LONGHORN_BACKUP_ROLE`'s.
K3S_SERVER = REPO / "hosts" / "components" / "k3s-server"
# The three Longhorn tier lists, `k3s_longhorn_{r2,weekly,nobackup}_volumes`, live in this
# role's defaults since H1.7a. Python readers resolve them through `lib.service_tiers`.
LONGHORN_RECONCILE_ROLE = K8S_ROLES / "longhorn-reconcile"
# The Longhorn arm flags, backup schedules, retain counts and targets' prefixes live in this role's
# defaults since H1.7b.
LONGHORN_BACKUP_ROLE = K8S_ROLES / "longhorn-backup"
K3S_DEFAULTS = K3S_SERVER / "defaults" / "main.yml"
# The k3s pin (`k3s_version`, `k3s_install_script_url`, `k3s_install_script_sha256`) lives in the
# Flux-owned `system-upgrade` role's defaults since H6.1b, so a pin change routes to that tag.
# `K3S_DEFAULTS` keeps every other key, which did not move.
SYSTEM_UPGRADE_DEFAULTS = K8S_ROLES / "system-upgrade" / "defaults" / "main.yml"
# The component holding the host checks since H7.35: their schedules and thresholds are in its
# `defaults/main.yml`, and its `files/` holds `longhorn_backup_health.py`. `longhorn_lib/`, which
# that reader imports, lives in `scripts/lib/` since H7.2e.
K3S_HOST_CHECKS = REPO / "hosts" / "components" / "k3s-host-checks"
K3S_HOST_CHECKS_DEFAULTS = K3S_HOST_CHECKS / "defaults" / "main.yml"
K3S_HOST_DNS = REPO / "hosts" / "components" / "k3s-host-dns"
K3S_FILES = K3S_HOST_CHECKS / "files"
# The suite's shared guard modules (`_ratchet.py`, `_helpers.py`), for a script that reuses one.
ANSIBLE_TESTS = ANSIBLE / "tests"

# The bare module names a script outside the roles imports from each role directory, by the
# directory constant. `lib/stdlib_only.py`'s walk refuses an import from a role directory, or
# of a basename two of them share, that this map does not list.
ROLE_MODULES = {
    MONITOR_BRIDGE_FILES: ["longhorn_robustness"],
}
