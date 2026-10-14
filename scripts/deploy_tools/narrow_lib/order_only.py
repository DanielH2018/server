"""Whether a filter plugin's change only reorders the play, judged by running it (#4781, #4842).

`digest_covers.py` asks this of a changed filter plugin: a change that moves only the order the
k8s play deploys roles in, never an entry a role reads, lets the render proof discharge a line
it wrote. The rule runs the play's sort chain twice in a subprocess, once with
`ansible/filter_plugins/` as of each end of the range, on the same data: the k8s host's
`containers_list` and the role tree, both as of the range's HEAD. Holding the data fixed and
varying only the code keeps an inventory edit elsewhere in the range out of the comparison.

A module of its own because it runs Python where the rest of `digest_covers.py` reads YAML,
and it imports nothing from that file.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

import json
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from functools import lru_cache
from pathlib import Path

from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST
from lib.git import git

# DECIDED: a `toposort.py` change is order-only when running the k8s sort chain at both ends
# yields the same entries and leaves its input untouched (#4842). `deploy.yml` reads the
# plugin as `filter_by_platform('k8s')`, then `build_k8s_dep_map` into `toposort_containers`,
# whose result becomes the `containers_list` every role reads through `item`; and as
# `filter_by_platform('docker')` for the Docker play. So a change whose outputs are a
# permutation of identical entries, and that writes no entry in place, moves deploy order and
# nothing a role does. #4841 and #4844 read the code's syntax for writes instead, and each
# review found a new aliasing bypass: Python mutation cannot be tracked soundly from syntax.
# Running the code imports it, which `filters.py` never does; here both ends are commits the
# deploy itself imports, under the same interpreter. A side effect outside the returned entries
# that leaves the process, such as a file write, an environment change or a child process, ends
# the run through the runner's audit hook, and a path that appears under the temp tree fails
# it too (#4846). The gaps taken: a branch HEAD's data does not exercise, a write into another
# imported module's state, which no audit event reports and which persists in the controller
# process in a deploy (#4995), a call that raises no audit event and creates a file at an
# absolute path outside the temp tree (#4999), and a role whose bytes depend on list order,
# which the render proof the caller still requires catches.
ORDER_ONLY_PLUGINS = frozenset({"ansible/filter_plugins/toposort.py"})

_PLUGINS = "ansible/filter_plugins"
_ROLES = "ansible/roles"
_TIMEOUT = 60

# Runs in `python -I -B`, so neither PYTHONPATH nor the caller's cwd reaches the import, and
# the import writes no `__pycache__`. Resolves each filter through `FilterModule().filters()`,
# as Ansible does, so a re-pointed registration is the code that runs.
#
# An audit hook, installed before the import so the plugin's module-level code runs under it
# too, ends the process on any event outside the allowlist below, and on an `open` whose flags
# write or create (#4846). The allowlist is every event the real chain raises; an event it
# misses, after an interpreter or Ansible upgrade, reads as doubt and keeps the line rather than
# discharging it. An allowlist, because each denylist review found another mutating event.
#
# The plugin is imported after the hook and can reach anything `__main__`, `os` or `builtins`
# holds, so the hook reads nothing from them: `_install` binds the exit function and the tables
# as closure cells, and is deleted before the import. Only the interpreter's hook list then
# refers to the hook, and the `gc` events that would walk to it are off the allowlist. The hook exits with
# `os._exit` rather than raising, because a raise lands in the plugin's frame, where an `except`
# can swallow it. `_run` reads the nonzero exit as doubt.
_RUNNER = """
import copy, json, os, sys


def _install(exit_, write_flags, allowed):
    def audit(event, args):
        try:
            if event == "open":
                if args[2] & write_flags:
                    exit_(86)
            elif event not in allowed:
                exit_(86)
        except BaseException:
            exit_(86)

    sys.addaudithook(audit)


