"""The fixtures every suite under `scripts/` shares.

A test imports the module it covers dotted from the `scripts/` root (`from diagnostics import
probe`), so nothing here loads a module by path.
"""

import pytest

from lib import kubectl


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
