"""The probe modules must reach patched core helpers through `core.<name>`.

`monkeypatch.setattr(probe_core, "sops_extract", ...)` rebinds the attribute on
the probe_core module object. A module that did `from probe_core import
sops_extract` holds its own reference in its globals, taken at import time, and
that reference never sees the patch — the test passes while patching nothing.

The failure is silent in the direction that matters: a test believing it stubbed
out SOPS would really shell out to `sops`, and one believing it stubbed `fetch`
would really reach the network. Splitting gen_infra_map (#372) hit exactly this,
which is why it is a check rather than a comment.

Run: uv run pytest scripts/diagnostics/test_probe_boundaries.py
"""

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]

# Names the probe test suites monkeypatch on probe_core. Keep in step with the
# `setattr(core, "...")` calls in scripts/test_probe*.py.
PATCHED = frozenset(
    {
        "fetch",
        "k8s_namespace",
        "metallb_vip",
        "pi_ip",
        "sops_extract",
    }
)


def _probe_modules() -> list[Path]:
    """Every non-test module that imports probe_core.

    Deliberately not a `probe*.py` glob: `postflight.py` imports probe_core too and
    would have slipped straight through one. The rule is about who imports it, so
    that is what this matches.
    """
    found = []
    for path in sorted(SCRIPTS.glob("*.py")):
        if path.name in ("probe_core.py",) or path.name.startswith("test_"):
            continue
        if "probe_core" in path.read_text():
            found.append(path)
    return found


def test_there_are_probe_modules_to_check():
    # Without this the assertions below pass vacuously if the scan ever stops matching.
    assert _probe_modules(), f"no probe_core consumers found under {SCRIPTS}"


def test_no_module_binds_a_patched_core_helper_by_name():
    problems = []
    for path in _probe_modules():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "probe_core":
                continue
            for alias in node.names:
                if alias.name in PATCHED:
                    problems.append(
                        f"{path.name}:{node.lineno}: `from probe_core import "
                        f"{alias.name}` — call it as `core.{alias.name}(...)` instead, "
                        "or the tests' monkeypatch silently misses this module"
                    )
    assert not problems, "\n".join(problems)


def test_every_patched_name_exists_on_probe_core():
    # A rename that leaves PATCHED stale would quietly stop guarding that name.
    import probe_core

    missing = sorted(n for n in PATCHED if not hasattr(probe_core, n))
    assert not missing, f"PATCHED names absent from probe_core: {missing}"
