"""The two host-write census rows of target slice D5.1c: no k8s role writes a host, and no
host script writes a cluster object.

Flux applies a flux-owned role's manifests and runs none of its tasks, so a task that writes
a host stops running the moment its role flips, and nothing removes what it wrote. The plan is
`docs/target-architecture/deploy/d5-bulk-move.md`, *Host writes and concurrency (D5.1c)*. Each
row sits here rather than in `test_census_rows_roles.py`, which imports both into `ROWS`,
because that module is near the cap for a test module.

`HOST_WRITES` reads every task file of a k8s role that has a `containers_list` entry. A role
without one is a utility role that no tag flips, and the set is derived, never listed. Its
`allow` map names, per task file, the PR that moves the file's host writes. An entry holds only
while its role is Ansible-owned, so a flip PR cannot pass while its role still writes a host.

`HOST_SCRIPT_WRITES` reads the scripts and modules those host-write tasks install, and the
`templates/` and `files/` of `roles/setup/k8s_host_files/` once D5.1h creates that role. A
`kubectl` write there is a second writer beside Flux (contract C4). The plan names only the
`k8s_host_files` files. The derived half covers the same scripts before they move, so the row
has a subject from the day it lands and loses none of it at the move.
"""

import ast
import re

from _helpers import REPO, walk_tasks
from _row_table import Census, Subject, tracked
from lib import yaml_fast
from lib.k8s_roles import OWNER_VALUES, k8s_entries

_K8S = "ansible/roles/k8s/"
_HOST_FILES_ROLE = "ansible/roles/setup/k8s_host_files/"

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
# Everything after `kubectl … exec … --` runs inside a pod, so it writes no host.
_EXEC_TAIL = re.compile(r"\bkubectl\b.*?\bexec\b.*?\s--\s.*", re.S)


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


_H2_THEN_D5 = (
    "H2's family PR for the role moves the live crons and scripts, then D5's `k8s_host_files` "
    "conversion PR for the role moves the absent arms it leaves"
)
_CONFIGMAP = (
    "D5's Wave D PR for the role: the imperative ConfigMap's node staging under "
    "/etc/rancher/k3s/ becomes a rendered ConfigMap"
)
_UNNAMED = "#5266: the D5 plan names no PR that moves this write yet"

HOST_WRITE_ALLOW = {
    _K8S + "artifacts/tasks/main.yml": (
        f"{_H2_THEN_D5}, and its two directory tasks, which no H2 PR moves"
    ),
    _K8S + "autofix-bridge/tasks/main.yml": _CONFIGMAP,
    _K8S + "configarr/tasks/main.yml": _H2_THEN_D5,
    _K8S + "crowdsec/tasks/main.yml": _H2_THEN_D5,
    _K8S + "docs/tasks/main.yml": "D5's `k8s_host_files` conversion PR for docs",
    _K8S + "game-stats/tasks/main.yml": f"the record cleanup's absent arm: {_UNNAMED}",
    _K8S + "game-stats/tasks/stage.yml": _CONFIGMAP,
    _K8S + "game-stats/tasks/terraria.yml": _CONFIGMAP,
    _K8S + "game-stats/tasks/valheim.yml": _CONFIGMAP,
    _K8S + "janitorr/tasks/main.yml": _H2_THEN_D5,
    _K8S
    + "media-volume/tasks/main.yml": f"the media directories on the node: {_UNNAMED}",
    _K8S + "monitor-bridge/tasks/main.yml": _CONFIGMAP,
    _K8S + "observability/tasks/dashboards.yml": _CONFIGMAP,
    _K8S + "observability/tasks/main.yml": (
        "H2.7 moves the telemetry heartbeat and its cron and leaves no host row; the "
        f"claude-otel record cleanup is not one of H2.7's rows: {_UNNAMED}"
    ),
    _K8S + "pi-peer-backup/tasks/main.yml": (
        "pi-peer-backup's Wave C PR moves both daniel-pi writes to `k8s_host_files`"
    ),
    _K8S + "qbittorrent/tasks/main.yml": _H2_THEN_D5,
    _K8S + "registry/tasks/main.yml": (
        "D5's GC retirement (Wave D's registry bullet), whose absent arms then move with "
        "the rest"
    ),
    _K8S + "traefik/tasks/main.yml": (
        "D5's `k8s_host_files` conversion PR for traefik moves the Cloudflare drift absent "
        f"arms; the RBAC file outside {_MANIFESTS}: {_UNNAMED}"
    ),
}


