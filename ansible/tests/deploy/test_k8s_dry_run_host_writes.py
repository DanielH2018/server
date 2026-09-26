"""Guards on the HOST half of `k8s_dry_run`, the opt-in no-mutation mode for the k8s plane.

`test_k8s_dry_run.py` beside this file covers the cluster half. This one covers the node: a
k8s role's own tasks stage Python modules a pod mounts, write `/usr/local/bin` scripts, install
the crons that run them, and render probe-Job manifests under `/etc/rancher/k3s/manifests/`.
`--dry-run` promises the run changed nothing, host included.

Unguarded, any session's dry run ships its own tree's host half before that tree has landed —
past the health gate and past the auto-deploy denylist, which both watch the cluster. It is
also what kept `roles/setup/render_records` disarmed: an hourly fleet dry run would have
installed origin/master's host plane every hour (#2611, #2614).

The guard is `k8s_dry_run`, not `k8s_no_mutate`. `template`, `copy`, `file` and `cron` skip
their own writes under `--check` and report what would change, and the wider fact would turn
that diff into a skip.

What the census cannot see: a `command`/`shell` that writes the host through neither a file
module nor a redirect. claude-otel's `inject_dashboard_annotations.py --dest
/etc/rancher/k3s/dashboards-annotated` is that shape — guarded today through the include, and
unguarding it would not fail anything here. Recognising it needs a per-script rule, which is
worth writing the day a second one exists.
"""

import re
from pathlib import Path

from _helpers import REPO
from _k8s_guards import (
    _GUARD_FACT,
    _guard_covered_files,
    _role_with_tasks,
)
from lib import yaml_fast

_K8S_ROLES = REPO / "ansible/roles/k8s"


_HOST_WRITE_MODULES = (
    "ansible.builtin.template",
    "ansible.builtin.copy",
    "ansible.builtin.file",
    "ansible.builtin.cron",
    "ansible.builtin.lineinfile",
    "ansible.builtin.blockinfile",
    "ansible.builtin.assemble",
    "ansible.builtin.get_url",
    "ansible.builtin.unarchive",
    "ansible.posix.synchronize",
)

# A `command`/`shell` that redirects into an absolute path writes the host as surely as `copy`
# does. The four script-ConfigMap renders (monitor-bridge, autofix-bridge and the two stats
# roles) are exactly this shape: `kubectl create configmap --dry-run=client -o yaml > <file>`.
_HOST_REDIRECT = re.compile(r">>?\s*/(?:etc|opt|usr|var|srv)/")

# Roles whose host writes are the dry-run MECHANISM rather than a side effect of it.
# roles/k8s/manifests renders into a throwaway directory under the flag (test_k8s_dry_run.py
# pins that), and both issues scope themselves to writes "outside roles/k8s/manifests".
_HOST_WRITE_EXEMPT = frozenset({"manifests"})

# Non-vacuity. This census globs for its subjects, so a rename or a directory move would empty
# it and `assert not offenders` would pass over nothing (.claude/rules/python-layout.md). These
# roles each carry at least one host write today; if one stops appearing, the census broke
# rather than the role getting clean — check before editing the list.
_ROLES_THAT_WRITE_THE_HOST = frozenset(
    {
        "artifacts",
        "autofix-bridge",
        "claude-otel",
        "configarr",
        "crowdsec",
        "game-stats-lib",
        "headlamp",
        "image-builder",
        "janitorr",
        "media-volume",
        "monitor-bridge",
        "n8n",
        "netpol-baseline",
        "pi-peer-backup",
        "prowlarr",
        "qbittorrent",
        "registry",
        "terraria-stats",
        "traefik",
        "valheim-stats",
        "volume-claim",
    }
)


def _iter_tasks(tasks: object):
    """Every task dict in a task file, descending into block/rescue/always."""
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            yield from _iter_tasks(task.get(key))


def _writes_the_host(task: dict) -> bool:
    if any(module in task for module in _HOST_WRITE_MODULES):
        return True
    for key in ("ansible.builtin.command", "ansible.builtin.shell"):
        module = task.get(key)
        body = module.get("cmd", "") if isinstance(module, dict) else module
        if body and _HOST_REDIRECT.search(str(body)):
            return True
    return False


