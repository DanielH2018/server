"""`k8s_tags` names every tag of every k8s entry in the service list, each one Flux applies.

Every k8s entry is Flux's, so no key on the entry marks it
(docs/target-architecture/host/h1-cluster-config-to-flux.md, H1.9).
"""

from lib.k8s_roles import k8s_tags

SERVICES = """\
containers_list:
  - name: bento-pdf
    platform: k8s
  - name: n8n
    platform: k8s
    tags: [n8n, n8n-images]
  - name: sonarr
    platform: k8s
"""


def test_k8s_tags_names_every_tag_of_every_k8s_entry(tmp_path):
    services = tmp_path / "services.yml"
    services.write_text(SERVICES + "  - name: wg-easy\n    platform: docker\n")
    assert k8s_tags(services) == {"bento-pdf", "n8n", "n8n-images", "sonarr"}


def test_the_entries_d2_to_d5_moved_are_live_k8s_tags():
    """D2.7 through D5.20, n8n's flip (#5546), nut's (D5.17), #5594 and #5652 moved these forty-two.

    D5.19 moved artifacts, autofix-bridge, cloudflare-ddns, docs and game-stats, #5594 moved
    monitor-bridge and prowlarr, #5652 moved karakeep, media-volume and pi-peer-backup, Wave A's last PR moved
    blackbox-exporter, D5.10 moved zigbee2mqtt, D5.11 moved home-assistant, and D5.12 moved wg-easy.

    A floor, not an equality: the live set grows with each new service.
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
        "home-assistant",
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
        "wg-easy",
    } <= k8s_tags()
