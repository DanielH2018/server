"""The `kuma_ingress_monitors` filter refuses a malformed `kuma:` key (#3690).

The filter (`ansible/filter_plugins/kuma_monitors.py`) yields one tile per `containers_list`
entry with a `hostname`. Uptime Kuma rendered those tiles until M4's PR 4 (#5625). The
`blackbox-ingress` scrape job still builds its targets from them, and
`test_blackbox_ingress_targets.py` covers what the job renders. These guards cover the filter
itself.

Run: uv run pytest ansible/tests/services/test_kuma_ingress_monitors.py
"""

import pytest

from ansible.errors import AnsibleFilterError
from kuma_monitors import FilterModule, kuma_ingress_monitors

_NEW = {"name": "newsvc", "platform": "k8s", "hostname": "newsvc", "port": 8080}


@pytest.mark.parametrize(
    "kuma",
    [{"display": "typo for name"}, "off", True],
)
def test_kuma_ingress_monitors_is_flagged_on_a_malformed_kuma_key(kuma):
    with pytest.raises(ValueError, match="newsvc"):
        kuma_ingress_monitors([{**_NEW, "kuma": kuma}])


def test_kuma_ingress_monitors_is_clean_on_an_override_and_forces_the_suffix():
    [tile] = kuma_ingress_monitors([{**_NEW, "kuma": {"id": "other", "name": "N"}}])
    assert (tile["id"], tile["name"]) == ("other-k8s", "N")


# ── error shape (#4505) ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kuma",
    ["yes", {"id": "x", "colour": "red"}],
    ids=["not-a-mapping", "unknown-key"],
)
def test_a_refusal_is_an_ansible_filter_error_named_for_its_filter(kuma):
    """A render failure names the filter that refused, through the filter Ansible calls."""
    entries = [{"name": "svc", "hostname": "svc", "kuma": kuma}]
    with pytest.raises((ValueError, AnsibleFilterError)) as caught:
        FilterModule().filters()["kuma_ingress_monitors"](entries)
    assert isinstance(caught.value, AnsibleFilterError), type(caught.value)
    assert str(caught.value).startswith("kuma_ingress_monitors: "), str(caught.value)