def _unguarded_host_writes(role: Path) -> list[str]:
    """Host writes in this role that no dry-run guard covers.

    The guard has to sit on the task itself or on the include that pulled its whole file in —
    `_guard_covered_files` is the same closure the cluster-side check uses, and it is what
    covers claude-otel's `dashboards.yml` and volume-claim's `claim.yml`.
    """
    covered = _guard_covered_files(role)
    offenders = []
    for task_file in sorted((role / "tasks").glob("*.yml")):
        if task_file.name in covered:
            continue
        for task in _iter_tasks(yaml_fast.safe_load(task_file.read_text())):
            if not _writes_the_host(task):
                continue
            if _GUARD_FACT.search(str(task.get("when", ""))):
                continue
            offenders.append(f"{role.name}/{task_file.name}: {task.get('name')}")
    return offenders


def _roles_with_host_writes() -> list[Path]:
    return [
        role
        for role in sorted(_K8S_ROLES.iterdir())
        if role.is_dir()
        and (role / "tasks").is_dir()
        and role.name not in _HOST_WRITE_EXEMPT
    ]


def test_every_host_write_outside_manifests_is_guarded() -> None:
    offenders = [
        w for role in _roles_with_host_writes() for w in _unguarded_host_writes(role)
    ]
    assert not offenders, (
        f"these tasks write the NODE on a dry run: {offenders}. Add "
        f"`when: not k8s_dry_run | bool`, as roles/k8s/artifacts does. `--check` needs no "
        "guard — template/copy/file/cron skip their own writes there, and guarding on "
        "k8s_no_mutate would throw away the would-change diff that makes --check useful."
    )


def test_the_host_write_census_is_not_empty() -> None:
    found = {
        role.name
        for role in _roles_with_host_writes()
        for task_file in sorted((role / "tasks").glob("*.yml"))
        for task in _iter_tasks(yaml_fast.safe_load(task_file.read_text()))
        if _writes_the_host(task)
    }
    missing = sorted(_ROLES_THAT_WRITE_THE_HOST - found)
    assert not missing, (
        f"the census no longer sees a host write in {missing}. Either the role really lost its "
        "host plane — then drop it from _ROLES_THAT_WRITE_THE_HOST — or the glob stopped "
        "matching and every assertion above is now passing over an empty set."
    )


_HOST_WRITE_TASK = (
    "- name: Install the widget cron script\n"
    "  tags: [cron]\n"
    "  ansible.builtin.template:\n"
    "    src: widget.sh.j2\n"
    "    dest: /usr/local/bin/widget.sh\n"
    '    mode: "0700"\n'
)


def test_an_unguarded_host_write_is_flagged(tmp_path: Path) -> None:
    role = _role_with_tasks(tmp_path, main=_HOST_WRITE_TASK)
    assert _unguarded_host_writes(role) == [
        "widget/main.yml: Install the widget cron script"
    ]


def test_a_guarded_host_write_is_clean(tmp_path: Path) -> None:
    role = _role_with_tasks(
        tmp_path / "a", main=_HOST_WRITE_TASK + "  when: not k8s_dry_run | bool\n"
    )
    assert _unguarded_host_writes(role) == []

    # And through a guarded include, claude-otel's shape.
    included = _role_with_tasks(
        tmp_path / "b",
        main=(
            "- name: Stage the widget\n"
            "  when: not k8s_dry_run | bool\n"
            "  ansible.builtin.include_tasks: stage.yml\n"
        ),
        stage=_HOST_WRITE_TASK,
    )
    assert _unguarded_host_writes(included) == []


def test_a_shell_redirect_into_a_host_path_counts_as_a_write(tmp_path: Path) -> None:
    """The four script-ConfigMap renders write the node with no file module in sight."""
    redirect = (
        "- name: Render the script ConfigMap manifest\n"
        "  ansible.builtin.shell:\n"
        "    cmd: >-\n"
        "      k3s kubectl create configmap widget-script --from-file=/tmp/w\n"
        "      --dry-run=client -o yaml > /etc/rancher/k3s/widget/configmap.yaml\n"
    )
    role = _role_with_tasks(tmp_path, main=redirect)
    assert _unguarded_host_writes(role) == [
        "widget/main.yml: Render the script ConfigMap manifest"
    ]
