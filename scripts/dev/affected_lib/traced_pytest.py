"""Run pytest while logging which directory listings were the harness's own.

`affected_tests.py record` runs this file under `strace` in place of `python -m pytest`. The
trace reports every directory a process listed, and two callers list directories for every
test module whatever the module tests: the import system lists each `sys.path` entry, and
pytest's collector lists the module's directory. Counted as the module's own, those listings
would select every module in a directory whenever a file is added there.

The log at `$AFFECTED_LISTING_LOG` gets one line per listing: `H` for one of those two
callers, `T` for any other, a tab, then the path. The recorder drops a directory only when
every listing of it was an `H`.

This is a launcher, not a plugin, because the audit hook has to be in place before pytest
imports its plugins: the import system lists a `sys.path` entry once and caches the result.
"""

import os
import sys

LOG_ENV = "AFFECTED_LISTING_LOG"
_EVENTS = frozenset({"os.listdir", "os.scandir"})


def is_harness_frame(filename: str) -> bool:
    """Whether a listing made from `filename` is the import system's or pytest's collector's.

    `importlib.metadata` counts as the import system: pytest lists every `sys.path` entry
    through it to find installed plugins.
    """
    return (
        filename.startswith("<frozen importlib")
        or "/importlib/metadata/" in filename
        or "/_pytest/" in filename
    )


def harness_only(log_text: str) -> set[str]:
    """The directories that only the import system or pytest's collector listed."""
    seen: dict[str, set[str]] = {}
    for line in log_text.splitlines():
        kind, _, path = line.partition("\t")
        if path:
            seen.setdefault(os.path.normpath(path), set()).add(kind)
    return {path for path, kinds in seen.items() if kinds == {"H"}}


def _log_listings(log_path: str) -> None:
    def hook(event: str, args: tuple) -> None:
        if event not in _EVENTS:
            return
        path = args[0] if args and args[0] is not None else "."
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if not isinstance(path, (str, os.PathLike)):
            return
        kind = "H" if is_harness_frame(sys._getframe(1).f_code.co_filename) else "T"
        with open(log_path, "a") as log:
            log.write(f"{kind}\t{os.path.abspath(os.fspath(path))}\n")

    sys.addaudithook(hook)


if __name__ == "__main__":
    # Without the log, answer what this file is and import nothing: an import would list `sys.path`
    # ahead of the hook.
    if LOG_ENV not in os.environ:
        print(__doc__)
        raise SystemExit(0 if {"-h", "--help"} & set(sys.argv[1:]) else 64)
    _log_listings(os.environ[LOG_ENV])
    sys.path[0] = os.getcwd()
    import pytest

    raise SystemExit(pytest.main(sys.argv[1:]))
