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
# it too (#4846). `os.mkfifo` and `os.mknod` raise no audit event, so the runner replaces them
# with a call that ends the run (#4999). The calls in `_dbm`, `_gdbm`, `_posixshmem` and
# `_multiprocessing` that create or remove a filesystem entry raise no event either, so the
# runner imports those modules first and replaces the calls the same way (#5063). A side
# effect that stays in the process is compared between the two ends rather than refused:
# importing Ansible rebinds `sys.intern` at both (#4995). That covers an attribute of another
# module added, rebound or deleted, an item added to or removed from a list, dict or set a
# module holds, a module loaded, and process state no module attribute holds, such as the
# umask, a signal handler or the recursion limit (#5060).
# The gaps taken: a branch HEAD's data does not exercise; a change nested below a module's
# own container, or inside an object that is not a plain list, dict or set; process state the
# runner does not read, such as the codecs registry or an at-fork handler; an unaudited call
# in a module the runner does not replace; and a role whose bytes depend on list order, which
# the render proof the caller still requires catches.
# The runner is distinguishable from a deploy. Its cwd ends in `/cwd` under an `order-only-`
# temp dir, `sys.flags.isolated` is 1, and `sys.argv[0]` is `-c`. So the check holds only for
# a plugin written in good faith. It catches an accidental side effect, not an evasive one,
# such as a fresh `posix` from `_imp.create_builtin` that still has `mknod`.
# We accept that limit, because a commit that can land a hostile filter plugin on master can
# act through any deploy anyway.
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
# `mkfifo` and `mknod` create a file and raise no event, so `_install` replaces them on `os`
# and `posix` with a function that ends the process (#4999), and does the same to the
# `unaudited` table's calls (#5063).
#
# The hook also records the module state the run leaves behind (#4995). At the plugin's
# `import` event it copies the attributes of every module loaded so far, a shallow copy of
# each attribute that is a plain list, dict or set, and the process state `_process_state`
# reads (#5060). At the `order-only.report` event the runner raises after the chain, it writes
# into the report each `module.attribute` added, rebound or deleted since then, each one whose
# container gained, lost or replaced an item, each `process.<name>` that differs, and each
# module loaded since then. The `exempt` containers are caches whose contents follow which
# code ran and the temp paths it ran from, rather than anything a deploy would keep.
# A submodule bound on its parent by the import system is not a write. A module first loaded
# after the copy has no baseline, and the real plugin loads all of Ansible that way, so
# `order_only` runs each end twice: the first run names the modules it loads, and the second
# imports them before the plugin, as a deploy's controller already has, so a write into any of
# them shows in the diff. A report the hook never filled in reads as doubt.
#
# The plugin is imported after the hook and can reach anything `__main__`, `os` or `builtins`
# holds, so the hook reads nothing from them: `_install` binds the exit function and the tables
# as closure cells, and is deleted before the import. Only the interpreter's hook list then
# refers to the hook, and the `gc` events that would walk to it are off the allowlist. The hook
# exits with `os._exit` rather than raising, because a raise lands in the plugin's frame, where
# an `except` can swallow it. `_run` reads the nonzero exit as doubt. The chain runs inside
# `_chain`, so the runner binds no name in `__main__` while the module state is compared.
_RUNNER = """
import _signal, copy, gc, json, os, posix, sys

# Imported before the hook so their file-creating calls can be replaced below (#5063).
unaudited = {
    "_dbm": ("open",),
    "_gdbm": ("open",),
    "_posixshmem": ("shm_open", "shm_unlink"),
    "_multiprocessing": ("SemLock", "sem_unlink"),
}
for name in list(unaudited):
    try:
        __import__(name)
    except ImportError:
        del unaudited[name]


def _install(exit_, write_flags, allowed, exempt, modules, unaudited, process):
    before = {}
    contents = {}
    state = {}

    def shallow(value):
        # Exact types only: a subclass, such as `os.environ`, runs its own code in the hook.
        kind = type(value)
        if kind is list:
            return list.copy(value)
        if kind is dict:
            return dict.copy(value)
        if kind is set:
            return set.copy(value)
        return None

    def changed(old, now):
        if len(old) != len(now):
            return True
        if type(old) is list:
            return any(a is not b for a, b in zip(old, now))
        if type(old) is set:
            return any(item not in now for item in old)
        missing = object()
        return any(dict.get(now, k, missing) is not v for k, v in old.items())

    def module_writes():
        writes = []
        for name, (module, attrs) in before.items():
            if modules.get(name) is not module:
                writes.append(name)
                continue
            now = module.__dict__
            for key, value in attrs.items():
                if key not in now or now[key] is not value:
                    writes.append(name + "." + key)
                elif (name, key) in contents and changed(contents[name, key], value):
                    writes.append(name + "." + key)
            for key, value in now.items():
                if key not in attrs:
                    submodule = modules.get(name + "." + key)
                    if submodule is None or value is not submodule:
                        writes.append(name + "." + key)
        for key, value in process().items():
            if state.get(key) != value:
                writes.append("process." + key)
        loaded = [name for name in modules if name not in before]
        writes.sort()
        loaded.sort()
        return writes, loaded

    def audit(event, args):
        try:
            if event == "open":
                if args[2] & write_flags:
                    exit_(86)
            elif event == "import":
                if args[0] == "toposort" and not before:
                    for name, module in modules.copy().items():
                        attrs = module.__dict__.copy()
                        before[name] = (module, attrs)
                        for key, value in attrs.items():
                            copied = None if name + "." + key in exempt else shallow(value)
                            if copied is not None:
                                contents[name, key] = copied
                    state.update(process())
            elif event == "order-only.report":
                args[0]["module_writes"], args[0]["modules_loaded"] = module_writes()
            elif event not in allowed:
                exit_(86)
        except BaseException:
            exit_(86)

    # The stubs sit on modules the plugin can read, where it can reach their closure, so each
    # gets a cell of its own. Sharing the hook's `exit_` cell would let a write to it disarm
    # the hook.
    def stub(exit_stub):
        def refuse(*args, **kwargs):
            exit_stub(86)

        return refuse

    sys.addaudithook(audit)
    for module in (os, posix):
        module.mkfifo = module.mknod = stub(exit_)
    for name, attrs in unaudited.items():
        for attr in attrs:
            setattr(modules[name], attr, stub(exit_))


def _process_state(umask, getsignal, signals, getrecursionlimit, getpriority, prio,
                   getsid, getpgrp, ids, get_threshold, isenabled, getswitchinterval):
    def read():
        # Set and restore: the umask has no reader that leaves it alone.
        mask = umask(0)
        umask(mask)
        state = {
            "umask": mask,
            "recursionlimit": getrecursionlimit(),
            "priority": getpriority(prio, 0),
            "sid": getsid(0),
            "pgrp": getpgrp(),
            "ids": tuple(f() for f in ids),
            "gc": (isenabled(), get_threshold()),
            "switchinterval": getswitchinterval(),
        }
        for signum in signals:
            state["signal." + str(signum)] = getsignal(signum)
        return state

    return read


_install(
    os._exit,
    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC,
    frozenset({
        "builtins.id", "compile", "exec", "function.__new__", "marshal.loads",
        "object.__getattr__", "object.__setattr__", "os.listdir", "socket.getaddrinfo",
        "sys._getframemodulename",
        # Interpreter shutdown, after the result is printed.
        "cpython.PyInterpreterState_Clear", "cpython._PySys_ClearAuditHooks",
    }),
    # Containers whose contents follow which code ran rather than what it did: the import
    # system's caches and the stdlib's memo tables. `sys.modules` is `modules_loaded`.
    frozenset({
        "sys.modules", "sys.path_importer_cache", "linecache.cache", "re._cache",
        "re._cache2", "encodings._cache", "importlib._bootstrap._module_locks",
        "importlib._bootstrap._blocking_on", "_frozen_importlib._module_locks",
        "_frozen_importlib._blocking_on",
    }),
    sys.modules,
    unaudited,
    _process_state(
        os.umask, _signal.getsignal,
        tuple(s for s in _signal.valid_signals() if s < _signal.NSIG),
        sys.getrecursionlimit, os.getpriority, os.PRIO_PROCESS, os.getsid, os.getpgrp,
        (os.getuid, os.geteuid, os.getgid, os.getegid, os.getgroups),
        gc.get_threshold, gc.isenabled, sys.getswitchinterval,
    ),
)
del _install, _process_state, unaudited, name


def _chain(plugins, roles, preload):
    for name in preload:
        __import__(name)
    sys.path.insert(0, plugins)
    import toposort
    f = toposort.FilterModule().filters()
    entries = json.load(sys.stdin)
    before = copy.deepcopy(entries)
    k8s = f["filter_by_platform"](entries, "k8s")
    out = {
        "k8s": f["toposort_containers"](k8s, f["build_k8s_dep_map"](k8s, roles)),
        "docker": f["filter_by_platform"](entries, "docker"),
    }
    report = {
        "out": {k: [json.dumps(e, sort_keys=True) for e in v] for k, v in out.items()},
        "input_unchanged": entries == before,
    }
    sys.audit("order-only.report", report)
    print(json.dumps(report))


_chain(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))
"""


