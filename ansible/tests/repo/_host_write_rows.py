"""The host-write census row of target slice D5.1c: no k8s role writes a host.

Flux applies a flux-owned role's manifests and runs none of its tasks, so a task that writes
a host stops running the moment its role flips, and nothing removes what it wrote. The plan is
`docs/target-architecture/deploy/d5-bulk-move.md`, *Host writes and concurrency (D5.1c)*. The
row sits in its own module because `test_census_rows_roles.py`, which imports it into `ROWS`,
is near the cap for a test module.

`HOST_WRITES` reads every task file of a k8s role that has a `containers_list` entry. A role
without one is a utility role that no tag flips, and the set is derived, never listed. Its
`allow` map names, per task file, the PR that moves the file's host writes. A flip PR cannot
pass while its role keeps an entry there: `flipped_allow_entries` names any entry whose role's
`containers_list` entry sets `owner`, and `test_host_write_rows.py` fails on one.

The second D5.1c row, `HOST_SCRIPT_WRITES` in `_host_script_row.py`, reads the files these
tasks install.
"""

import re

from _helpers import walk_tasks
from _row_table import Census, Subject, tracked
from lib import yaml_fast
from lib.k8s_roles import OWNER_VALUES, k8s_entries

_K8S = "ansible/roles/k8s/"

# ── No k8s role writes a host ─────────────────────────────────────────────────────────

_ALWAYS = frozenset({"cron", "authorized_key"})
_DESTINED = frozenset(
    {
        "copy",
        "template",
        "file",
        "lineinfile",
        "blockinfile",
        "get_url",
        "unarchive",
        "systemd",
        "systemd_service",
        "service",
    }
)
_COMMANDS = frozenset({"command", "shell"})
# The one host directory a k8s role may write: the manifests role's render, which Flux's
# bundle replaces once the role flips.
_MANIFESTS = "/etc/rancher/k3s/manifests/"
# A command that writes a file: one of the six writing tools as a word, or an output redirect
# other than to /dev/null or onto another descriptor (`2>&1`, `>&2`).
_WRITES_A_FILE = re.compile(
    r"(?<![\w./-])(?:install|tee|cp|mv|mkdir|ln)(?![\w-])"
    r"|(?<![\d&<>=-])>>?(?!&|>|\s*/dev/null)"
)
# What follows `kubectl … exec … --` runs inside a pod, so it writes no host: an `sh -c` or
# `bash -c` with its quoted script, or else the words up to the end of that shell command. A
# `;`, `&`, `|`, newline or redirect after it is the host shell's again.
_EXEC_TAIL = re.compile(
    r"\bkubectl\b[^;|&\n]*?\bexec\b[^;|&\n]*?\s--\s+"
    r"""(?:(?:ba)?sh\s+-c\s+(?:"(?:\\.|[^"\\])*"|'[^']*')|[^;|&\n>]*)"""
)


def _command_text(args) -> str:
    if isinstance(args, str):
        return args
    if isinstance(args, dict):
        argv = args.get("argv")
        return " ".join(map(str, argv)) if argv else str(args.get("cmd", ""))
    return ""


def _destination(args) -> str | None:
    """The host path a destined module writes, or None for one that names none (`systemd`)."""
    if isinstance(args, str):
        found = re.search(r"\b(?:dest|path)=(\S+)", args)
        return found.group(1) if found else None
    if isinstance(args, dict):
        return args.get("dest") or args.get("path")
    return None


def _host_write_tasks(subject: Subject) -> list[tuple[dict, str, object]]:
    """(task, module short name, module args) for every task in `subject` that writes a host."""
    found = []
    for task in walk_tasks(yaml_fast.safe_load(subject.text) or []):
        for key, args in task.items():
            module = key.rsplit(".", 1)[-1]
            if module in _ALWAYS:
                found.append((task, module, args))
            elif module in _DESTINED:
                dest = _destination(args)
                if not (isinstance(dest, str) and dest.startswith(_MANIFESTS)):
                    found.append((task, module, args))
            elif module in _COMMANDS:
                if _WRITES_A_FILE.search(_EXEC_TAIL.sub("", _command_text(args))):
                    found.append((task, module, args))
    return found


def host_writes(subject: Subject) -> list[str]:
    """`<task name>: <module>` for every task in `subject` that writes a host."""
    return [
        f"{task.get('name', '<unnamed>')}: {module}"
        for task, module, _args in _host_write_tasks(subject)
    ]


def _flippable_task_files() -> list[str]:
    """Every task file of a k8s role that has a `containers_list` entry."""
    entries = k8s_entries()
    return [
        rel for rel in tracked(_K8S + "*/tasks/*.yml") if rel.split("/")[3] in entries
    ]


HOST_WRITE_ALLOW = {
    _K8S + "registry/tasks/main.yml": (
        "D5's GC retirement (Wave D's registry bullet), whose absent arms then move with "
        "the rest"
    ),
}


