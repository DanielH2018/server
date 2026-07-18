"""Guard: ical-proxy's Dockerfile pip pins must match uv.lock's dev-group versions.

The ical-proxy test suite (`ansible/roles/containers/ical-proxy/files/test_app.py`) imports
`app.py`, which imports flask — so it validates the app against whatever flask/requests uv.lock
pins for the `dev` group. The runtime image installs them via `pip install flask==X requests==Y`
in `templates/Dockerfile.j2`. If those versions drift, the test certifies a flask the container
never runs (2026-07-18 review L1). This fails the instant they diverge, forcing the Dockerfile pin
to be bumped in lockstep whenever lockFileMaintenance moves uv.lock.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "ansible/roles/containers/ical-proxy/templates/Dockerfile.j2"
UV_LOCK = REPO / "uv.lock"

_TRACKED = ("flask", "requests")


def _dockerfile_pins():
    text = DOCKERFILE.read_text()
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r"\b(flask|requests)==([^\s\"']+)", text)
    }


def _uv_lock_version(pkg):
    m = re.search(
        rf'name = "{re.escape(pkg)}"\s*\nversion = "([^"]+)"', UV_LOCK.read_text()
    )
    return m.group(1) if m else None


def test_ical_proxy_dockerfile_pins_match_uv_lock():
    pins = _dockerfile_pins()
    # Both deps must stay pinned — an unpinned `pip install flask` re-opens the skew this guards.
    assert set(pins) == set(_TRACKED), (
        f"ical-proxy Dockerfile must pin exactly {list(_TRACKED)}; found {pins}"
    )
    for pkg in _TRACKED:
        locked = _uv_lock_version(pkg)
        assert locked is not None, f"{pkg} not found in uv.lock"
        assert pins[pkg] == locked, (
            f"{pkg} pinned to {pins[pkg]} in ical-proxy Dockerfile but uv.lock has {locked} — "
            f"bump the Dockerfile pin so the test env matches the runtime image"
        )
