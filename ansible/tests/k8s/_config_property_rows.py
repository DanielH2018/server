"""Rendered-manifest property rows whose subject is config a ConfigMap or an arg carries.

`test_rendered_properties.py` runs these rows beside its own. A row lives here only to keep that
file under the test-module length cap; the rules for what makes a row are in its docstring.
"""

import re

from lib import yaml_fast

from _k8s_render import pod_spec
from _property_table import Property

_PROMETHEUS_ROLE = "observability"


def _speedtest_scrape_job(role: str, tpl: str, doc: dict):
    if role != _PROMETHEUS_ROLE or doc.get("kind") != "ConfigMap":
        return
    config = doc.get("data", {}).get("prometheus.yml")
    if config is None:
        return
    for job in yaml_fast.safe_load(config)["scrape_configs"]:
        if job.get("job_name") == "speedtest":
            yield "speedtest", job


SPEEDTEST_TARGETS = [{"targets": ["speedtest.homelab.svc:80"]}]


def _speedtest_offence(role: str, job: dict) -> str | None:
    if job.get("metrics_path") != "/prometheus":
        return f"metrics_path is {job.get('metrics_path')!r}, not '/prometheus'"
    if job.get("static_configs") != SPEEDTEST_TARGETS:
        return f"targets are {job.get('static_configs')!r}, not {SPEEDTEST_TARGETS!r}"
    return None


def _docs_nginx_conf(role: str, tpl: str, doc: dict):
    if role == "docs" and doc.get("kind") == "ConfigMap":
        data = doc.get("data", {})
        if "default.conf" in data:
            yield "default.conf", data


NO_CACHE = 'add_header Cache-Control "no-cache";'
_CACHE_CONTROL = re.compile(r'add_header Cache-Control "([^"]*)"')


def effective_asset_cache_control(conf: str) -> str | None:
    """The Cache-Control `location /` answers with, under nginx's no-merge rule."""
    location = re.search(r"location / \{(.*?)\n\s*\}", conf, re.DOTALL)
    if location is None:
        return None
    if "add_header" in location.group(1):
        found = _CACHE_CONTROL.search(location.group(1))
        return found.group(1) if found else None
    outside_locations = re.sub(r"location [^{]*\{.*?\n\s*\}", "", conf, flags=re.DOTALL)
    found = _CACHE_CONTROL.search(outside_locations)
    return found.group(1) if found else None


def _docs_no_cache_offence(role: str, data: dict) -> str | None:
    conf = data["default.conf"]
    if NO_CACHE not in conf:
        return f"default.conf does not carry {NO_CACHE}"
    effective = effective_asset_cache_control(conf)
    if effective != "no-cache":
        return (
            f"`location /` answers Cache-Control {effective!r}: an add_header of its own "
            "drops the server-level policy"
        )
    return None


_DOCS_CONF = (
    'server {\n    add_header Cache-Control "no-cache";\n'
    "    location = /healthz {\n        return 200;\n    }\n"
    "    location / {\n        try_files $uri =404;\n    }\n}\n"
)


def _alloy_config(role: str, tpl: str, doc: dict):
    if role == "loki-homelab" and doc.get("kind") == "ConfigMap":
        data = doc.get("data", {})
        if "config.alloy" in data:
            yield "config.alloy", data


ALLOY_REQUIRED_FRAGMENTS = (
    # The containerd envelope strip. Omitting it broke terraria-stats' start-anchored parser.
    "stage.cri {}",
    # The Traefik drop stage, scoped to the rotate sidecar and nothing else.
    'selector = "{container=\\"access-log-rotate\\"}"',
    'drop_counter_reason = "traefik_routine_access_log"',
    '"DownstreamStatus\\":(200|204|304),.*\\"Duration\\":[0-9]{1,9},',
    # The HA cast refresh_token redaction, scoped the same way. Its behaviour (token gone,
    # `_handle_signal_show_view` marker intact) is test_alloy_redacts_ha_refresh_token.py.
    'selector = "{container=\\"home-assistant\\"}"',
    "stage.replace {",
    # Node scoping: Alloy has no __host__ filter, so this is what stops every node tailing
    # every pod's path.
    'field = "spec.nodeName=" + sys.env("HOSTNAME")',
    # The pod-stream label set.
    'target_label  = "container"',
    'target_label  = "pod"',
    'target_label  = "namespace"',
    'target_label = "machine"',
    'replacement   = "/var/log/pods/*$1/*.log"',
    'url = "http://loki-homelab:3100/loki/api/v1/push"',
)

ALLOY_REQUIRED_JOBS = ("k8s", "syslog", "authlog", "k8s-audit")


def _alloy_labels_offence(role: str, data: dict) -> str | None:
    config = data["config.alloy"]
    problems = [f"missing {f!r}" for f in ALLOY_REQUIRED_FRAGMENTS if f not in config]
    problems += [
        f"no job {job!r}"
        for job in ALLOY_REQUIRED_JOBS
        if not re.search(rf'"job"\s*=\s*"{job}"|replacement\s*=\s*"{job}"', config)
    ]
    if 'target_label = "app"' in config or '"app" =' in config:
        problems.append("an `app` label reaches Loki")
    return "; ".join(problems) or None


_ALLOY_CLEAN = "\n".join(
    ALLOY_REQUIRED_FRAGMENTS + tuple(f'"job" = "{job}"' for job in ALLOY_REQUIRED_JOBS)
)

_MOUNT_EXCLUDE_ARG = "--collector.filesystem.mount-points-exclude="

