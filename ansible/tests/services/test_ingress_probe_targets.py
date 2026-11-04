"""The `ingress_probe_targets` filter refuses a malformed `probe:` key (#3690).

The filter (`ansible/filter_plugins/ingress_probes.py`) yields one target per `containers_list`
entry with a `hostname`. The `blackbox-ingress` scrape job builds its targets from them, and
`test_blackbox_ingress_targets.py` covers what the job renders. These guards cover the filter
itself.

Run: uv run pytest ansible/tests/services/test_ingress_probe_targets.py
"""

import pytest

from ansible.errors import AnsibleFilterError
from ingress_probes import FilterModule, ingress_probe_targets

_NEW = {"name": "newsvc", "platform": "k8s", "hostname": "newsvc", "port": 8080}


@pytest.mark.parametrize(
    "probe",
    [{"display": "typo for name"}, "off", True],
)
def test_ingress_probe_targets_is_flagged_on_a_malformed_probe_key(probe):
    with pytest.raises(ValueError, match="newsvc"):
        ingress_probe_targets([{**_NEW, "probe": probe}])


def test_ingress_probe_targets_is_clean_on_an_override_and_forces_the_suffix():
    [target] = ingress_probe_targets([{**_NEW, "probe": {"id": "other", "name": "N"}}])
    assert (target["id"], target["name"]) == ("other-k8s", "N")


# ── error shape (#4505) ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "probe",
    ["yes", {"id": "x", "colour": "red"}],
    ids=["not-a-mapping", "unknown-key"],
)
def test_a_refusal_is_an_ansible_filter_error_named_for_its_filter(probe):
    """A render failure names the filter that refused, through the filter Ansible calls."""
    entries = [{"name": "svc", "hostname": "svc", "probe": probe}]
    with pytest.raises((ValueError, AnsibleFilterError)) as caught:
        FilterModule().filters()["ingress_probe_targets"](entries)
    assert isinstance(caught.value, AnsibleFilterError), type(caught.value)
    assert str(caught.value).startswith("ingress_probe_targets: "), str(caught.value)
