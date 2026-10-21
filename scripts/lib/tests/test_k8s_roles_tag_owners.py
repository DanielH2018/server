"""`tag_owners` maps each tag whose containers_list entry sets `owner` to that value.

The script reader of D2's ownership flag (docs/target-architecture/deploy/d2-first-leaf.md).
"""

import pytest

from lib.k8s_roles import tag_owners

HOST_VARS = """\
containers_list:
  - name: bento-pdf
    platform: k8s
    owner: flux
  - name: n8n
    platform: k8s
    owner: released
    tags: [n8n, n8n-images]
  - name: sonarr
    platform: k8s
"""


def test_both_owner_values_map_every_tag_of_their_entry(tmp_path):
    host_vars = tmp_path / "daniel-box.yml"
    host_vars.write_text(HOST_VARS)
    assert tag_owners(host_vars) == {
        "bento-pdf": "flux",
        "n8n": "released",
        "n8n-images": "released",
    }


@pytest.mark.parametrize("value", ["ansible", "Flux", "", "~", "[flux]"])
def test_any_other_owner_value_raises(tmp_path, value: str):
    host_vars = tmp_path / "daniel-box.yml"
    host_vars.write_text(HOST_VARS.replace("owner: flux", f"owner: {value}"))
    with pytest.raises(ValueError, match="bento-pdf"):
        tag_owners(host_vars)


def test_the_entries_d2_to_d5_moved_are_live_and_owned_by_flux():
    """D2.7 through D5.20, n8n's flip (#5546), nut's (D5.17), #5594 and #5652 moved these forty.

    D5.19 moved artifacts, autofix-bridge, cloudflare-ddns, docs and game-stats, #5594 moved
    monitor-bridge and prowlarr, #5652 moved karakeep, media-volume and pi-peer-backup, Wave A's last PR moved
    blackbox-exporter, and D5.10 moved zigbee2mqtt.

    A floor, not an equality: from D5.5 on the scaffolder writes `owner: flux` on every new
    entry, so the live set grows with each new service.
    """
    assert {
        "artifacts",
        "autofix-bridge",
        "bazarr",
        "bento-pdf",
        "blackbox-exporter",
        "cloudflare-ddns",
        "code-server",
        "deploy-ui",
        "docs",
        "freshrss",
        "game-stats",
        "gpu-exporter",
        "homelab-mcp",
        "homepage",
        "ical-proxy",
        "karakeep",
        "littlelink",
        "livesync",
        "loki-homelab",
        "mosquitto",
        "longhorn-ui",
        "media-volume",
        "monitor-bridge",
        "n8n",
        "navidrome",
        "node-exporter",
        "nut",
        "nut-exporter",
        "peanut",
        "pi-peer-backup",
        "pihole-exporter",
        "prowlarr",
        "radarr",
        "scrutiny",
        "sonarr",
        "speedtest",
        "terraria",
        "texbrain",
        "valheim",
        "zigbee2mqtt",
    } <= tag_owners().keys()
