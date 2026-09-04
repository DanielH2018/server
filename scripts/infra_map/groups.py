"""The functional grouping behind the workload strip under the diagram.

The one part of the page that is hand-kept rather than derived from the inventory,
which is why it sits apart from the views that draw it. It imports nothing.
"""

# Anything unlisted falls into "Other" and stays visible, so a new service shows up as
# ungrouped instead of silently vanishing from the page.
SERVICE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Edge & identity",
        ("traefik", "authelia", "crowdsec", "cloudflare-ddns", "pihole", "headlamp"),
    ),
    (
        "Media",
        (
            "jellyfin",
            "sonarr",
            "radarr",
            "bazarr",
            "prowlarr",
            "qbittorrent",
            "tdarr",
            "configarr",
            "janitorr",
            "media-volume",
            "volume-claim",
        ),
    ),
    (
        "Home automation",
        ("home-assistant", "zigbee2mqtt", "mosquitto", "ical-proxy", "peanut", "nut"),
    ),
    (
        "Observability",
        (
            "uptime-kuma",
            "loki-homelab",
            "claude-otel",
            "node-exporter",
            "scrutiny",
            "monitor-bridge",
            "autofix-bridge",
            "healthchecks",
            "speedtest",
            "rollout-drain",
        ),
    ),
    (
        "Apps & tooling",
        (
            "freshrss",
            "karakeep",
            "littlelink",
            "bento-pdf",
            "homepage",
            "n8n",
            "n8n-images",
            "code-server",
            "livesync",
            "homelab-mcp",
            "registry",
            "image-builder",
        ),
    ),
    ("Games", ("terraria", "terraria-stats", "valheim", "valheim-stats")),
    (
        "Storage & backup",
        ("longhorn-ui", "pi-peer-backup", "dri-device-plugin"),
    ),
)


def group_services(model: dict) -> list[dict]:
    """Bucket every service into a functional group for the diagram strip."""
    by_group: dict[str, list[dict]] = {name: [] for name, _ in SERVICE_GROUPS}
    by_group["Pi · LAN-only"] = []
    by_group["Other"] = []
    lookup = {name: group for group, names in SERVICE_GROUPS for name in names}

    for service in model["services"]:
        if service["platform"] == "docker":
            by_group["Pi · LAN-only"].append(service)
        else:
            by_group[lookup.get(service["name"], "Other")].append(service)

    groups = []
    for name, services in by_group.items():
        if not services:
            continue
        groups.append(
            {
                "name": name,
                "services": sorted(services, key=lambda s: s["name"]),
                "healthy": sum(1 for s in services if s["status"] == "healthy"),
            }
        )
    return groups
