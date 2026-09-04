#!/usr/bin/env python3
"""The two cron-environment rules a rendered shell template must satisfy.

cron inherits neither PATH nor KUBECONFIG, and the two omissions fail differently. A missing
PATH means `k3s` does not resolve, so the script dies loudly. A missing KUBECONFIG resolves
the binary and then reports an EMPTY cluster — zero pods, zero volumes, nothing wrong — so a
health check built on it goes green while seeing nothing at all. One check cannot cover both.

Both rules take a rendered script plus the cron resolution from
`scripts/lib/cron_targets.py`, and return an error string or None.
`scripts/validate/shell_templates.py` runs them over every template it renders.
"""

import sys
from pathlib import Path

# A directly-invoked script gets only its own directory on sys.path, and pyproject's
# `pythonpath` is a pytest setting — so the cross-directory imports below need the
# scripts/ root here, the same way its siblings reach it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cron_targets import (
    BARE_K8S_INVOCATION,
    CRON_ROOT_USER,
    CRONTAB_KUBECONFIG_ENV,
    CRONTAB_PATH_ENV,
    K8S_INVOCATION_ANY,
    KUBECONFIG_ASSIGNED,
    PATH_EXPORT_INCLUDES_LOCAL_BIN,
    iter_cron_targets,
    strip_comments,
)
from lib.repo_paths import REPO, ROLES


def cron_path_error(
    template: Path, rendered: str, cron_map: dict[Path, Path]
) -> str | None:
    """Return an error string if `template` is a cron target with a bare, PATH-unfixed k8s call.

    None if this template is fine. "No PATH fix" means neither an in-script export nor a
    crontab PATH line covers the bare `kubectl`/`k3s` invocation.
    """
    task_file = cron_map.get(template)
    if task_file is None:
        return None  # not a cron job target at all — out of scope for this rule

    code = strip_comments(rendered)
    if PATH_EXPORT_INCLUDES_LOCAL_BIN.search(code):
        return None
    if not BARE_K8S_INVOCATION.search(code):
        return None
    if CRONTAB_PATH_ENV.search(task_file.read_text()):
        return None

    try:
        rel_task_file = task_file.relative_to(REPO)
    except ValueError:
        rel_task_file = (
            task_file  # e.g. a unit test's tmp_path fixture, not a real repo path
        )
    return (
        "calls kubectl/k3s bare but is a cron job target "
        f"({rel_task_file}) — cron's PATH omits /usr/local/bin. Add "
        '`export PATH="/usr/local/bin:${PATH}"` (see longhorn-trim-volumes.sh.j2), call k3s/'
        "kubectl by absolute path, or set a crontab-level PATH via env: yes on the cron task."
    )


def cron_kubeconfig_error(
    template: Path, rendered: str, roles: Path = ROLES
) -> str | None:
    """Return an error string if a NON-ROOT cron target touches the cluster with no KUBECONFIG.

    None if this template is fine. The sibling of `cron_path_error`, and the trap its own memory entry predicted would still be
    live once the PATH half was fixed: "cron inherits neither PATH nor KUBECONFIG". The two fail
    differently, which is why one check cannot cover both. A missing PATH makes k3s not resolve,
    so the script dies loudly. A missing KUBECONFIG resolves the binary fine and then reports an
    EMPTY CLUSTER — zero pods, zero volumes, nothing wrong — so a health check built on it goes
    green while seeing nothing at all.

    Root is exempt because k3s writes /etc/rancher/k3s/k3s.yaml as 0640 root:root and reads it
    by default; `ubuntu` cannot open it. `ansible.builtin.cron` defaults `user` to the connection
    user, so a task with no `user:` is treated as non-root and must set KUBECONFIG.

    The user is read from the SPECIFIC cron task scheduling this template, never by searching the
    task file. health-crons.yml schedules eight crons with different users, so a file-wide search
    would let one task's `user: root` excuse a sibling non-root task — the guard reading green on
    exactly the case it exists to catch.

    Verify a suspected instance by running the wrapper the way cron does:
    `scripts/dev/run_as_cron.sh --expect-output /usr/local/bin/<wrapper>.sh`, which exits 66 on
    the clean-exit-no-output signature this fault produces.
    """
    code = strip_comments(rendered)
    if not K8S_INVOCATION_ANY.search(code):
        return None

    for tpl, task_file, cron_task, job_env in iter_cron_targets(roles):
        if tpl != template:
            continue
        if str(cron_task.get("user", "")).strip() == CRON_ROOT_USER:
            return None
        if "KUBECONFIG=" in job_env:
            return None
        if KUBECONFIG_ASSIGNED.search(code):
            return None
        if CRONTAB_KUBECONFIG_ENV.search(task_file.read_text()):
            return None
        try:
            rel_task_file = task_file.relative_to(REPO)
        except ValueError:
            rel_task_file = task_file
        user = (
            str(cron_task.get("user", "")).strip() or "the connection user (non-root)"
        )
        return (
            f"touches the cluster but is scheduled as {user} ({rel_task_file}) with no "
            "KUBECONFIG — cron does not inherit one, and k3s.yaml is root-only, so this "
            "reports an EMPTY cluster rather than failing. Set KUBECONFIG in the script, in "
            "the cron job: line, or via env: yes on the cron task — or schedule it as root "
            "(see crowdsec-appsec-verify.sh.j2)."
        )
    return None
