#!/usr/bin/env python3
"""Regroup ansible/vars/secrets.yml into commented sections, sorted within each.

Run as the SOPS editor, so SOPS owns decrypt / re-encrypt / re-MAC:

    EDITOR='python3 scripts/sops_reorg.py' sops ansible/vars/secrets.yml

Values are moved verbatim and never parsed or reformatted -- only line order and
section comments change. Verify the round-trip with a digest rather than by reading
the file back:

    sops -d ansible/vars/secrets.yml | grep -E '^[a-zA-Z_]' | sort | sha256sum

Identical digests before and after mean nothing but order and comments moved. This
matters here because the repo's SOPS diff driver can't decrypt in every environment,
so `git diff` on this file is not a reliable check.

The file is an append log below the early hand-maintained sections: keys landed in
the order features shipped, and ~30 monitor_bridge push tokens ended up scattered.
"""

import re
import sys

# Audited 2026-08-15: no template, task, script, or hook references any of these,
# and none is a login anyone types by hand.
PRUNE = {
    "traefik_user",
    "traefik_password",
    "monitor_bridge_docker_user_push_token",
    "monitor_bridge_cloudflare_drift_push_token",
    "authelia_beszel_password_hash",
}


def _pre(*prefixes):
    return lambda k: k.startswith(prefixes)


def _exact(*names):
    names = set(names)
    return lambda k: k in names


# (title, predicate). First match wins, so narrow rules sit above broad ones.
# An empty section is not emitted.
SECTIONS = [
    ("General", _exact("domain", "email", "become_password")),
    (
        "Monitor bridge - Kuma push tokens",
        lambda k: k.startswith("monitor_bridge_") and k.endswith("_push_token"),
    ),
    ("Monitor bridge - other credentials", _pre("monitor_bridge_")),
    ("Other Kuma push tokens", lambda k: k.endswith("_push_token")),
    ("Cloudflare", _pre("cloudflare_")),
    ("Authelia / SSO", _pre("authelia_")),
    (
        "CrowdSec (crowdsec_username/password are the Metabase dashboard login)",
        _pre("crowdsec_"),
    ),
    (
        "Backups - Backblaze B2 (kopia_b2_* are Longhorn's credentials now)",
        _pre("kopia_"),
    ),
    ("Backups - Cloudflare R2", _pre("r2_")),
    ("Backups - other", _exact("pi_peer_backup_ssh_key", "longhorn_backup_push_token")),
    (
        "Monitoring - Grafana / Prometheus / Loki / Scrutiny",
        _pre("grafana_", "scrutiny_", "prometheus_"),
    ),
    ("Monitoring - Uptime Kuma", _pre("uptime_kuma_")),
    ("Monitoring - Healthchecks.io", _pre("healthchecks_")),
    (
        "Home Assistant",
        lambda k: (
            k.endswith("_ha_token")
            or k in {"nut_ha_password", "google_assistant_service_account"}
        ),
    ),
    ("Mosquitto / Zigbee", _pre("mqtt_", "zigbee_")),
    ("UPS / NUT", _pre("nut_", "peanut_")),
    (
        "Media - *arr stack",
        _pre(
            "sonarr_",
            "radarr_",
            "prowlarr_",
            "arr_",
            "jellyfin_",
            "configarr_",
            "janitorr_",
        ),
    ),
    ("Media - qBittorrent (WebUI login, typed by hand)", _pre("qbittorrent_")),
    ("Media - other", _pre("karakeep_", "freshrss_")),
    (
        "Networking - WireGuard / VPN",
        lambda k: (
            k.startswith(("wireguard_", "wg_easy_", "mullvad_"))
            or k == "speedtest_app_key"
        ),
    ),
    ("Pi-hole", _pre("pihole_")),
    ("Raspberry Pi", _pre("pi_")),
    (
        "Game servers (terraria_password is the live server password)",
        _pre("terraria_", "valheim_"),
    ),
    ("Claude tooling", _pre("claude_", "homelab_mcp")),
    (
        "GitOps / automation",
        _pre("gitops_", "secret_rotation_", "manifest_prune_", "n8n_", "livesync_"),
    ),
    (
        "Notifications",
        lambda k: (
            "webhook" in k
            or k.startswith("smtp_")
            or k == "monitor_discord_webhook_url"
        ),
    ),
    ("Calendars", _pre("calendar_")),
    ("Third-party API keys", _exact("weather_api_key", "coinmarket_api_key")),
    ("Apps", lambda k: k.startswith("code_server_") or k == "handy_master_secret"),
    ("Everything else", lambda k: True),
]

KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")

# Comments that only ever labelled a section this script regenerates. Any other
# comment is a real annotation and rides along with the key beneath it.
STALE_COMMENTS = {
    "general details",
    "traefik",
    "portainer",
    "cloudflare",
    "authelia",
    "code server",
    "pihole",
    "crowdsec",
    "uptime kuma",
    "monitor bridge",
    "healthchecks",
    "kopia",
    "sonarr api key",
    "radarr api key",
    "jellyfin api key",
    "qbittorrent login",
    "freshrss login",
    "wireguard conf",
    "speedtest",
    "terraria",
    "n8n",
    "livesync",
    "nut",
    "karakeep",
    "grafana",
    "git ops",
    "mosquitto/zigbee",
    "calendar links",
    "open weather api key",
    "coin market cap api key",
}


def parse(lines):
    """-> (records, trailing), where a record is (key, lines) with comments attached."""
    records, pending, trailing = [], [], []
    for line in lines:
        match = KEY_RE.match(line)
        if match:
            records.append((match.group(1), pending + [line]))
            pending = []
        elif line.startswith("#"):
            if line.lstrip("# ").strip().rstrip(":").lower() not in STALE_COMMENTS:
                pending.append(line)
        elif not line.strip():
            continue  # blank lines are regenerated between sections
        elif records:
            records[-1][1].append(line)  # block-scalar continuation
        else:
            trailing.append(line)
    return records, pending + trailing


def reorganize(text):
    records, leftover = parse(text.splitlines())
    kept = [rec for rec in records if rec[0] not in PRUNE]

    buckets = {title: [] for title, _ in SECTIONS}
    for key, body in kept:
        for title, matches in SECTIONS:
            if matches(key):
                buckets[title].append((key, body))
                break

    out = []
    for title, _ in SECTIONS:
        if not buckets[title]:
            continue
        if out:
            out.append("")
        out.append("# " + title)
        for _, body in sorted(buckets[title], key=lambda entry: entry[0]):
            out.extend(body)
    out.extend(leftover)

    return "\n".join(out) + "\n", len(records) - len(kept)


def main(path):
    with open(path) as handle:
        text = handle.read()

    result, dropped = reorganize(text)

    with open(path, "w") as handle:
        handle.write(result)

    print("reorganized: %d keys pruned" % dropped, file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
