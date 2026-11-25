"""The probe modules must reach patched core helpers through `core.<name>`.

`monkeypatch.setattr(core, "sops_extract", ...)` rebinds the attribute on
the core module object. A module that did `from core import
sops_extract` holds its own reference in its globals, taken at import time, and
that reference never sees the patch — the test passes while patching nothing.

The failure is silent in the direction that matters: a test believing it stubbed
out SOPS would really shell out to `sops`, and one believing it stubbed `fetch`
would really reach the network. Splitting gen_infra_map hit exactly this,
which is why it is a check rather than a comment.

Run: uv run pytest scripts/diagnostics/tests/test_probe_boundaries.py
"""

import ast
from pathlib import Path

# A wrong directory (`scripts/`, one above the modules) matched only `scripts/conftest.py`, so the
# guard checked nothing.
DIAGNOSTICS = Path(__file__).resolve().parents[1]

# Naming two known consumers catches a wrong directory, which an 'is non-empty' check would not.
KNOWN_CONSUMERS = frozenset(
    {"probe.py", "probe_lib/health.py", "probe_lib/monitors.py"}
)

# Keep in step with the `setattr(core, ...)` calls in test_probe*.py.
PATCHED = frozenset(
    {
        "domain",
        "fetch",
        "k8s_namespace",
        "metallb_vip",
        "sops_extract",
    }
)


def _probe_modules() -> list[Path]:
    """Every non-test module that imports core, at any depth under `scripts/diagnostics/`.

    Deliberately not a `probe*.py` glob: `postflight.py` imports core too and
    would have slipped straight through one. The rule is about who imports it, so
    that is what this matches.

    `rglob`, not `glob`: the twelve subcommand modules moved into `probe_lib/` and a
    one-level glob saw none of them. KNOWN_CONSUMERS is what caught that, and it names a
    module in the subdirectory for exactly this reason.
    """
    found = []
    for path in sorted(DIAGNOSTICS.rglob("*.py")):
        rel = path.relative_to(DIAGNOSTICS).as_posix()
        if rel == "probe_lib/core.py" or path.name.startswith("test_"):
            continue
        if "tests" in path.relative_to(DIAGNOSTICS).parts:
            continue
        if "core" in path.read_text():
            found.append(path)
    return found


def test_the_census_reaches_the_known_consumers():
    # Without this the assertions below pass vacuously if the scan ever stops matching.
    names = {path.relative_to(DIAGNOSTICS).as_posix() for path in _probe_modules()}
    missing = KNOWN_CONSUMERS - names
    assert not missing, (
        f"{missing} not found under {DIAGNOSTICS}; census sees {sorted(names)}"
    )


def test_no_module_binds_a_patched_core_helper_by_name():
    problems = []
    for path in _probe_modules():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "core":
                continue
            for alias in node.names:
                if alias.name in PATCHED:
                    problems.append(
                        f"{path.name}:{node.lineno}: `from core import "
                        f"{alias.name}` — call it as `core.{alias.name}(...)` instead, "
                        "or the tests' monkeypatch silently misses this module"
                    )
    assert not problems, "\n".join(problems)


def test_every_patched_name_exists_on_probe_core():
    # A rename that leaves PATCHED stale would quietly stop guarding that name.
    from diagnostics.probe_lib import core

    missing = sorted(n for n in PATCHED if not hasattr(core, n))
    assert not missing, f"PATCHED names absent from core: {missing}"
