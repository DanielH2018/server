"""A small named-entry registry shared by this repo's CLI dispatchers.

Extracted from monitor-bridge's ``CHECKS`` list (``ansible/roles/k8s/monitor-bridge/files/
check.py``) — the same shape, generalised: a name maps to a description and a callable, plus
optional flags. monitor-bridge itself keeps its own copy (it ships inside a container image
and must not import from ``scripts/``); this module is for everything that CAN import from
``scripts/`` — ``probe.py`` and ``scripts/validate/run_all.py`` today.

Argparse still owns argument parsing in every caller. This module owns three things argparse
does not: `only`/`skip` selection (monitor-bridge's ``CHECKS_ONLY``/``CHECKS_SKIP``
semantics — `only`, when non-empty, restricts to that set; `skip` always excludes), a
`--list` renderer, and a completeness guard so a new entry point can't ship unregistered.

Import it through the same bootstrap as any other ``lib`` module::

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from lib.registry import Registry, package_entry_points
"""

import dataclasses
import importlib
import inspect
import pkgutil


@dataclasses.dataclass
class Entry:
    name: str
    description: str
    func: object
    module: str | None = None
    flags: frozenset = dataclasses.field(default_factory=frozenset)


class Registry:
    """An ordered set of named entries.

    Each entry carries a description, a callable, an optional `module` (which package
    submodule backs it, for the completeness guard below) and optional `flags`
    (caller-defined markers, e.g. "handler" for "main() dispatches to this func directly").
    """

    def __init__(self, label="registry"):
        self.label = label
        self._entries: dict[str, Entry] = {}

    def add(self, name, func, description="", module=None, flags=()):
        if name in self._entries:
            raise ValueError(f"{self.label}: duplicate entry {name!r}")
        self._entries[name] = Entry(name, description, func, module, frozenset(flags))
        return func

    def __contains__(self, name):
        return name in self._entries

    def __iter__(self):
        return iter(self._entries.values())

    def __len__(self):
        return len(self._entries)

    def names(self):
        return list(self._entries)

    def get(self, name):
        return self._entries[name]

    def enabled(self, name, only=None, skip=None):
        """Same only/skip semantics as monitor-bridge's `check_enabled`.

        A non-empty `only` restricts to that set; `skip` always excludes, even from an
        entry named in `only`.
        """
        only = frozenset(only or ())
        skip = frozenset(skip or ())
        if only and name not in only:
            return False
        return name not in skip

    def selected(self, only=None, skip=None):
        """Entries surviving `only`/`skip`, in registration order."""
        return [e for e in self._entries.values() if self.enabled(e.name, only, skip)]

    def unknown(self, names):
        """Names that aren't registered.

        For validating --only/--skip input, the way monitor-bridge's `validate_check_filter`
        flags an unknown check name.
        """
        return sorted(set(names) - set(self._entries))

    def render_list(self):
        """One `name  description` line per entry, sorted by name, for a `--list` flag."""
        width = max((len(n) for n in self._entries), default=0)
        return [
            f"{name.ljust(width)}  {entry.description}".rstrip()
            for name, entry in sorted(self._entries.items())
        ]

    def covered_modules(self):
        return {e.module for e in self._entries.values() if e.module}

    def assert_complete(self, expected_modules):
        """Raise if the registered `module` set doesn't exactly match `expected_modules`.

        This is the completeness guard: pass `package_entry_points(pkg)` as
        `expected_modules` to assert every public entry point in `pkg` is registered.
        """
        expected = set(expected_modules)
        covered = self.covered_modules()
        missing = expected - covered
        extra = covered - expected
        if missing or extra:
            raise AssertionError(
                f"{self.label}: registry does not match {sorted(expected)} — "
                f"missing {sorted(missing)}, extra (registered but not expected) {sorted(extra)}"
            )


def package_entry_points(package, prefixes=("run", "main")):
    """Submodule names directly under `package` that define a run/main entry point.

    A module qualifies when it defines, at module scope, a function named exactly one of
    `prefixes` (e.g. "main") or prefixed with one of them plus an underscore (e.g.
    "run_health") — `probe_lib/health.py`'s `run_health` and `scripts/validate/*.py`'s
    `main` both match the same call. Only functions the module itself DEFINES count — a
    `run_x` imported from elsewhere doesn't make the importing module an entry point.
    `package` must already be imported (its `__path__` is what's walked); private modules
    (leading `_`) and subpackages are skipped. Used to build the `expected_modules` a
    completeness guard checks a registry against.
    """
    names = []
    for modinfo in pkgutil.iter_modules(package.__path__):
        if modinfo.name.startswith("_") or modinfo.ispkg:
            continue
        mod = importlib.import_module(f"{package.__name__}.{modinfo.name}")
        hit = any(
            inspect.isfunction(obj)
            and obj.__module__ == mod.__name__
            and (name in prefixes or any(name.startswith(f"{p}_") for p in prefixes))
            for name, obj in vars(mod).items()
        )
        if hit:
            names.append(modinfo.name)
    return sorted(names)
