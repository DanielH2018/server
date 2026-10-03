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


def _authelia_webauthn(role: str, tpl: str, doc: dict):
    if role != "authelia" or doc.get("kind") != "Secret":
        return
    if doc.get("metadata", {}).get("name") != "authelia-config":
        return
    raw = doc.get("stringData", {}).get("configuration.yml")
    webauthn = (yaml_fast.safe_load(raw or "") or {}).get("webauthn")
    if webauthn:
        yield "webauthn", webauthn


# The keys this role deliberately writes. Deliberately NARROWER than the surface Authelia 4.39.21
# accepts: the docs site describes a wider schema than any one release validates, and admitting
# `metadata` or `filtering` would admit the copy-paste this row exists to reject. Adding a key is
# the deliberate act: check it against the tag `authelia_k8s_image` names, in that tag's own
# `config.template.yml` and `internal/configuration/validator/webauthn.go`, then add it here.
WRITTEN_WEBAUTHN_KEYS = frozenset(
    {
        "disable",
        "enable_passkey_login",
        "display_name",
        "attestation_conveyance_preference",
        "timeout",
        "selection_criteria",
    }
)
WRITTEN_SELECTION_CRITERIA_KEYS = frozenset({"attachment", "user_verification"})


def _webauthn_offence(role: str, webauthn: dict) -> str | None:
    if webauthn.get("disable") is not False:
        return f"webauthn is not enabled (disable={webauthn.get('disable')!r})"
    if webauthn.get("enable_passkey_login"):
        return "enable_passkey_login makes WebAuthn a first factor"
    unchecked = sorted(set(webauthn) - WRITTEN_WEBAUTHN_KEYS) + sorted(
        set(webauthn.get("selection_criteria") or {}) - WRITTEN_SELECTION_CRITERIA_KEYS
    )
    if unchecked:
        return (
            f"carries {unchecked}, never checked against the pinned Authelia version: it "
            "renders, it parses, and the pod refuses to start on it"
        )
    return None


_GOOD_WEBAUTHN = {
    "disable": False,
    "enable_passkey_login": False,
    "display_name": "Authelia example.com",
    "attestation_conveyance_preference": "indirect",
    "timeout": "60 seconds",
    "selection_criteria": {"attachment": "", "user_verification": "preferred"},
}


def _pihole_dns_config(role: str, tpl: str, doc: dict):
    if role == "pihole" and doc.get("kind") == "ConfigMap":
        if doc["metadata"]["name"] == "pihole-dns-config":
            yield "pihole-dns-config", doc.get("data", {})


def _local_wildcard_offence(role: str, data: dict) -> str | None:
    lines = [
        line.strip()
        for conf in data.values()
        for line in conf.splitlines()
        if not line.lstrip().startswith("#")
    ]
    if not any(re.fullmatch(r"local=/local\.[^/]+/", line) for line in lines):
        return "no `local=/local.<domain>/`: an AAAA for a .local. name is forwarded upstream"
    null = [line for line in lines if re.fullmatch(r"address=/[^/]+/(::|#)", line)]
    if null:
        return (
            f"{null} answer the NULL address; a client preferring IPv6 dials `::` first"
        )
    # Without the A wildcard, the two checks above pass on a template that dropped the whole
    # block, with `local=` surviving alone as an NXDOMAIN for every .local. name.
    if not any(
        re.fullmatch(r"address=/local\.[^/]+/\d+\.\d+\.\d+\.\d+", line)
        for line in lines
    ):
        return "the .local. A wildcard is gone, so every .local. name resolves nowhere"
    return None


_PIHOLE_WILDCARD = "address=/local.example.com/10.0.0.2\nlocal=/local.example.com/\n"


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
    Property(
        name="authelia-webauthn-is-a-checked-second-factor",
        reason=(
            "`enable_passkey_login` is the first-factor passwordless flow, not a second factor. "
            "Turning it on changes what `one_factor` means for every rule in `access_control`: "
            "a route guarded at one factor becomes reachable with a credential the operator "
            "enrolled as a second one. And Authelia refuses to start on a key it does not "
            "recognise; the pinned version's schema is narrower than the docs site, so "
            "`metadata`, `filtering.prohibit_backup_eligibility` and the experimental passkey "
            "toggles are what a copy-paste brings in. This pod rolls under `Recreate` in front "
            "of most public routes, so that failure lands with the old pod already gone, and "
            "validate/k8s_manifests.py only asks whether the YAML parses."
        ),
        select=_authelia_webauthn,
        offence=_webauthn_offence,
        red=("authelia", {**_GOOD_WEBAUTHN, "enable_passkey_login": True}),
        green=("authelia", _GOOD_WEBAUTHN),
        more_red=(
            ("authelia", {**_GOOD_WEBAUTHN, "disable": True}),
            # Real in the docs, unchecked here: the copy-paste case.
            ("authelia", {**_GOOD_WEBAUTHN, "metadata": {"enabled": True}}),
            (
                "authelia",
                {
                    **_GOOD_WEBAUTHN,
                    "selection_criteria": {
                        "user_verification": "preferred",
                        "discoverability": "preferred",
                    },
                },
            ),
        ),
        must_find=frozenset({"webauthn"}),
    ),
    Property(
        name="pihole-local-wildcard-answers-aaaa-nodata",
        reason=(
            "Since dnsmasq 2.86 an `address=` line carrying only an IPv4 sends every other record "
            "type upstream, so the `.local.` wildcard needs a second directive to keep AAAA "
            "lookups local. `address=/local.<domain>/::` answers `::`, the IPv6 NULL address: "
            "a client preferring IPv6 tries it first, Happy Eyeballs falls back after a stall, "
            "and grpc-go retries `[::]:443` forever. `local=/local.<domain>/` keeps the query "
            "local and answers NODATA, the form dnsmasq's manual names for restoring the "
            "pre-2.86 behaviour. The property is a pair: `local=` present AND the `::` line "
            "gone, since either alone reads as fixed while the other still answers."
        ),
        select=_pihole_dns_config,
        offence=_local_wildcard_offence,
        red=("pihole", {"02-wildcard.conf": "address=/local.example.com/10.0.0.2\n"}),
        green=("pihole", {"02-wildcard.conf": _PIHOLE_WILDCARD}),
        more_red=(
            (
                "pihole",
                {
                    "02-wildcard.conf": _PIHOLE_WILDCARD
                    + "address=/local.example.com/::\n"
                },
            ),
            ("pihole", {"02-wildcard.conf": "local=/local.example.com/\n"}),
        ),
        must_find=frozenset({"pihole-dns-config"}),
    ),
)