_install(
    os._exit,
    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC,
    frozenset({
        "builtins.id", "compile", "exec", "function.__new__", "import", "marshal.loads",
        "object.__getattr__", "object.__setattr__", "os.listdir", "socket.getaddrinfo",
        "sys._getframemodulename",
        # Interpreter shutdown, after the result is printed.
        "cpython.PyInterpreterState_Clear", "cpython._PySys_ClearAuditHooks",
    }),
)
del _install
sys.path.insert(0, sys.argv[1])
import toposort
f = toposort.FilterModule().filters()
entries = json.load(sys.stdin)
before = copy.deepcopy(entries)
k8s = f["filter_by_platform"](entries, "k8s")
out = {
    "k8s": f["toposort_containers"](k8s, f["build_k8s_dep_map"](k8s, sys.argv[2])),
    "docker": f["filter_by_platform"](entries, "docker"),
}
canon = {k: [json.dumps(e, sort_keys=True) for e in v] for k, v in out.items()}
print(json.dumps({"out": canon, "input_unchanged": entries == before}))
"""


def _extract(repo: Path, treeish: str, dest: Path) -> None:
    """`git archive` of `treeish` unpacked under `dest`."""
    dest.mkdir(parents=True)
    tar_path = dest.parent / f"{dest.name}.tar"
    git("archive", "--format=tar", "-o", str(tar_path), treeish, cwd=repo)
    with tarfile.open(tar_path) as tar:
        tar.extractall(dest, filter="data")


@lru_cache(maxsize=None)
def _run(repo: Path, plugins: str, head: str) -> tuple[dict, bool] | None:
    """The chain's canonical outputs with the plugin tree `plugins` on HEAD's data, or None.

    None on any doubt: data that will not load or serialize, an import error, an exception,
    a timeout, a side effect the runner's audit hook reports, a path that appeared under the
    temp tree during the run, or output that is not the expected JSON.
    """
    try:
        host_vars = git(
            "show", f"{head}:ansible/inventory/host_vars/{K8S_HOST}.yml", cwd=repo
        )
        entries = yaml_fast.safe_load(host_vars.stdout)["containers_list"]
        data = json.dumps(entries)
        with tempfile.TemporaryDirectory(prefix="order-only-") as tmp:
            root = Path(tmp)
            code, ansible, cwd = root / "plugins", root / "ansible", root / "cwd"
            _extract(repo, plugins, code)
            _extract(repo, f"{head}:{_ROLES}", ansible / "roles")
            cwd.mkdir()
            before = set(root.rglob("*"))
            r = subprocess.run(
                [sys.executable, "-I", "-B", "-c", _RUNNER, str(code), str(ansible)],
                cwd=cwd,
                input=data,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )
            # Some calls create a file and raise no audit event (`os.mkfifo`, `os.mknod`), so a
            # relative path the hook cannot see still shows up here (#4846).
            created = set(root.rglob("*")) - before
        if r.returncode != 0 or created:
            return None
        result = json.loads(r.stdout)
        return result["out"], result["input_unchanged"] is True
    except (
        subprocess.SubprocessError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        tarfile.TarError,
    ):
        return None


def order_only(repo: Path, path: str, base: str, head: str) -> bool:
    """Whether `path`'s change over `base..head` only reorders the k8s play (#4842).

    True when the sort chain, run with the plugins at `base` and at `head` on HEAD's data,
    returns the same entries as multisets for each output, and neither run changed its input.
    False for a plugin not in `ORDER_ONLY_PLUGINS` and on any doubt (`_run`).
    """
    if path not in ORDER_ONLY_PLUGINS:
        return False
    trees = git(
        "rev-parse", f"{base}:{_PLUGINS}", f"{head}:{_PLUGINS}", cwd=repo, check=False
    )
    if trees.returncode != 0:
        return False
    old_tree, new_tree = trees.stdout.split()
    old, new = _run(repo, old_tree, head), _run(repo, new_tree, head)
    if old is None or new is None or not (old[1] and new[1]):
        return False
    return old[0].keys() == new[0].keys() and all(
        Counter(old[0][k]) == Counter(new[0][k]) for k in old[0]
    )
