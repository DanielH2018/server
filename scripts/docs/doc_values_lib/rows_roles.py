"""The value rows whose docs are a k8s role's `CLAUDE.md`."""

import json
import re
from pathlib import Path as _Path

from lib import yaml_fast
from docs.doc_values_lib.readers import (
    Reader,
    cardinal,
    constant,
    count,
    dependents,
    key,
    match,
    metrics_port,
    per,
    phrase,
    rows,
    service_port,
    signed,
    weekly_shard,
)

_SERVICES = "k8s/services.yml"
_PIHOLE_NETPOL = "k8s/roles/netpol-baseline/templates/networkpolicy-pihole.yaml.j2"
_ADMIN_UI_PORT = r"# The admin UI[\s\S]*?- port: (\d+)"
_NODE_EXPORTER = "k8s/roles/node-exporter/CLAUDE.md"
_CONFIGARR = "k8s/roles/configarr/CLAUDE.md"
_CONFIGARR_CONFIG = "k8s/roles/configarr/templates/config/config.yml.j2"
_LOKI = "k8s/roles/loki-homelab/CLAUDE.md"


def _cf_score(trash_id: str) -> Reader:
    return match(rf"- {trash_id}\n[\s\S]*?score: (-?\d+)", signed)


def _role(name: str, path: str) -> str:
    return f"k8s/roles/{name}/{path}"


def _rungs(path: _Path) -> str:
    """The free-disk keys every `*-expiration` map holds, as "5, 10 and 25"."""
    blocks = re.findall(r"-expiration:\n((?: +\d+: .*\n)+)", path.read_text())
    key_sets = {tuple(re.findall(r"(\d+):", block)) for block in blocks}
    if len(key_sets) != 1:
        raise ValueError(f"the expiration maps disagree on their rungs: {key_sets}")
    *head, last = key_sets.pop()
    return f"{', '.join(head)} and {last}"


def _captured(pattern: str, text: str) -> str:
    found = re.search(pattern, text)
    if found is None:
        raise ValueError(f"nothing matches /{pattern}/")
    return found.group(1)


def _startup_window_minutes(path: _Path) -> str:
    probe = _captured(r"startupProbe:\n((?: {12,}.*\n)+)", path.read_text())
    period = _captured(r"periodSeconds: (\d+)", probe)
    threshold = _captured(r"failureThreshold: (\d+)", probe)
    return cardinal(per(60)(str(int(period) * int(threshold))))


def _snapshot_cap_gib(path: _Path) -> str:
    defaults = yaml_fast.safe_load(path.read_text())
    factor = _captured(r"\* (\d+) \* 1024", defaults["jellyfin_k8s_snapshot_max_size"])
    return str(int(defaults["jellyfin_k8s_size"].removesuffix("Gi")) * int(factor))


def _scored_formats(path: _Path) -> str:
    return str(sum(1 for cf in json.loads(path.read_text()) if cf["score"]))


