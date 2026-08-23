"""The host Python pin is exact, and tracks the repo's minor.

Two failure modes, both silent:

  * An UNPINNED `uv python install 3.14` resolves to whatever uv offers per host. That already
    happened: daniel-box carried 3.14.6 and daniel-server 3.14.5 with nothing requesting either.
    Once 18 host scripts run on it, a patch-level difference between the two hosts is a
    difference in what actually executes.
  * A pin that DRIFTS from `.python-version` puts the hosts on one minor while `uv run`, CI and
    the image pins move to the next — reintroducing the split-interpreter problem this migration
    exists to end, in the other direction.

`.python-version` is deliberately not edited by this plan; it is the source of truth this pin
follows. test_python_version_pins_in_lockstep already couples it to both workflows.
"""

from __future__ import annotations

import re

import yaml
from _helpers import REPO as _REPO


_ALL_VARS = _REPO / "ansible/inventory/group_vars/all.yml"
_PYTHON_VERSION = _REPO / ".python-version"


def _pin() -> str:
    return yaml.safe_load(_ALL_VARS.read_text())["host_python_version"]


def test_host_python_version_is_pinned_to_an_exact_patch():
    pin = _pin()
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin), (
        f"host_python_version is {pin!r}; it must be an exact patch version. An unpinned or "
        "minor-only pin lets uv resolve differently per host, which is how daniel-box ended up "
        "on 3.14.6 and daniel-server on 3.14.5."
    )


def test_host_python_pin_tracks_the_repo_minor():
    pin_minor = ".".join(_pin().split(".")[:2])
    repo_minor = ".".join(_PYTHON_VERSION.read_text().strip().split(".")[:2])
    assert pin_minor == repo_minor, (
        f"host_python_version is on {pin_minor} but .python-version is on {repo_minor}. The host "
        "interpreter must track the repo's minor, or host scripts and `uv run` diverge again."
    )
