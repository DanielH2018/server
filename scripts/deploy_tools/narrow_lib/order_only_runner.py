"""The script `order_only` runs in a subprocess at each end of a `toposort.py` change.

Kept apart from `order_only.py` so each module reads alone: this one is the program the
subprocess runs, as a string, and that one prepares its inputs and compares its reports. The
program opens with `order_only_tables.py`'s tables and process-state reader.
"""

# Runs in `python -I -B`, so neither PYTHONPATH nor the caller's cwd reaches the import, and
# the import writes no `__pycache__`. Resolves each filter through `FilterModule().filters()`,
# as Ansible does, so a re-pointed registration is the code that runs.
#
# An audit hook, installed before the import so the plugin's module-level code runs under it
# too, ends the process on any event outside the allowlist below, and on an `open` whose flags
# or mode string write or create (#4846, #5081). The allowlist is every event the real chain
# raises; an event it misses, after an interpreter or Ansible upgrade, reads as doubt and keeps the line rather than
# discharging it. An allowlist, because each denylist review found another mutating event.
# `mkfifo` and `mknod` create a file and raise no event, so `_install` replaces them on `os`
# and `posix` with a function that ends the process (#4999), and does the same to the
# `unaudited` table's calls (#5063). It also ends the process when a C module outside
# `c_modules` is created, at the `_imp` call that creates it, and backs that with the `import`
# event's name and a scan of `sys.modules` by loader (#5201). It wraps each of the `counted` table's calls in one that
# counts the calls that return, for the process state no function reads back (#5081).
#
# The hook also records the module state the run leaves behind (#4995). At the plugin's
# `import` event it copies the attributes of every module loaded so far. It also takes, by
# `walk`, a breadth-first shallow copy of each object below a module attribute that is a
# container, carries a `__dict__` or `__slots__`, or has a `readers` entry, at any depth
# (#5107), through a bound slot to its `__self__` (#5209), the process state `_process_state` reads (#5060), and the counts of the `counted`
# calls (#5081). At the `order-only.imported` event `_chain` raises once the import returns,
# it walks again and copies the objects the plugin's module code created (#5085). At the
# `order-only.report` event the runner raises after the chain, it writes into the report each
# `module.attribute` added, rebound or deleted since then, the `walk` path of each object that
# gained, lost or replaced an item or attribute, the path and `signature` of each object the
# second walk copied (#5107), each `process.<name>` that differs, and each module loaded since
# then. The `exempt` containers are caches whose contents follow which
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

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

from deploy_tools.narrow_lib.order_only_tables import TABLES