_MOUNTS_TO_EXCLUDE = (
    "/proc",
    "/sys",
    "/dev",
    "/host",
    "/etc",
    "/var/lib/kubelet/pods",
    "/var/lib/kubelet/pods/abc-123/volumes/kubernetes.io~empty-dir/cache",
)

# The point of the narrowed exclusion: CSI global mounts must be SCRAPED.
_MOUNTS_TO_SCRAPE = (
    "/var/lib/kubelet/plugins/kubernetes.io/csi/driver.longhorn.io/abc/globalmount",
    "/var/lib/kubelet/plugins_registry",
)


def _node_exporter_mount_exclusion(role: str, tpl: str, doc: dict):
    if role != "node-exporter" or doc.get("kind") != "DaemonSet":
        return
    for container in pod_spec(doc).get("containers", []):
        for arg in container.get("args", []):
            if arg.startswith(_MOUNT_EXCLUDE_ARG):
                yield container["name"], {"pattern": arg[len(_MOUNT_EXCLUDE_ARG) :]}


def _mount_exclusion_offence(role: str, subject: dict) -> str | None:
    pattern = re.compile(subject["pattern"])
    if kept := [p for p in _MOUNTS_TO_EXCLUDE if not pattern.match(p)]:
        return f"{pattern.pattern!r} no longer excludes {kept}"
    if hidden := [p for p in _MOUNTS_TO_SCRAPE if pattern.match(p)]:
        return (
            f"{pattern.pattern!r} excludes {hidden}: the #1243 regression, where an absent "
            "node_filesystem_readonly series reads as healthy rather than blind"
        )
    return None


CONFIG_PROPERTIES = (
    Property(
        name="speedtest-scraped-at-its-native-endpoint",
        reason=(
            "monitor-bridge's `speedtest` check pushes a verdict to Kuma: a tile, not a series. "
            "Only a Prometheus scrape of speedtest-tracker answers whether a slow result is a "
            "one-off or a week-long slide (#996). The app serves `/prometheus` natively, so the "
            "job in observability's prometheus.yaml.j2 is the whole integration, and a wrong "
            "path or target leaves a silently empty or 404ing target."
        ),
        select=_speedtest_scrape_job,
        offence=_speedtest_offence,
        red=(
            _PROMETHEUS_ROLE,
            {"metrics_path": "/metrics", "static_configs": SPEEDTEST_TARGETS},
        ),
        green=(
            _PROMETHEUS_ROLE,
            {"metrics_path": "/prometheus", "static_configs": SPEEDTEST_TARGETS},
        ),
        must_find=frozenset({"speedtest"}),
    ),
    Property(
        name="docs-nginx-revalidates-assets",
        reason=(
            "Nothing this repo writes into the docs tree is content-hashed, so a browser or an "
            "edge that caches a script past a rebuild runs the old bundle against the new page. "
            "`no-cache` is revalidate-before-use (an unchanged file still answers 304), and it "
            "sits at `server` level so `location /` inherits it. nginx's `add_header` does not "
            "merge across levels: a location declaring ANY `add_header` drops every inherited "
            "one, which is why `/build-info.json` restates its policy and `location /` must "
            "declare none."
        ),
        select=_docs_nginx_conf,
        offence=_docs_no_cache_offence,
        red=(
            "docs",
            {
                "default.conf": _DOCS_CONF.replace(
                    "try_files", "add_header X-Frame-Options DENY;\n        try_files"
                )
            },
        ),
        green=("docs", {"default.conf": _DOCS_CONF}),
        must_find=frozenset({"default.conf"}),
    ),
    Property(
        name="alloy-emits-the-loki-labels-consumers-select-on",
        reason=(
            "monitor-bridge's selectors, terraria-stats' and valheim-stats' `{container=...}` "
            'queries and the `{job="syslog"}` deploy annotation on every dashboard select on '
            "`job`, `container`, `pod`, `namespace`, `machine` and `stream`, and on the ABSENCE "
            "of `app`. A missing `machine` or a stray `app` is a shipped-blind bug: the HA ban "
            "check went blind that way on 2026-08-23. The config is River, not YAML, so the "
            "row reads the rendered ConfigMap text."
        ),
        select=_alloy_config,
        offence=_alloy_labels_offence,
        red=("loki-homelab", {"config.alloy": _ALLOY_CLEAN + '\n"app" = "x"'}),
        green=("loki-homelab", {"config.alloy": _ALLOY_CLEAN}),
        must_find=frozenset({"config.alloy"}),
    ),
    Property(
        name="node-exporter-scrapes-csi-global-mounts",
        reason=(
            "A wholesale exclusion of `var/lib/kubelet` hides CSI global mounts from "
            "node_filesystem_readonly, and `checks.storage.check_kubelet_plugin_readonly` reads "
            "green on an EMPTY vector by design. So a regex that widens back to excluding "
            "`var/lib/kubelet/plugins` is invisible to the check: node-exporter stays up, the "
            "family goes empty, and the monitor reads healthy forever (#1243). The pattern must "
            "still exclude the unbounded pod paths, which is what it exists for."
        ),
        select=_node_exporter_mount_exclusion,
        offence=_mount_exclusion_offence,
        # The pre-#1243 pattern, which hid the CSI global mounts.
        red=(
            "node-exporter",
            {"pattern": r"^/(sys|proc|dev|host|etc|var/lib/kubelet)($|/)"},
        ),
        green=(
            "node-exporter",
            {"pattern": r"^/(sys|proc|dev|host|etc|var/lib/kubelet/pods)($|/)"},
        ),
        must_find=frozenset({"node-exporter"}),
    ),
)
