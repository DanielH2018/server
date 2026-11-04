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
    [{"id": "x", "name": "the retired override"}, {}, "off", True, None],
)
def test_ingress_probe_targets_is_flagged_on_a_probe_key_other_than_false(probe):
    with pytest.raises(ValueError, match="newsvc"):
        ingress_probe_targets([{**_NEW, "probe": probe}])


def test_ingress_probe_targets_is_clean_on_probe_false_and_on_no_probe_key():
    assert ingress_probe_targets([{**_NEW, "probe": False}]) == []
    assert ingress_probe_targets([_NEW]) == [
        {"hostname": "newsvc", "behind_sso": False, "entry": "newsvc"}
    ]


# ── error shape (#4505) ─────────────────────────────────────────────────────────────────────


def test_a_refusal_is_an_ansible_filter_error_named_for_its_filter():
    """A render failure names the filter that refused, through the filter Ansible calls."""
    entries = [{"name": "svc", "hostname": "svc", "probe": "yes"}]
    with pytest.raises((ValueError, AnsibleFilterError)) as caught:
        FilterModule().filters()["ingress_probe_targets"](entries)
    assert isinstance(caught.value, AnsibleFilterError), type(caught.value)
    assert str(caught.value).startswith("ingress_probe_targets: "), str(caught.value)