def ansible_owned_allow(
    allow: dict[str, str], entries: dict[str, dict]
) -> dict[str, str]:
    """`allow` without the entries of a role whose `containers_list` entry sets `owner`.

    Flux runs no task of a flux-owned role, so its host writes would stop silently at the
    flip. Dropping its entry turns each into an offence the flip PR must move first.
    """
    return {
        rel: why
        for rel, why in allow.items()
        if entries.get(rel.split("/")[3], {}).get("owner") not in OWNER_VALUES
    }


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
    # The writes the three must_find members keep until D5's own PRs: docs's directory,
    # pi-peer-backup's two Pi writes and artifacts' two directory tasks.
    min_matches=5,
    # Only D5's PRs move these three, so the row fails when one stops matching rather than
    # when the selector drifts. Each D5 PR that moves a member drops it here in the same commit.
    must_find=frozenset(
        {
            _K8S + "artifacts/tasks/main.yml",
            _K8S + "docs/tasks/main.yml",
            _K8S + "pi-peer-backup/tasks/main.yml",
        }
    ),
    allow=ansible_owned_allow(HOST_WRITE_ALLOW, k8s_entries()),
    count=lambda subject: len(host_writes(subject)),
)

# ── No host script writes a cluster object ────────────────────────────────────────────

_WRITE_VERBS = frozenset(
    {
        "scale",
        "patch",
        "apply",
        "create",
        "delete",
        "annotate",
        "label",
        "replace",
        "set",
    }
)
# `kubectl`, or a shell variable holding it (`$KUBECTL`, `${KUBECTL}`), then any flags with
# their values, then the subcommand. A `kubectl exec … -- cscli delete` names `exec`.
_SHELL_KUBECTL = re.compile(
    r"(?:\bkubectl\b|\$\{?\w*KUBECTL\w*\}?)"
    r"(?:\s+-{1,2}[\w-]+(?:=\S+|\s+(?!-|(?:"
    + "|".join(_WRITE_VERBS)
    + r")\b)[^\s;|&]+)?)*"
    r"\s+(?P<verb>[a-z]+)(?:\s+(?P<sub>[a-z]+))?"
)


def _is_write(verb: str, sub: str | None) -> bool:
    return verb in _WRITE_VERBS or (verb == "rollout" and sub == "restart")


def _shell_writes(text: str) -> list[str]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        hits += [
            f"line {n}: {line.strip()}"
            for m in _SHELL_KUBECTL.finditer(line)
            if _is_write(m["verb"], m["sub"])
        ]
    return hits


def _argv_subcommand(items: list[ast.expr]) -> tuple[str, str | None] | None:
    """The subcommand after `kubectl` in an argv list literal, skipping flags and their values."""
    words = [i.value if isinstance(i, ast.Constant) else None for i in items]
    k = 0
    while k < len(words):
        word = words[k]
        if isinstance(word, str) and word.startswith("-"):
            # A flag's value is the next item when it is not itself a flag or a verb.
            value = words[k + 1] if k + 1 < len(words) else ""
            skip = "=" not in word and not (
                isinstance(value, str)
                and (value.startswith("-") or value in _WRITE_VERBS)
            )
            k += 2 if skip else 1
            continue
        if isinstance(word, str):
            nxt = words[k + 1] if k + 1 < len(words) else None
            return word, nxt if isinstance(nxt, str) else None
        return None
    return None


def _python_writes(text: str) -> list[str]:
    hits = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        for k, item in enumerate(node.elts):
            if isinstance(item, ast.Constant) and item.value == "kubectl":
                found = _argv_subcommand(node.elts[k + 1 :])
                if found and _is_write(*found):
                    hits.append(
                        f"line {node.lineno}: kubectl {' '.join(filter(None, found))}"
                    )
    return hits


def kubectl_writes(subject: Subject) -> list[str]:
    """Every `kubectl` call in `subject` whose subcommand writes a cluster object."""
    if subject.rel.endswith(".py"):
        return _python_writes(subject.text)
    return _shell_writes(subject.text)


