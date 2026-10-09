"""Uptime Kuma's ingress tiles derive from `containers_list` (#3690).

`static-monitors.yaml.j2` renders one http tile per entry with a `hostname`, through the
`kuma_ingress_monitors` filter (`ansible/filter_plugins/kuma_monitors.py`). These guards render
the Secret from the real inventory with an entry added or changed, so each states what the pod
would receive.

Run: uv run pytest ansible/tests/services/test_kuma_ingress_monitors.py
"""

import copy
import re

import pytest

from _k8s_render import host_context, render_role_template
from _kuma_entities import entities_with
from kuma_monitors import kuma_ingress_monitors

_NEW = {"name": "newsvc", "platform": "k8s", "hostname": "newsvc", "port": 8080}


def _with(*changes: dict, drop_kuma: str | None = None) -> dict[str, dict]:
    """The rendered entity set, with `changes` appended to containers_list."""
    entries = copy.deepcopy(host_context()["containers_list"])
    if drop_kuma:
        entry = next(e for e in entries if e["name"] == drop_kuma)
        entry["kuma"] = False
    return entities_with({"containers_list": [*entries, *changes]})


def test_a_new_entry_with_a_hostname_gets_a_tile_with_no_template_edit():
    tile = _with({**_NEW, "use_authelia": True, "auth_tier": "one_factor"})[
        "newsvc-k8s.json"
    ]
    assert tile["type"] == "http"
    assert tile["name"] == "k3s newsvc"
    assert tile["url"].startswith("https://newsvc.local.")
    assert tile["accepted_statuscodes"] == ["302"]
    assert tile["max_redirects"] == 0
    assert tile["notification_name_list"]


def test_an_entry_outside_authelia_expects_the_default_status():
    tile = _with(_NEW)["newsvc-k8s.json"]
    assert "accepted_statuscodes" not in tile
    assert "max_redirects" not in tile


def test_kuma_false_opts_an_entry_out():
    assert "grafana-k8s.json" in _with()
    assert "grafana-k8s.json" not in _with(drop_kuma="observability")


def test_the_override_keeps_a_hand_picked_id_and_name():
    tile = _with()["auth-k8s.json"]
    assert tile["name"] == "k3s Authelia Portal"


# Every ingress id that existed when the tiles became derived (#3690). AutoKuma runs
# ON_DELETE=delete (#2076), so an id that stops rendering deletes the monitor and its history.
# Most of these ids derive from the entry's `name`, so renaming an entry would do exactly that.
_PINNED_IDS = frozenset(
    {
        "auth-k8s",
        "bento-pdf-k8s",
        "littlelink-k8s",
        "speedtest-k8s",
        "freshrss-k8s",
        "headlamp-k8s",
        "zigbee2mqtt-k8s",
        "homepage-k8s",
        "peanut-k8s",
        "grafana-k8s",
        "home-assistant-k8s",
        "karakeep-k8s",
        "code-server-k8s",
    }
)


def test_every_pinned_ingress_id_still_renders():
    generated = {
        t["id"] for t in kuma_ingress_monitors(host_context()["containers_list"])
    }
    lost = sorted(_PINNED_IDS - generated)
    assert not lost, (
        f"{lost} no longer render, and AutoKuma would delete each monitor with its history. "
        "If an entry was renamed, give it `kuma: {id: <old stem>}`; drop an id here only when "
        "its service is retired"
    )


def test_no_generated_id_collides_with_a_hand_written_one():
    """A duplicate Secret key renders as valid YAML, and the later tile silently replaces the earlier."""
    rendered = render_role_template("uptime-kuma", "static-monitors.yaml.j2")
    ids = re.findall(r"^  ([\w-]+)\.json: \|$", rendered, re.M)
    # One generated id and one hand-written id, so a key pattern that stopped matching fails.
    assert {"grafana-k8s", "livesync-k8s"} <= set(ids)
    assert len(ids) == len(set(ids)), sorted(i for i in set(ids) if ids.count(i) > 1)


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
