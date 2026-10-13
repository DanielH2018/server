"""The k8s render is built before any test's timed phases, by `ansible/tests/conftest.py`.

Under xdist each worker builds `_k8s_render`'s render once, and `--durations` charges it to
whichever test asks first (#4873). The conftest hook builds it when collection finishes
instead, through `_k8s_render.prebuild`.

The hook cases run pytest in a child process, because only a fresh session shows whether the
render existed before its first test ran; this process built it long ago. The probe test the
child runs, `test_the_old_wide_prefix_is_flagged`, reads no render itself, so the render can
only be there because the hook built it.
"""

import sys

from _k8s_render import prebuild
from lib.proc_testing import run
from lib.repo_paths import REPO

# Records, at the first test's setup and ahead of its fixtures, whether the render exists.
_PROBE = """
import sys
import pytest

seen = []


class Probe:
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        render = sys.modules.get("_k8s_render")
        seen.append(render is not None and render._CACHE is not None)


code = pytest.main([*sys.argv[1:], "-n0", "-p", "no:cacheprovider"], plugins=[Probe()])
print(f"PROBE exit={int(code)} built={seen[:1]}")
"""
_PROBE_TEST = (
    "ansible/tests/services/test_karakeep_public_bypass.py"
    "::test_the_old_wide_prefix_is_flagged"
)


def _built_before_the_first_test(*extra: str) -> str:
    proc = run(
        [sys.executable, "-c", _PROBE, _PROBE_TEST, *extra],
        cwd=REPO,
        # A nested session pays its own collection and one full render.
        timeout=300,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("PROBE ")]
    assert lines, proc.stdout[-2000:] + proc.stderr[-2000:]
    return lines[-1]


def test_the_render_is_built_before_the_first_test_runs():
    assert _built_before_the_first_test() == "PROBE exit=0 built=[True]"


def test_without_the_hook_the_first_test_finds_no_render():
    """The red half: with every conftest off, the same session reaches its first test with no
    render, so the case above passes only because the hook ran."""
    assert _built_before_the_first_test("--noconftest") == "PROBE exit=0 built=[False]"


def test_a_failed_render_is_read_once_and_does_not_escape_prebuild():
    calls = []

    def broken_render():
        calls.append(1)
        raise AssertionError("widget/deployment.yaml.j2 failed to render: boom")

    prebuild(broken_render)
    assert calls == [1]
