"""The second host-write census row of target slice D5.1c: no host script writes a cluster
object.

It reads the scripts and modules that `_host_write_rows.py`'s host-write tasks install, and the
`templates/` and `files/` of `roles/setup/k8s_host_files/` once D5.1h creates that role. A
`kubectl` write there is a second writer beside Flux (contract C4). It sits apart from the
first row because together they pass the cap for a module. The plan names only the
`k8s_host_files` files. The derived half covers the same scripts before they move, so the row
has a subject from the day it lands and loses none of it at the move.
"""

import ast
import re

from _helpers import REPO
from _host_write_rows import _flippable_task_files, _host_write_tasks
from _row_table import Census, Subject, tracked

_HOST_FILES_ROLE = "ansible/roles/setup/k8s_host_files/"

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


def _callee(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _argv_write(elts: list[ast.expr], lineno: int) -> list[str]:
    found = _argv_subcommand(elts)
    if found and _is_write(*found):
        return [f"line {lineno}: kubectl {' '.join(filter(None, found))}"]
    return []


def _python_writes(text: str) -> list[str]:
    """Every kubectl write in a Python module, in the three shapes host scripts use.

    An argv list literal, with a `*NAME` splat of a list bound by assignment expanded in place;
    a call of a name bound to `host_lib.kubectl_runner(...)`, whose arguments follow kubectl;
    and a string that a shell would run. A bare string statement is a docstring and passes.
    """
    tree = ast.parse(text)
    lists: dict[str, list[ast.expr]] = {}
    runners: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target, value = node.targets[0], node.value
        if isinstance(target, ast.Name) and isinstance(value, (ast.List, ast.Tuple)):
            lists[target.id] = value.elts
        if isinstance(target, ast.Name) and isinstance(value, ast.Call):
            if _callee(value) == "kubectl_runner":
                runners.add(target.id)
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }

    def expand(elts: list[ast.expr]) -> list[ast.expr]:
        out = []
        for item in elts:
            splat = isinstance(item, ast.Starred) and isinstance(item.value, ast.Name)
            out += lists.get(item.value.id, [item]) if splat else [item]
        return out

    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            elts = expand(node.elts)
            for k, item in enumerate(elts):
                if isinstance(item, ast.Constant) and item.value == "kubectl":
                    hits += _argv_write(elts[k + 1 :], node.lineno)
        elif isinstance(node, ast.Call) and _callee(node) in runners:
            hits += _argv_write(node.args, node.lineno)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            hits += [
                f"line {node.lineno}: {node.value.strip()}"
                for m in _SHELL_KUBECTL.finditer(node.value)
                if _is_write(m["verb"], m["sub"])
            ]
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


# registry-gc.sh.j2's scale-down, held as a red case. The script's ghcr-mirror phase outlives
# registry's flip, so its allow entry below rests on the operator's ruling on #5732.
# ansible/tests/services/test_registry_gc_ghcr_mirror.py holds this line to the template's.
REGISTRY_GC_SCALE = '  $KUBECTL scale "deploy/${1}" --replicas=0 >/dev/null 2>&1\n'


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
        Subject("registry-gc.sh.j2", "#!/bin/bash\n" + REGISTRY_GC_SCALE),
        Subject("x.sh.j2", "k3s kubectl -n homelab rollout restart deploy/x\n"),
        Subject("x.sh", "${KUBECTL} --namespace=homelab annotate pod x a=b\n"),
        # A boolean flag must not swallow the verb as its value.
        Subject("x.sh", "kubectl --insecure-skip-tls-verify delete pod x\n"),
        Subject(
            "x.py", 'run([K3S, "kubectl", "-n", NAMESPACE, "patch", "deploy/x"])\n'
        ),
        Subject(
            "x.py",
            'kubectl = host_lib.kubectl_runner(KUBECTL, NS, 30)\nkubectl("delete", "pod", "x")\n',
        ),
        Subject("x.py", 'K = ["kubectl", "-n", NS]\nrun([*K, "delete", "job/x"])\n'),
        Subject("x.py", 'run("kubectl delete job x", shell=True)\n'),
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
    # move most of them to setup roles, so the floor and the member are the one only D5 moves.
    # pi-peer-backup's forced-command wrapper, the other, moved to `k8s_host_files` at its flip.
    min_matches=1,
    must_find=frozenset({_HOST_FILES_ROLE + "templates/registry-gc.sh.j2"}),
    allow={
        _HOST_FILES_ROLE + "templates/registry-gc.sh.j2": (
            "The script scales only deploy/ghcr-mirror, since D5 retired the push store's GC "
            "phase. ghcr-mirror.yaml.j2 renders no `replicas:`, so neither the Ansible apply "
            "nor Flux's server-side apply owns `spec.replicas`, the one field the script "
            "writes, and Flux reverts nothing it sets. The operator accepted this exception "
            "to C4 on that field ownership (#5732), so the mirror's GC stays a host script "
            "after the flip"
        ),
    },
)
