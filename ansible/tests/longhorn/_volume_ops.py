"""Longhorn-API contract assertions shared by k8s/volume-revert and k8s/volume-snapshot.

The two roles carry two genuinely identical contracts, extracted here: every maintenance-mode
API call pins a single status code, and every role declares its autodeploy stance. Other
apparent pairs between the two test files — the detach's body shape, the mutating-task census,
the listing jsonpath's unreachable-cluster detection — were checked and found to differ in what
they assert (one checks `hostId` absence, the other the whole body is empty; one is a
string-match census, the other a task-walk census; the unreachable-cluster token lists and
timeouts diverge). Those stay local to their own file rather than being forced into a shared
shape that would weaken one side or hide a deliberate difference.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from _helpers import load_tasks


def assert_every_api_call_pins_a_single_status_code(claim_path: Path) -> None:
    """A range accepts a 2xx that did not do the work.

    Longhorn answers a successful action with 200, so 200 is what each call demands.
    """
    for task in load_tasks(claim_path):
        uri = task.get("ansible.builtin.uri")
        if uri is None:
            continue
        assert uri["status_code"] == 200, task["name"]
        assert uri["url"].startswith("{{ longhorn_api }}/v1/volumes/"), task["name"]


def assert_the_role_declares_an_autodeploy_stance(defaults_path: Path) -> None:
    """Every role under roles/k8s/ must declare `k8s_autodeploy`; the denylist is derived from
    those declarations."""
    defaults = yaml.safe_load(defaults_path.read_text())
    assert defaults["k8s_autodeploy"] is False
    assert defaults["k8s_autodeploy_reason"].strip()
