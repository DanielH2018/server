"""The fixtures every suite under `scripts/` shares.

A test imports the module it covers dotted from the `scripts/` root (`from diagnostics import
probe`), so nothing here loads a module by path.
"""

import pytest

from lib import kubectl


def _fake_k8s_endpoint(hostname):
    return f"https://{hostname}.example", f"{hostname}.example:443:10.0.0.240"


@pytest.fixture
def fake_k8s_endpoint():
    return _fake_k8s_endpoint


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