_HOOK_AND_CHAIN = """
def _install(
    exit_, write_flags, allowed, exempt, modules, unaudited, counted, readers, process, kinds,
    c_modules, loaders,
):
    before = {}
    contents = {}
    claimed = set()
    where = {}
    walked_after_import = []
    created = []
    state = {}
    calls = {}
    skip, deque, getset, member, class_dict, type_flags, wrapper, wrapper_self, named = kinds
    scalars = (bool, int, float, str, bytes, type(None))
    unset = object()
    builtin_names, builtin_importer, extension_loader, module_type, imp = loaders
    loaded_first = frozenset(modules)
    checked = set()

    def shallow(value):
        # Only C-level copies and descriptors, so no code the plugin defines runs in the hook:
        # `issubclass` on `type(value)` rather than `isinstance`, which reads `__class__`, and
        # an instance `__dict__` only through the `getset_descriptor` CPython puts there.
        kind = type(value)
        if kind in skip:
            return None
        if issubclass(kind, dict):
            return dict.copy(value)
        if issubclass(kind, list):
            return list.copy(value)
        if issubclass(kind, (set, deque)):
            return tuple(value)
        if issubclass(kind, type):
            return class_dict.__get__(value).copy()
        if kind is wrapper:
            # A bound slot such as `threading._counter`, an `itertools.count`'s `__next__`,
            # holds its object only as `__self__` (#5209).
            return {"__self__": wrapper_self.__get__(value)}
        # An instance's `__dict__`, then the members of each heap type in its MRO, read
        # through their `member_descriptor`s, then any C reader's result. A heap type is a
        # Python-defined class, whose `__slots__` are its members, or a C type an extension
        # builds at import, such as `_decimal.Context`.
        copied = {}
        found_any = found_dict = False
        for klass in kind.__mro__:
            found = class_dict.__get__(klass)
            if not found_dict and "__dict__" in found:
                descriptor = found["__dict__"]
                if type(descriptor) is not getset:
                    return None
                copied.update(descriptor.__get__(value, kind))
                found_any = found_dict = True
            # Heap types only: a static C type's members, such as `property.fget`, would widen
            # the walk across every object of that type without closing a shape.
            if type_flags.__get__(klass) & 512:
                for key, descriptor in found.items():
                    if type(descriptor) is member:
                        found_any = True
                        try:
                            copied[key] = descriptor.__get__(value, kind)
                        except AttributeError:
                            copied[key] = unset
        for klass, read in readers:
            if issubclass(kind, klass):
                copied["()"] = repr(read(value))
                found_any = True
        return copied if found_any else None

    def same(old, now):
        # An exact scalar by value, so a reader's fresh `str` or a getter's fresh `int` that
        # holds the same value is no change; anything else by identity.
        if old is now:
            return True
        kind = type(old)
        return kind is type(now) and kind in scalars and old == now

    def changed(old, now):
        if len(old) != len(now):
            return True
        if type(old) is dict:
            missing = object()
            return any(not same(v, dict.get(now, k, missing)) for k, v in old.items())
        if type(old) is tuple and type(now) is not list:
            # A set's iteration order follows its hashes, so compare membership.
            return any(item not in now for item in old)
        return any(a is not b for a, b in zip(old, now))

    def walk(roots, new=None):
        # Breadth-first, so an object is copied at the shallowest depth any module attribute
        # reaches it and its children are then copied too: `logging.Logger` precedes `root` in
        # `vars(logging)`, and a depth-first pass would claim the root logger at depth 2 and
        # never read its `filters`. Each object is copied once, under the first path in
        # breadth-first order, so both ends name it alike while they load the same modules in
        # the same order. A named logger sits four levels down, under `logging.Logger`, its
        # `manager` and the manager's `loggerDict` (#5085), so its `filters` list sits at five.
        # The walk follows every level, so no change is out of reach by depth alone (#5107).
        # A second walk, once the plugin's module code has run, descends through the objects
        # the first copied and copies only the ones it did not, under a path not yet taken,
        # and lists those paths in `new`.
        seen = set()
        level = roots
        while level:
            following = []
            for path, value in level:
                if id(value) in seen or path in exempt:
                    continue
                try:
                    copied = shallow(value)
                except Exception:
                    continue
                if copied is None:
                    continue
                seen.add(id(value))
                if id(value) not in claimed and path not in contents:
                    claimed.add(id(value))
                    where[id(value)] = path
                    contents[path] = (value, copied)
                    if new is not None:
                        new.append(path)
                items = copied.items() if type(copied) is dict else enumerate(copied)
                # A set's items follow hash order, which differs between processes, so `~`
                # marks their names as ones `signature` must not compare across the ends.
                mark = "/~" if issubclass(type(value), set) else "/#"
                for index, (key, child) in enumerate(items):
                    name = "/" + key if type(key) is str else mark + str(index)
                    following.append((path + name, child))
            level = following

    def signature(root):
        # What differs between two ends' copies of an object, as one string. A breadth-first
        # walk from `root` records, under each path relative to it, an exact `bool`, `int`,
        # `str` or `None` by value, a container by its length, a function by its qualified
        # name, and descends into every object no walk copied, such as a tuple or the handlers
        # of a logger the chain created (#5209).
        # A reference to an object a walk copied reads as that object's walk path, so a
        # re-pointed `parent` shows (#5209), except under a set item, whose path follows hash
        # order. `_cache` follows which levels were asked about, not what the logger is set
        # to. A reader's `()` result is left out, because a random generator's state differs
        # between two processes that never touch it.
        parts = []
        seen = {id(root)}
        level = [("", root)]
        while level:
            following = []
            for path, value in level:
                kind = type(value)
                if path and issubclass(kind, frozenset):
                    # Hash order again, so only the length (#5209).
                    parts.append(path + "#" + str(frozenset.__len__(value)))
                    continue
                if path and issubclass(kind, tuple):
                    copied = [tuple.__getitem__(value, i) for i in range(tuple.__len__(value))]
                else:
                    try:
                        copied = shallow(value)
                    except Exception:
                        copied = None
                if copied is None:
                    if not path:
                        return "?"
                    continue
                if type(copied) is not dict or issubclass(type(value), dict):
                    parts.append(path + "#" + str(len(copied)))
                if issubclass(type(value), set):
                    continue
                items = copied.items() if type(copied) is dict else enumerate(copied)
                for key, item in items:
                    if type(copied) is dict and (type(key) is not str or key in ("_cache", "()")):
                        continue
                    name = path + "/" + str(key)
                    kind = type(item)
                    if kind in (bool, int, str, type(None)):
                        parts.append(name + "=" + repr(item))
                    elif id(item) in where:
                        target = where[id(item)]
                        parts.append(name + "=@" + ("~" if "/~" in target else target))
                    elif kind in named:
                        # No walk copies a callable, so name it: a filter or handler method
                        # replaced by another function shows as a different name (#5209).
                        parts.append(name + "=&" + named[kind](item))
                    elif id(item) not in seen:
                        seen.add(id(item))
                        following.append((name, item))
            level = following
        parts.sort()
        return ",".join(parts)

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
            for key, value in now.items():
                if key not in attrs:
                    submodule = modules.get(name + "." + key)
                    if submodule is None or value is not submodule:
                        writes.append(name + "." + key)
        for path, (value, old) in contents.items():
            try:
                now = shallow(value)
            except Exception:
                now = None
            if now is None or changed(old, now):
                writes.append(path)
        for path, (value, old) in contents.items():
            if path.endswith("/manager/loggerDict") and type(value) is dict:
                # A logger created while the chain runs sits in no copy, so `loggerDict` gains
                # the same key at both ends. Its own state is what differs (#5085).
                for name, logger in dict.copy(value).items():
                    if id(logger) not in claimed and type(name) is str:
                        writes.append(path + "/" + name + "=" + signature(logger))
        for path in created:
            # An object the plugin's module code created holds whatever that code wrote to it
            # before either walk, so its state is compared between the ends, not within a run
            # (#5107).
            writes.append(path + "=" + signature(contents[path][0]))
        for key, value in read_state().items():
            if state.get(key) != value:
                writes.append("process." + key)
        loaded = [name for name in modules if name not in before]
        writes.sort()
        loaded.sort()
        return writes, loaded

    def foreign_c_module():
        # Whether a C module outside `c_modules` loaded since the hook was installed, by its
        # loader, behind the `_imp` guards below (#5201).
        for name, module in modules.copy().items():
            if name in checked or name in loaded_first or name in c_modules:
                continue
            checked.add(name)
            if type(module) is not module_type:
                continue
            loader = module.__dict__.get("__loader__")
            if loader is builtin_importer or type(loader) is extension_loader:
                return True
        return False

    def read_state():
        now = process()
        now.update(calls)
        return now

    def audit(event, args):
        try:
            if event == "open":
                # A C-level `fopen`, such as `readline.write_history_file`, passes its mode as
                # a string and its flags as 0 (#5081). `type` first, so no `str` subclass's
                # `__contains__` runs in the hook.
                mode = args[1]
                if args[2] & write_flags or (
                    type(mode) is str and any(c in mode for c in "wax+")
                ):
                    exit_(86)
            elif event == "import":
                # A built-in module is refused by name, before its code can run; a shared
                # object the import before this one loaded is refused here (#5201).
                name = args[0]
                if (
                    type(name) is str
                    and name in builtin_names
                    and name not in c_modules
                    and name not in modules
                ) or foreign_c_module():
                    exit_(86)
                if name == "toposort" and not before:
                    for name, module in modules.copy().items():
                        before[name] = (module, module.__dict__.copy())
                    walk([
                        (name + "." + key, value)
                        for name, (module, attrs) in before.items()
                        for key, value in attrs.items()
                    ])
                    state.update(read_state())
            elif event == "order-only.imported":
                # The plugin's module code has run, so copy what it created, such as a logger
                # its base end makes and its head end then writes to (#5085). The roots are
                # the modules the first walk started from, which leaves out the plugin's own.
                if before and not walked_after_import:
                    walked_after_import.append(True)
                    walk([
                        (name + "." + key, value)
                        for name, (module, attrs) in before.items()
                        for key, value in module.__dict__.copy().items()
                    ], created)
            elif event == "order-only.report":
                if foreign_c_module():
                    exit_(86)
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

    def counter(name, call):
        calls[name] = 0

        def count(*args, **kwargs):
            result = call(*args, **kwargs)
            calls[name] += 1
            return result

        return count

    def c_module_guard(exit_guard, create):
        # Every C module is created through `_imp.create_builtin` or `_imp.create_dynamic`,
        # whether the import system calls it or the plugin does, and a direct call raises no
        # `import` event and leaves nothing in `sys.modules` (#5201).
        def guarded(spec, *args):
            name = spec.name
            if type(name) is not str or (name not in c_modules and name not in loaded_first):
                exit_guard(86)
            return create(spec, *args)

        return guarded

    sys.addaudithook(audit)
    for attr in ("create_builtin", "create_dynamic"):
        setattr(imp, attr, c_module_guard(exit_, getattr(imp, attr)))
    for module in (os, posix):
        module.mkfifo = module.mknod = stub(exit_)
    for name, attrs in unaudited.items():
        for attr in attrs:
            setattr(modules[name], attr, stub(exit_))
    for name, attrs in counted.items():
        module = modules[name]
        for attr in attrs:
            setattr(module, attr, counter(name + "." + attr, getattr(module, attr)))


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
    counted,
    tuple(readers),
    _process_state(
        os.umask, _signal.getsignal,
        tuple(s for s in _signal.valid_signals() if s < _signal.NSIG),
        sys.getrecursionlimit, os.getpriority, os.PRIO_PROCESS, os.getsid, os.getpgrp,
        (os.getuid, os.geteuid, os.getgid, os.getegid, os.getgroups),
        gc.get_threshold, gc.isenabled, sys.getswitchinterval,
        _signal.getitimer, (_signal.ITIMER_REAL, _signal.ITIMER_VIRTUAL, _signal.ITIMER_PROF),
        partial(_signal.pthread_sigmask, _signal.SIG_BLOCK, ()), atexit._ncallbacks,
        partial(os.sched_getaffinity, 0), sys.get_int_max_str_digits, faulthandler.is_enabled,
        partial(_locale.setlocale, _locale.LC_ALL, None), sys.getdlopenflags,
        gc.get_freeze_count, _thread.stack_size, gc.get_debug,
        sys.get_coroutine_origin_tracking_depth, os.get_inheritable, _socket.getdefaulttimeout,
        decimal_context,
    ),
    (
        frozenset({
            types.ModuleType, types.FunctionType, types.BuiltinFunctionType, types.MethodType,
            types.CodeType, str, bytes, int, float, complex, bool, type(None), tuple,
            frozenset, range,
        }),
        _collections.deque,
        type(type.__dict__["__dict__"]),
        types.MemberDescriptorType,
        type.__dict__["__dict__"],
        type.__dict__["__flags__"],
        types.MethodWrapperType,
        types.MethodWrapperType.__dict__["__self__"],
        callable_names,
    ),
    c_modules,
    (
        frozenset(sys.builtin_module_names),
        sys.modules["_frozen_importlib"].BuiltinImporter,
        sys.modules["_frozen_importlib_external"].ExtensionFileLoader,
        types.ModuleType,
        sys.modules["_imp"],
    ),
)
del _install, _process_state, unaudited, counted, readers, decimal_context, c_modules, name
del partial, callable_names


def _chain(plugins, roles, preload):
    for name in preload:
        __import__(name)
    sys.path.insert(0, plugins)
    import toposort
    sys.audit("order-only.imported")
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

RUNNER = TABLES + _HOOK_AND_CHAIN
