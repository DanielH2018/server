"""Guards _land_fakes.py's module-scope TemporaryDirectory against warning at interpreter exit.

`_PRIMARY_TMP` in `_land_fakes.py` is held at module scope so its directory stays valid for
the whole test session. Left to its own implicit finalizer, `TemporaryDirectory` warns with a
`ResourceWarning` when that finalizer runs -- and `filterwarnings = ["error"]` in
pyproject.toml turns that warning into a raised exception during interpreter teardown. Under
plain CPython this only prints a traceback to stderr (exit code stays 0), but the same warning
inside a pytest-xdist worker's own atexit handling failed the whole run non-deterministically,
depending on load (see #1231). The fix registers an explicit `atexit.register(tmp.cleanup)`:
`TemporaryDirectory.cleanup()` detaches the weakref finalizer before the implicit one ever
runs, and atexit calls run LIFO, so our later-registered cleanup always beats the finalizer
registered when `tempfile`/`weakref` were imported -- deterministic, not load-dependent.

The `ResourceWarning` text on stderr is what's asserted, not an exit code, because the
exit-code amplification is the load-dependent xdist-specific part (#1231); the warning itself
is what pytest's `filterwarnings = ["error"]` would turn into a failure, and it fires (or
doesn't) exactly the same way in a bare subprocess.

Run: uv run pytest scripts/deploy_tools/tests/test_land_fakes_cleanup.py
"""

import subprocess
import sys
import textwrap
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
DEPLOY_TOOLS_DIR = TESTS_DIR.parent
SCRIPTS_DIR = DEPLOY_TOOLS_DIR.parent


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_importing_land_fakes_warns_nothing_at_interpreter_exit():
    """The fixed shape: _land_fakes' module-scope TemporaryDirectory must not warn on teardown."""
    code = textwrap.dedent(f"""
        import sys, warnings
        for _p in ({str(TESTS_DIR)!r}, {str(DEPLOY_TOOLS_DIR)!r}, {str(SCRIPTS_DIR)!r}):
            sys.path.insert(0, _p)
        warnings.filterwarnings("error")
        import _land_fakes  # noqa: F401
    """)
    result = _run(code)
    assert result.returncode == 0, result.stderr
    assert "ResourceWarning" not in result.stderr, result.stderr


def test_the_guard_would_catch_the_unfixed_shape():
    """The reject half: a module-scope TemporaryDirectory with no atexit cleanup DOES warn.

    Proves the assertion above is exercising something real: without an explicit
    `atexit.register(tmp.cleanup)`, the implicit weakref finalizer runs at interpreter exit,
    calls `warnings.warn(..., ResourceWarning)`, and `filterwarnings("error")` turns that into
    a printed traceback here (an exit-code failure only under the xdist-worker conditions in
    #1231, which are load-dependent rather than reproducible in a single subprocess).
    """
    code = textwrap.dedent("""
        import tempfile, warnings
        warnings.filterwarnings("error")
        _t = tempfile.TemporaryDirectory(
            prefix="land-primary-unfixed-", ignore_cleanup_errors=True
        )
    """)
    result = _run(code)
    assert "ResourceWarning" in result.stderr, result.stderr
