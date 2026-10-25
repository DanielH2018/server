"""The repo path anchors a script under ``scripts/`` reads the Ansible tree through.

WHY A MODULE WITH NO IMPORTS. ``render_guard.py`` was meant to be the one definition of
these, but importing it costs a ``jinja2`` and a ``yaml`` import, so a script that only
needed a path kept computing ``Path(__file__).resolve().parents[2]`` itself -- twenty of
them by 2026-09-01, under three different names (``REPO``, ``REPO_ROOT``, ``_REPO``). This
module imports nothing, so there is no reason left to inline the arithmetic.

Every path is absolute and resolved, so a script run from any cwd -- a cron, a prek hook,
a worktree under ``.claude/worktrees/`` -- reads the checkout it lives in, never the one
the shell happens to be in. That is the property ``deploy.sh`` relies on when it renders
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
FILTER_PLUGINS = ANSIBLE / "filter_plugins"
# deploy_logic.py lives in the role that deploys it, so a script that has to agree with the
# deployer imports it from here rather than carrying a second derivation.
GITOPS_DEPLOY_FILES = ROLES / "setup" / "gitops_deploy" / "files"
# `longhorn_robustness.py` ships in monitor-bridge's pod, so its one copy lives in that role's
# `files/`; the runbook gates import it from here (#3668).
MONITOR_BRIDGE_FILES = K8S_ROLES / "monitor-bridge" / "files"
# `host_lib.py`, `kuma_push.py` and `ci_verdict.py`, the modules the host packages and the setup
# roles ship beside a host script (H7.2e moved them out of `setup/common/files/`). A tool that
# imports one of the deployer's IMPURE modules (`deploy_state`, `deploy_config`) puts this on
# `sys.path` for `host_lib`. A tool that imports `deploy_logic` puts it there for `ci_verdict`,
# which `deploy_git` imports. `deploy_logic` itself must not load `host_lib`, which
# `test_deploy_logic_imports_without_host_lib` enforces by `sys.modules`.
HOST_LIB_FILES = REPO / "hosts" / "lib"
SHARED_TPL = ANSIBLE / "templates"  # shared macros

INVENTORY = ANSIBLE / "inventory"
HOSTS_INI = INVENTORY / "hosts.ini"
ALL_VARS = INVENTORY / "group_vars" / "all.yml"
HOST_VARS = INVENTORY / "host_vars"
# The k3s role's defaults: the host crons' schedules and thresholds, the etcd snapshot
# retention and the schema version the manifest validator pins all live here. The Longhorn
# backup schedule and arm flags are `LONGHORN_BACKUP_ROLE`'s.
K3S_ROLE = ROLES / "setup" / "k3s"
# The three Longhorn tier lists, `k3s_longhorn_{r2,weekly,nobackup}_volumes`, live in this
# role's defaults since H1.7a. Python readers resolve them through `lib.service_tiers`.
LONGHORN_RECONCILE_ROLE = K8S_ROLES / "longhorn-reconcile"
# The Longhorn arm flags, backup schedules, retain counts and targets' prefixes live in this role's
# defaults since H1.7b. The k3s role's defaults keep the host crons' keys.
LONGHORN_BACKUP_ROLE = K8S_ROLES / "longhorn-backup"
K3S_DEFAULTS = K3S_ROLE / "defaults" / "main.yml"
# The k3s pin (`k3s_version`, `k3s_install_script_url`, `k3s_install_script_sha256`) lives in the
# Flux-owned `system-upgrade` role's defaults since H6.1b, so a pin change routes to that tag.
# `K3S_DEFAULTS` keeps every other key, which did not move.
SYSTEM_UPGRADE_DEFAULTS = K8S_ROLES / "system-upgrade" / "defaults" / "main.yml"
# The k3s role's own Python, which the host crons run: `longhorn_backup_health.py`,
# `live_drift_check.py` and `manifest_declares.py`. `longhorn_lib/`, which the first imports,
# lives in `scripts/lib/` since H7.2e.
K3S_FILES = K3S_ROLE / "files"
# The suite's shared guard modules (`_ratchet.py`, `_helpers.py`), for a script that reuses one.
ANSIBLE_TESTS = ANSIBLE / "tests"

# The bare module names a script outside the roles imports from each role directory, by the
# directory constant. A bare name is not decidable from `sys.path` alone: `gitops_hold`,
# `gitops_markers` and `gitops_ledger` ship in both GITOPS_DEPLOY_FILES and
# MONITOR_BRIDGE_FILES with different content. `lib/stdlib_only.py`'s walk refuses an import
# from a role directory, or of a basename two of them share, that this map does not list.
ROLE_MODULES = {
    GITOPS_DEPLOY_FILES: ["gitops_hold", "gitops_ledger", "gitops_markers"],
    MONITOR_BRIDGE_FILES: ["longhorn_robustness"],
}
