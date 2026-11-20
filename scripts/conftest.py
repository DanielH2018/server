"""Shared scaffolding for scripts/test_*.py.

These scripts aren't a package (no `__init__.py`), so a test can't `import probe`
normally — nothing on disk resolves that name until something loads it by path. Every
test file would otherwise carry its own `importlib.util.spec_from_file_location` copy
of that dance; this collapses it to one load per script, done here at collection time
(pytest imports conftest.py before it collects any test module in this directory),
and registers each into `sys.modules` so a plain `import probe` / `import postflight`
/ `import grafana_panel_report` in a test file resolves to this same object —
standard `importlib` usage, not a special pytest hook.
"""

import importlib.util
import os
import sys

import pytest

from lib import kubectl

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_by_path(name, filename):
    """Load scripts/<filename> as a standalone module named `name`, and cache it in
    sys.modules so every subsequent `import <name>` (from any test file) reuses this
    exact object instead of re-executing the script."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, filename))
    assert spec and spec.loader, "spec_from_file_location found no loader"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# `scripts/diagnostics/` is deliberately NOT on pythonpath, so these flat scripts resolve by bare
# name only once loaded by path. `probe_lib/` needs no load: callers spell
# `from diagnostics.probe_lib import core`, which caches one object under one sys.modules key.
probe = _load_by_path("probe", "diagnostics/probe.py")

postflight = _load_by_path("postflight", "diagnostics/postflight.py")

# Two test modules import grafana_panel_report by bare name, so `uv run pytest scripts` fails at
# collection without this load.
grafana_panel_report = _load_by_path(
    "grafana_panel_report", "diagnostics/grafana_panel_report.py"
)

# The validators are not loaded by path: a flat name `render_templates` reachable from two
# pythonpath entries would create a second copy.


def _fake_k8s_endpoint(hostname):
    # The (base, --resolve pin) pair the live k8s_endpoint() derives from SOPS +
    # inventory — faked so plan() stays testable without either.
    return f"https://{hostname}.example", f"{hostname}.example:443:10.0.0.240"


@pytest.fixture
def fake_k8s_endpoint():
    return _fake_k8s_endpoint


# The `scripts/` classification, built once per worker. `classify()` re-reads every cron
# template, prek hook and workflow; the verdicts are a read-only mapping.
@pytest.fixture(scope="session")
def live_verdicts():
    from lib import script_classify as sc

    return sc.classify()


@pytest.fixture(autouse=True)
def _fresh_served_cluster():
    """Start and end every test with no cached cluster identity.

    `lib.kubectl.served_cluster` caches its answer per `Tools` for the life of the process,
    and an xdist worker runs many tests in one process. Without the clear, one test's fake
    cluster answers the next test's identity check.
    """
    kubectl.forget_served_cluster()
    yield
    kubectl.forget_served_cluster()
