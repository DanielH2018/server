"""The tables and process-state reader the `order_only` runner script opens with.

Kept apart from `order_only_runner.py` so each module stays under the module-length cap: this
one is what the runner reads, and that one is the audit hook and the chain that read it. Each
is a string, and `RUNNER` is their concatenation, so the subprocess runs them as one program.
"""

TABLES = """
import _codecs, _collections, _locale, _signal, _thread, atexit, codecs, copy, faulthandler
import _socket, gc, itertools, json, os, posix, signal, sys, types
from _functools import partial

# Imported before the hook so their file-creating calls can be replaced below (#5063).
unaudited = {
    "_dbm": ("open",),
    "_gdbm": ("open",),
    "_posixshmem": ("shm_open", "shm_unlink"),
    "_multiprocessing": ("SemLock", "sem_unlink"),
    # A Tcl interpreter's `open` and `file` commands create files with no event (#5107).
    "_tkinter": ("create",),
}
for name in list(unaudited):
    try:
        __import__(name)
    except ImportError:
        del unaudited[name]

# Calls that change interpreter state no function reads back: the codec search path and error
# handlers, the at-fork handlers, and faulthandler's C signal handlers and watchdog thread. The
# runner wraps each to count the calls that return, and compares the counts (#5081).
counted = {
    "codecs": ("register", "unregister", "register_error"),
    "_codecs": ("register", "unregister", "register_error"),
    "os": ("register_at_fork",),
    "posix": ("register_at_fork",),
    "faulthandler": (
        "register", "unregister", "dump_traceback_later", "cancel_dump_traceback_later",
    ),
    # A signal's `SA_RESTART` flag, which no function reads back (#5107).
    "signal": ("siginterrupt",),
    "_signal": ("siginterrupt",),
}

# Every C module the real chain imports once the hook is installed, and `_string`, which
# `logging` imports and which holds no state. A C module holds state and calls that no walk,
# reader or audit event reaches, so the hook ends the run on the import of any other, whether
# built in or a shared object (#5201). A module loaded before the hook, as the runner's own
# imports and the `unaudited` table's are, is outside the check. An interpreter or Ansible
# upgrade that adds a C module to the chain reads as doubt and turns
# `test_4836s_real_toposort_change_is_order_only` red; extend the list then.
c_modules = frozenset({
    "_ast", "_bisect", "_bz2", "_contextvars", "_datetime", "_heapq", "_lzma", "_opcode",
    "_socket", "_string", "_tokenize", "_typing", "_zstd", "fcntl", "grp", "math", "pwd",
    "unicodedata", "zlib",
})

# C objects whose state no descriptor the walk reads exposes, each with a C-level reader. The
# walk stores what the reader returns, as its `repr`, under the key `()` (#5107).
readers = []
decimal_context = None


def _calls(*reads):
    def read(value):
        return tuple(call(value) for call in reads)

    return read


# Whether a lock is held, and how deep this thread holds it (#5209). A held `logging._lock`
# deadlocks every other thread of the controller.
readers.append((_thread.RLock, _calls(_thread.RLock.locked, _thread.RLock._recursion_count)))
# A count's next value, which only its `repr` exposes. A count over a number whose class the
# plugin defines runs that class's `__repr__` in the hook; the good-faith limit covers it.
readers.append((itertools.count, itertools.count.__repr__))
del _calls
try:
    import _random

    readers.append((_random.Random, _random.Random.getstate))
except ImportError:
    pass
try:
    import _decimal

    def _context(fields, getattribute):
        def read(context):
            return (
                tuple(field.__get__(context) for field in fields),
                getattribute(context, "traps"),
                getattribute(context, "flags"),
            )

        return read

    readers.append((_decimal.Context, _context(
        tuple(
            _decimal.Context.__dict__[name]
            for name in ("prec", "Emax", "Emin", "rounding", "capitals", "clamp")
        ),
        _decimal.Context.__getattribute__,
    )))
    # The thread's own context lives in a context variable no module attribute holds, so
    # `_process_state` reads it through `getcontext` (#5209).
    decimal_context = (_decimal.getcontext, readers[-1][1])
    del _context
except ImportError:
    pass


def _process_state(umask, getsignal, signals, getrecursionlimit, getpriority, prio,
                   getsid, getpgrp, ids, get_threshold, isenabled, getswitchinterval,
                   getitimer, timers, sigmask, ncallbacks, affinity, int_digits,
                   faulthandler_enabled, getlocale, getdlopenflags, freeze_count, stack_size,
                   get_debug, coroutine_depth, get_inheritable, socket_timeout, decimal_context):
    def read():
        # Set and restore: neither the umask nor the thread stack size has a reader that leaves
        # it alone, since `stack_size()` with no argument sets the size to 0 (#5107).
        mask = umask(0)
        umask(mask)
        size = stack_size(0)
        stack_size(size)
        state = {
            "umask": mask,
            "recursionlimit": getrecursionlimit(),
            "priority": getpriority(prio, 0),
            "sid": getsid(0),
            "pgrp": getpgrp(),
            "ids": tuple(f() for f in ids),
            "gc": (isenabled(), get_threshold()),
            "switchinterval": getswitchinterval(),
            "sigmask": sorted(sigmask()),
            "atexit": ncallbacks(),
            "affinity": sorted(affinity()),
            "intmaxstrdigits": int_digits(),
            "faulthandler": faulthandler_enabled(),
            "locale": getlocale(),
            "dlopenflags": getdlopenflags(),
            "gcfreeze": freeze_count(),
            "stacksize": size,
            "gcdebug": get_debug(),
            "coroutinedepth": coroutine_depth(),
            "sockettimeout": socket_timeout(),
        }
        for fd in (0, 1, 2):
            # No audit event reports `os.set_inheritable`, and a child the controller spawns
            # inherits only the fds marked so (#5209).
            try:
                state["inheritable." + str(fd)] = get_inheritable(fd)
            except OSError:
                state["inheritable." + str(fd)] = None
        if decimal_context is not None:
            getcontext, read_context = decimal_context
            context = getcontext()
            state["decimal"] = (id(context), repr(read_context(context)))
        for timer in timers:
            # A running timer's remaining time falls between reads, so compare whether it runs.
            value, interval = getitimer(timer)
            state["itimer." + str(timer)] = (value > 0, interval)
        for signum in signals:
            state["signal." + str(signum)] = getsignal(signum)
        return state

    return read
"""