def _installed_sources(task_file: str) -> list[str]:
    """The role files the host-write `copy` and `template` tasks in `task_file` install.

    A `src` resolves against the role's `templates/` or `files/`, or through `{{ role_path }}`,
    with `{{ item }}` expanded over a literal `loop`. A src left templated after that names no
    one file and is skipped.
    """
    role = "/".join(task_file.split("/")[:4])
    found = []
    for task, module, args in _host_write_tasks(Subject(task_file)):
        if module not in ("copy", "template") or not isinstance(args, dict):
            continue
        src = str(args.get("src", "")).replace("{{ role_path }}", role)
        loop = task.get("loop")
        items = loop if isinstance(loop, list) and "{{ item }}" in src else [None]
        for item in items:
            path = src if item is None else src.replace("{{ item }}", str(item))
            if "{{" in path or not path:
                continue
            if not path.startswith(role):
                path = (
                    f"{role}/{'templates' if module == 'template' else 'files'}/{path}"
                )
            found.append(path)
    return found


def _host_scripts() -> list[str]:
    installed = {
        path for rel in _flippable_task_files() for path in _installed_sources(rel)
    }
    return sorted(
        {rel for rel in installed if (REPO / rel).is_file()}
        | set(tracked(_HOST_FILES_ROLE + "templates/*", _HOST_FILES_ROLE + "files/*"))
    )


# The scale-down registry-gc.sh.j2 runs for its GC, held here because the template goes with
# the GC (Wave D's registry bullet).
_REGISTRY_GC_SCALE = (
    '  $KUBECTL scale "deploy/${DEPLOY}" --replicas=0 >/dev/null 2>&1\n'
)

HOST_SCRIPT_WRITES = Census(
    name="host-scripts-write-no-cluster-object",
    reason=(
        "A host script that writes an object Flux renders is a second writer under contract "
        "C4: Flux reverts it at the next reconcile, or it reverts Flux. `kubectl exec` into an "
        "app writes no object and passes. Move the write into the cluster as a Job the role "
        "renders, or retire it, as D5 retires the registry GC rather than moving it."
    ),
    files=_host_scripts,
    offence=kubectl_writes,
    red=(
        Subject("registry-gc.sh.j2", "#!/bin/bash\n" + _REGISTRY_GC_SCALE),
        Subject("x.sh.j2", "k3s kubectl -n homelab rollout restart deploy/x\n"),
        Subject("x.sh", "${KUBECTL} --namespace=homelab annotate pod x a=b\n"),
        # A boolean flag must not swallow the verb as its value.
        Subject("x.sh", "kubectl --insecure-skip-tls-verify delete pod x\n"),
        Subject(
            "x.py", 'run([K3S, "kubectl", "-n", NAMESPACE, "patch", "deploy/x"])\n'
        ),
    ),
    green=(
        Subject(
            "crowdsec.sh.j2",
            "/usr/local/bin/k3s kubectl -n homelab exec deploy/crowdsec -c crowdsec -- "
            'cscli decisions delete --ip "$IP"\n',
        ),
        Subject("x.sh", '$KUBECTL rollout status "deploy/x" --timeout=180s\n'),
        Subject("x.sh", '# kubectl delete job x, by hand\n$KUBECTL logs "job/x"\n'),
        Subject(
            "x.py",
            '"""Made with `kubectl create job`."""\n'
            'run([K3S, "kubectl", "-n", NS, "exec", "deploy/x", "--", "cscli", "delete"])\n',
        ),
    ),
    # 24 installed scripts, modules and files when the row landed (2026-10-16). H2's family PRs
    # move most of them to setup roles, so the floor and the members are the two only D5 moves.
    min_matches=2,
    must_find=frozenset(
        {
            _K8S + "pi-peer-backup/files/pi-peer-backup-shell.sh",
            _K8S + "registry/templates/registry-gc.sh.j2",
        }
    ),
    allow={
        _K8S + "registry/templates/registry-gc.sh.j2": (
            "D5 retires the registry GC before registry flips (Wave D's registry bullet, Open "
            "question 15), and the template goes with it"
        ),
    },
)