# source -> (doc, pattern, read) for each sentence that states a value from that source.
VALUES = rows(
    {
        "k8s/templates/wireguard-sidecar.yml.j2": [
            (
                _role("qbittorrent", "CLAUDE.md"),
                r"sidecar's (\w+)-minute startupProbe window",
                _startup_window_minutes,
            ),
        ],
        _role("qbittorrent", "files/qbittorrent_job.py"): [
            (
                _role("qbittorrent", "CLAUDE.md"),
                r"`last_external_address_v4` within (\d+) seconds",
                constant("EXTERNAL_WAIT_SECONDS"),
            ),
        ],
        "scripts/renderer/job_files/flux_job_connect.py": [
            (
                _role("arr-notification", "CLAUDE.md"),
                r"retries for (\d+) seconds",
                constant("RETRY_SECONDS"),
            ),
        ],
        _role("janitorr", "templates/config/application.yml.j2"): [
            (
                _role("janitorr", "CLAUDE.md"),
                r"The rungs are ([\d, ]+\s+and \d+)",
                _rungs,
            ),
        ],
        "hosts/components/k3s-host-checks/defaults/main.yml": [
            (
                _role("janitorr", "CLAUDE.md"),
                r"threshold of (\d+)% used",
                key("k3s_disk_max_pct"),
            ),
        ],
        _CONFIGARR_CONFIG: [
            (
                _CONFIGARR,
                r"\*\*([-+]\d+)\*\* \| rejected",
                _cf_score("local-fake-remux-groups"),
            ),
            (
                _CONFIGARR,
                r"\*\*([-+]\d+)\*\* \| preferred on upgrade",
                _cf_score("local-anime-trusted-groups"),
            ),
            (
                _CONFIGARR,
                r"\*\*([-+]\d+)\*\* \| prefer releases",
                _cf_score("local-anime-english-sub-groups"),
            ),
            (
                _CONFIGARR,
                r"\*\*([-+]\d+)\*\* \| milder",
                _cf_score("local-anime-multisub-tags"),
            ),
        ],
        _role("configarr", "files/baseline/anime-cf-scores.json"): [
            (_CONFIGARR, r"\*\*(\d+) scored bespoke", _scored_formats),
            (_CONFIGARR, r"the (\d+) bespoke CFs", _scored_formats),
            (_CONFIGARR, r"lists the (\d+) non-zero", _scored_formats),
        ],
        _role("configarr", "files/baseline/anime-profile.json"): [
            (
                _CONFIGARR,
                r"`cutoffFormatScore` is (\d+)",
                lambda path: str(json.loads(path.read_text())["cutoffFormatScore"]),
            ),
        ],
        _SERVICES: [
            (
                _role("pihole-exporter", "CLAUDE.md"),
                r"serving `pihole_\*` on `:(\d+)`",
                metrics_port("pihole-exporter"),
            ),
            (
                _role("pihole", "CLAUDE.md"),
                r"web UI only\*\* \(port (\d+)\)",
                service_port("pihole"),
            ),
            (
                _role("nut-exporter", "CLAUDE.md"),
                r"`network_ups_tools_\*` on `:(\d+)`",
                metrics_port("nut-exporter"),
            ),
            (
                _role("gpu-exporter", "CLAUDE.md"),
                r"`homelab_gpu_\*` on `:(\d+)`",
                service_port("gpu-exporter"),
            ),
            (
                _role("media-volume", "CLAUDE.md"),
                r"the volume (\w+) media-stack roles mount",
                dependents("media-volume"),
            ),
            (
                _role("pi-peer-backup", "CLAUDE.md"),
                r"weekly shard \((weekly-backup-d\d+)\)",
                weekly_shard("pi-peer-backup"),
            ),
            (
                _role("docs", "CLAUDE.md"),
                r'curl -s "http://\$IP:(\d+)/"',
                service_port("docs"),
            ),
            (
                _role("game-stats", "CLAUDE.md"),
                r"exposes Prometheus metrics on :(\d+)\.",
                metrics_port("game-stats"),
            ),
            (
                _role("game-stats", "CLAUDE.md"),
                r"where `:(\d+)` is defined",
                metrics_port("game-stats"),
            ),
        ],
        _role("pihole", "templates/precheck/guard.yaml.j2"): [
            (
                _role("pihole", "CLAUDE.md"),
                r"endpoint for (\d+)s, so Flux",
                match(r'GUARD_WAIT_SECONDS\n +value: "(\d+)"'),
            ),
            (
                _role("pihole", "CLAUDE.md"),
                r"inside the (\d+)s wait",
                match(r'GUARD_WAIT_SECONDS\n +value: "(\d+)"'),
            ),
        ],
        _PIHOLE_NETPOL: [
            (
                _role("pihole-exporter", "CLAUDE.md"),
                r"networkpolicy-pihole\.yaml\.j2`, on port (\d+)",
                match(_ADMIN_UI_PORT),
            ),
            (
                _role("pihole", "CLAUDE.md"),
                r"`app: pihole-gravity`, on\s+port (\d+)\.",
                match(_ADMIN_UI_PORT),
            ),
        ],
        _role("loki-homelab", "files/rules/homelab-logs.yaml"): [
            (
                _role("cloudflare-ddns", "CLAUDE.md"),
                r"for (\d+) minutes\. The pod name",
                match(r"UpdateSilent\n.*\[(\d+)m\]"),
            ),
            (_LOKI, r"evaluates every (\w+)", match(r"^ +interval: (\S+)$")),
        ],
        _role("loki-homelab", "defaults/main.yml"): [
            (_LOKI, r"`longhorn-nobackup`, (\w+)\)", key("loki_homelab_size")),
            (_LOKI, r"logs, `(\w+)` retention", key("loki_homelab_retention")),
        ],
        _role("loki-homelab", "templates/configmap.yaml.j2"): [
            (
                _LOKI,
                r"on its Service at port (\d+)",
                match(r"alertmanager_url: .*:(\d+)$"),
            ),
        ],
        _role("flux", "templates/networkpolicy.yaml.j2"): [
            (
                _role("flux", "CLAUDE.md"),
                r"the metrics port (\d+)",
                match(r"port: (\d+)"),
            ),
        ],
        "secrets/secret_rotation.yml": [
            (
                _role("flux", "CLAUDE.md"),
                r"a (\d+)-day expiry",
                match(r"^  promote_token:\n(?: {4}.*\n)*? {4}tier: external-(\d+)d"),
            ),
        ],
        _role("cluster-base", "templates/priorityclass.yaml.j2"): [
            (
                _role("cluster-base", "CLAUDE.md"),
                r"^The (\w+) workload-tier PriorityClasses",
                count(r"^kind: PriorityClass$", cardinal),
            ),
        ],
        _role("metallb", "templates/metallb-pool.yaml.j2"): [
            (
                _role("metallb", "CLAUDE.md"),
                r"plus the (\w+) address pools",
                count(r"^kind: IPAddressPool$", cardinal),
            ),
        ],
        _role("crowdsec", "defaults/main.yml"): [
            (
                _role("crowdsec", "CLAUDE.md"),
                r"home allowlist every (\d+) minutes",
                key("crowdsec_k8s_home_ips_period_minutes"),
            ),
            (
                _role("crowdsec", "CLAUDE.md"),
                r"home_ips_discover\.py` every (\d+) minutes",
                key("crowdsec_k8s_home_ips_period_minutes"),
            ),
        ],
        _role("image-builder", "templates/build-job.yaml.j2"): [
            (
                _role("image-builder", "CLAUDE.md"),
                r"Runs as uid (\d+)",
                match(r"runAsUser: (\d+)"),
            ),
        ],
        _role("node-exporter", "templates/daemonset.yaml.j2"): [
            (
                _NODE_EXPORTER,
                r"CPU limit is (\d+) core",
                match(r"container_resources\(cpu_limit='(\d+)'"),
            ),
            (
                _NODE_EXPORTER,
                r"wget` on `/metrics` every (\d+)s",
                match(r"livenessProbe:\n(?:.*\n)*? +periodSeconds: (\d+)"),
            ),
            (
                _NODE_EXPORTER,
                r"at the usual (\d+)s",
                match(r"readinessProbe:\n(?:.*\n)*? +periodSeconds: (\d+)"),
            ),
        ],
        _role("peanut", "templates/deployment.yaml.j2"): [
            (
                _role("peanut", "CLAUDE.md"),
                r"`fsGroup: (\d+)`",
                match(r"fs_group=(\d+)"),
            ),
        ],
        _role("media-volume", "templates/verify/job.yaml.j2"): [
            (
                _role("media-volume", "CLAUDE.md"),
                r"until its (\d+)s deadline",
                match(r"activeDeadlineSeconds: (\d+)"),
            ),
        ],
        "scripts/lib/longhorn_lib/longhorn_backups.py": [
            (
                _role("longhorn-backup", "CLAUDE.md"),
                r"`weekly-backup-d0` to `weekly-backup-d(\d+)`",
                constant("WEEKLY_SHARDS", lambda n: str(int(n) - 1)),
            ),
        ],
        _role("ical-proxy", "templates/deployment.yaml.j2"): [
            (
                _role("ical-proxy", "CLAUDE.md"),
                r"refreshing every (\d+) min",
                match(r'REFRESH_INTERVAL\n +value: "(\d+)"', per(60)),
            ),
        ],
        _role("autofix-bridge", "templates/deployment.yaml.j2"): [
            (
                _role("autofix-bridge", "CLAUDE.md"),
                r"sidecar `DRY_RUN` \((\w+),",
                match(r'name: DRY_RUN\n +value: "(\w+)"', phrase({"false": "live"})),
            ),
        ],
        "inventory/host_vars/daniel-box.yml": [
            (
                _role("autofix-bridge", "CLAUDE.md"),
                r"run `(\w+)` by host_vars",
                key("autofix_fake_remux_replace_mode"),
            ),
        ],
        _role("slskd", "defaults/main.yml"): [
            (
                _role("slskd", "CLAUDE.md"),
                r"albums every (\d+) minutes",
                key("slskd_k8s_soularr_interval", per(60)),
            ),
        ],
        _role("tdarr", "defaults/main.yml"): [
            (
                _role("tdarr", "CLAUDE.md"),
                r"\*\*`tdarr-server`\*\* \((\w+),",
                key("tdarr_k8s_server_size"),
            ),
            (
                _role("tdarr", "CLAUDE.md"),
                r"`tdarr-configs` \((\w+),",
                key("tdarr_k8s_configs_size"),
            ),
            (
                _role("tdarr", "CLAUDE.md"),
                r"VAAPI device \(`([^`]+)`",
                key("tdarr_k8s_dri_resource"),
            ),
        ],
        _role("jellyfin", "defaults/main.yml"): [
            (
                _role("jellyfin", "CLAUDE.md"),
                r"jellyfin_k8s_size`, (\d+) GiB",
                _snapshot_cap_gib,
            ),
        ],
        _role("bento-pdf", "defaults/main.yml"): [
            (
                _role("bento-pdf", "CLAUDE.md"),
                r"bento_pdf_k8s_uid`, (\d+), matches",
                key("bento_pdf_k8s_uid"),
            ),
        ],
    }
)
