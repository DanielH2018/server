"""Shared scaffolding for scripts/test_*.py.

These scripts aren't a package (no `__init__.py`), so a test can't `import probe`
normally — nothing on disk resolves that name until something loads it by path. Every
test file used to carry its own `importlib.util.spec_from_file_location` copy of that
dance; this collapses it to one load per script, done here at collection time (pytest
imports conftest.py before it collects any test module in this directory), and
registers each into `sys.modules` so a plain `import probe` / `import postflight` in a
test file resolves to this same object — standard `importlib` usage, not a special pytest
hook.
"""

import importlib.util
import os
import sys

import pytest

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


# probe.py must load first, by path — its own top-level `import probe_core as core`
# is a normal import, and every test file's later `import probe_core as core` (or
# `import probe_ha as ha` / `import probe_longhorn as longhorn`) is what reuses that
# same cached module rather than importing a second copy. Loading probe.py after
# would still work, but this is the order the test suites were written against.
probe = _load_by_path("probe", "diagnostics/probe.py")

postflight = _load_by_path("postflight", "diagnostics/postflight.py")

# The validators need no load here. `scripts/` is on pythonpath, `validate/` is a namespace
# package under it, and `from validate.k8s_manifests import ...` is spelled the same way by
# every caller — so normal import machinery caches one object under one sys.modules key.
# They were loaded by path while the flat name `validate_k8s_manifests` was reachable from
# two pythonpath entries, which is the second copy this replaced.


# Fake resolver: maps container name -> a recognizable IP. A wrong container name
# raises KeyError, so a misrouted subcommand fails loudly.
IPS = {"prometheus": "10.0.0.1", "loki": "10.0.0.2", "scrutiny": "10.0.0.3"}


def _fake_resolve(name):
    return IPS[name]


def _fake_k8s_endpoint(hostname):
    # The (base, --resolve pin) pair the live k8s_endpoint() derives from SOPS +
    # inventory — faked so plan() stays testable without either.
    return f"https://{hostname}.example", f"{hostname}.example:443:10.0.0.240"


@pytest.fixture
def ips():
    return IPS


@pytest.fixture
def fake_resolve():
    return _fake_resolve


@pytest.fixture
def fake_k8s_endpoint():
    return _fake_k8s_endpoint