def flipped_allow_entries(allow: dict[str, str], entries: dict[str, dict]) -> list[str]:
    """Every key of `allow` whose role's `containers_list` entry sets `owner`.

    Flux runs no task of a flux-owned role, so its host writes would stop silently at the
    flip. The flip PR moves them and drops the entry. An entry left behind would allow a write
    re-added after a handback, so the entry itself is refused, written or not.
    """
    return sorted(
        rel
        for rel in allow
        if entries.get(rel.split("/")[3], {}).get("owner") in OWNER_VALUES
    )


HOST_WRITES = Census(
    name="k8s-role-tasks-write-no-host",
    reason=(
        "Flux applies a flux-owned role's manifests and runs none of its tasks, so a host write "
        "in a k8s role stops at the flip and nothing removes what it wrote. A cron keeps "
        "running beside the flux-owned workload; an absent arm never runs. Move the write to "
        "`roles/setup/k8s_host_files/` (D5's conversion PR) or render it, then drop the "
        f"role's allow entry. Only {_MANIFESTS} is exempt, and only for the manifests render. "
        "`state: absent` and `state: directory` are host writes too."
    ),
    files=_flippable_task_files,
    offence=host_writes,
    red=(
        Subject(
            _K8S + "newsvc/tasks/main.yml",
            "- name: Schedule the heartbeat\n  ansible.builtin.cron:\n"
            "    name: heartbeat\n    job: /usr/local/bin/heartbeat.sh\n",
        ),
        Subject(
            _K8S + "newsvc/tasks/main.yml",
            "- name: Remove the heartbeat cron\n  ansible.builtin.cron:\n"
            "    name: heartbeat\n    state: absent\n",
        ),
        Subject(
            _K8S + "newsvc/tasks/main.yml",
            "- name: Install the helper\n  ansible.builtin.shell: >-\n"
            "    install -m 0755 /tmp/helper.sh /usr/local/bin/helper.sh\n",
        ),
        Subject(
            _K8S + "newsvc/tasks/main.yml",
            "- block:\n    - name: Ensure the peer directory\n"
            "      ansible.builtin.file:\n        path: /srv/peer\n"
            "        state: directory\n      delegate_to: daniel-pi\n",
        ),
        Subject(
            _K8S + "newsvc/tasks/main.yml",
            "- name: Write the state\n  ansible.builtin.command:\n"
            "    cmd: sh -c 'date > /var/lib/newsvc/stamp'\n",
        ),
        # The host shell's redirect after an exec, and a host command on the next line.
        Subject(
            _K8S + "newsvc/tasks/main.yml",
            "- name: Copy out\n  ansible.builtin.shell: >-\n"
            "    k3s kubectl -n x exec pod -- cat /f > /var/lib/x/f\n",
        ),
        Subject(
            _K8S + "newsvc/tasks/main.yml",
            "- name: Two steps\n  ansible.builtin.shell: |\n"
            "    k3s kubectl -n x exec pod -- true\n"
            "    install -m 0755 /tmp/a /usr/local/bin/a\n",
        ),
    ),
    green=(
        Subject(
            _K8S + "newsvc/tasks/main.yml",
            "- name: Render the manifest\n  ansible.builtin.template:\n"
            "    src: x.yaml.j2\n    dest: /etc/rancher/k3s/manifests/newsvc/x.yaml\n",
        ),
        # A `delegate_to` alone is no offence: release_digest.yml's reads pass.
        Subject(
            _K8S + "newsvc/tasks/main.yml",
            "- name: Resolve the deploy tag\n  ansible.builtin.command: git rev-parse HEAD\n"
            "  delegate_to: localhost\n",
        ),
        # janitorr's verify writes its probe symlink inside the pod, not on the host.
        Subject(
            _K8S + "newsvc/tasks/verify.yml",
            "- name: Write a probe symlink\n  ansible.builtin.command:\n"
            "    cmd: >-\n      k3s kubectl -n homelab exec pod -- bash -c\n"
            '      "mkdir -p /data/x; ln -sfn /data/a /data/x/b"\n',
        ),
        Subject(
            _K8S + "newsvc/tasks/main.yml",
            "- name: Read the pod\n  ansible.builtin.shell: >-\n"
            "    k3s kubectl get pod x -o name 2>/dev/null >/dev/null 2>&1\n",
        ),
    ),
    # The writes the must_find member keeps until its D5 PR: registry's GC cron, its scripts
    # and their absent arms. docs's, artifacts' and media-volume's directories and
    # pi-peer-backup's two Pi writes moved to `k8s_host_files`, and traefik's flip rendered
    # its RBAC staging into the bundle.
    min_matches=3,
    # Only a D5 PR moves this member, so the row fails when it stops matching rather than when
    # the selector drifts. The D5 PR that moves a member drops it here in the same commit.
    must_find=frozenset({_K8S + "registry/tasks/main.yml"}),
    allow=HOST_WRITE_ALLOW,
    count=lambda subject: len(host_writes(subject)),
)