def _extract(repo: Path, treeish: str, dest: Path) -> None:
    """`git archive` of `treeish` unpacked under `dest`."""
    dest.mkdir(parents=True)
    tar_path = dest.parent / f"{dest.name}.tar"
    git("archive", "--format=tar", "-o", str(tar_path), treeish, cwd=repo)
    with tarfile.open(tar_path) as tar:
        tar.extractall(dest, filter="data")


@lru_cache(maxsize=None)
def _run(
    repo: Path, plugins: str, head: str, preload: tuple[str, ...] = ()
) -> tuple[dict, bool, tuple[tuple[str, ...], tuple[str, ...]]] | None:
    """One run of the chain with the plugin tree `plugins` on HEAD's data, or None.

    Args:
      repo: The repository the trees are read from.
      plugins: The `ansible/filter_plugins` tree to run.
      head: The commit whose `containers_list` and role tree the chain reads.
      preload: Modules the runner imports before the plugin, outside the module-state diff.

    Returns:
      The chain's canonical outputs; whether the run left its input unchanged; and the
      module state the run left behind, as the `module.attribute` names it added, rebound,
      deleted or changed in place and the `process.<name>` state it changed (#4995, #5060),
      and the names of the modules it loaded. None on any doubt: data that
      will not load or serialize, an import error, an exception, a timeout, a side effect the
      runner's audit hook reports, a path that appeared under the temp tree during the run,
      or output that is not the expected JSON.
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
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    _RUNNER,
                    str(code),
                    str(ansible),
                    json.dumps(preload),
                ],
                cwd=cwd,
                input=data,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )
            # A backstop for a file-creating call that raises no audit event and that the
            # runner does not replace, on a path relative to the cwd (#4846).
            created = set(root.rglob("*")) - before
        if r.returncode != 0 or created:
            return None
        result = json.loads(r.stdout)
        state = tuple(result["module_writes"]), tuple(result["modules_loaded"])
        return result["out"], result["input_unchanged"] is True, state
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
    returns the same entries as multisets for each output, neither run changed its input, and
    both runs loaded the same modules and left the same module and process state behind
    (#4995, #5060). False for
    a plugin not in `ORDER_ONLY_PLUGINS` and on any doubt (`_run`).
    """
    if path not in ORDER_ONLY_PLUGINS:
        return False
    trees = git(
        "rev-parse", f"{base}:{_PLUGINS}", f"{head}:{_PLUGINS}", cwd=repo, check=False
    )
    if trees.returncode != 0:
        return False
    old_tree, new_tree = trees.stdout.split()
    first_old, first_new = _run(repo, old_tree, head), _run(repo, new_tree, head)
    if first_old is None or first_new is None or first_old[2][1] != first_new[2][1]:
        return False
    # The plugin trees' own modules are the code under test, so they load after the diff's copy.
    own = {
        name.removesuffix(".py")
        for tree in (old_tree, new_tree)
        for name in git("ls-tree", "--name-only", tree, cwd=repo).stdout.split()
    }
    preload = tuple(n for n in first_old[2][1] if n.partition(".")[0] not in own)
    old, new = _run(repo, old_tree, head, preload), _run(repo, new_tree, head, preload)
    if old is None or new is None or not (old[1] and new[1]):
        return False
    if old[2] != new[2]:
        return False
    return old[0].keys() == new[0].keys() and all(
        Counter(old[0][k]) == Counter(new[0][k]) for k in old[0]
    )
