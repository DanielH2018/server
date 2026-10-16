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

from deploy_tools.narrow_lib.order_only_runner import RUNNER

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
# runner imports those modules first and replaces the calls the same way (#5063). Any other C
# module can hold state and make calls that no audit event reports, so the runner ends the run
# when a C module outside its `c_modules` list, the ones the real chain imports, is created
# (#5201). It wraps the two `_imp` calls every built-in and shared-object module is created
# through, which run before the module's code does. A side effect that stays in the process is compared between the two ends rather
# than refused: importing Ansible rebinds `sys.intern` at both (#4995). That covers an attribute of another
# module added, rebound or deleted, a module loaded, and an in-place change to an object the
# walk copies. The walk copies every object at every depth below a module attribute, taking
# each object at the shallowest depth any module attribute reaches it: an item of a list, dict,
# set or deque, an attribute in an object's `__dict__`, or a member of a heap type, which
# includes every `__slots__` value (#5107). So it sees `logging.disable` writing `logging.root.manager`,
# `logging.root.addFilter` adding to `logging.root.filters`, a named logger's `disabled` flag
# under `logging.Logger.manager.loggerDict` (#5085), and that logger's `filters` a level below.
# The runner's `readers` table reads two C objects' state through their own C getters: a
# random generator's and a decimal context's (#5107). It also covers
# process state no module attribute holds, such as the umask, a signal handler or mask, a
# pending timer, an atexit callback, the recursion limit, the dlopen flags, gc's freeze count
# (#5060) or the thread stack size (#5107). No function reads back the codec registries, the
# at-fork handlers, faulthandler's handlers and watchdog or a signal's `siginterrupt` flag, so
# the runner wraps the calls that change them and compares how many returned. An `open` whose
# mode string writes ends the run as its flags do, because a C-level
# `fopen` such as `readline.write_history_file` raises the event with flags 0 (#5081).
# A second walk, once the plugin's module code has run, copies the objects that code created,
# such as a logger the base end makes and the head end's filter call disables (#5085). Neither
# walk sees what the module code wrote to such an object before it, so the report also compares
# each one's own `bool`, `int`, `str` and container-length attributes between the ends (#5107).
# A logger first created while the chain runs sits in neither walk, so the report compares those
# same attributes of each such logger (#5085).
# The gaps taken: a branch HEAD's data does not exercise; a write the plugin's module code
# makes to an object that code created which none of those attributes shows, such as one
# replacing an object reference; a change to the handler or filter objects of a logger first
# created while the chain runs, as opposed to how many it holds; state inside a C object outside
# the `readers` table, or in a static C type's members, when its module is one the runner loads
# before its hook or lists in `c_modules`; process state the runner neither reads nor counts; a
# counted call through a reference taken before the runner wrapped it; an unaudited call it does
# not replace in a C module of either kind; and a role whose bytes depend on list order,
# which the render proof the caller still requires catches.
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
                    RUNNER,
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
