"""Census rows over three tracked config files, each judged property by property.

`test_census_rows_roles.py` runs them. The files are the Alert History board, blackbox.yml and
the jellyfin role's defaults; `_secrets_config_rows.py` holds the secrets-scanning configs. A
row parses its file in its offence. Its red half is a small document, or the real file with
one property broken (`_Edited`).
"""

import json
import re
from collections.abc import Set as AbstractSet

import yaml
from _helpers import REPO, image_tag
from _row_table import Census, Subject
from lib import yaml_fast
from services.jellyfin._jellyfin_plugins import padded, version_tuple


class _Edited(Subject):
    """The real file with one substring replaced, so a red subject follows the file's shape."""

    def __init__(self, rel: str, before: str, after: str):
        super().__init__(rel)
        self._edit = (before, after)

    @property
    def text(self) -> str:
        before, after = self._edit
        original = (REPO / self.rel).read_text()
        if before not in original:
            raise AssertionError(
                f"{self.rel} no longer holds {before!r}: fix the red subject"
            )
        return original.replace(before, after, 1)


def _yaml(subject: Subject):
    return yaml_fast.safe_load(subject.text)


# ── The Alert History board ────────────────────────────────────────────────────────────

BOARD = "k8s/roles/observability/files/dashboards/Infrastructure/alert-history.json"
_PANELS = frozenset(
    {
        "Firing minutes by service (range total)",
        "Firing alerts over time, by service",
        "Alerts that fired in the range",
        "Firing now",
    }
)
_BOARD_TERMS = (
    "ALERTS{",
    'alertstate="firing"',
    'alertname!="Watchdog"',
    'service=~"$service"',
)
_FIRING = 'ALERTS{alertstate="firing",alertname!="Watchdog",service=~"$service"}'
_SERVICE_VARIABLE = {
    "name": "service",
    "definition": "label_values(ALERTS, service)",
    "includeAll": True,
    "allValue": ".*",
}


def _board(
    titles: AbstractSet[str] = _PANELS,
    *,
    datasource="prometheus",
    expr=f"sum by (service) ({_FIRING})",
    variable=_SERVICE_VARIABLE,
) -> Subject:
    panels = [
        {"title": title, "targets": [{"datasource": {"uid": datasource}, "expr": expr}]}
        for title in sorted(titles)
    ]
    return Subject(
        BOARD, json.dumps({"panels": panels, "templating": {"list": [variable]}})
    )


def _panel_titles_differ(subject: Subject) -> list[str]:
    titles = {panel["title"] for panel in json.loads(subject.text)["panels"]}
    return (
        []
        if titles == _PANELS
        else [f"panels are {sorted(titles)}, not the named four"]
    )


def _queries_not_firing_alerts_by_service(subject: Subject) -> list[str]:
    targets = [
        (panel["title"], target)
        for panel in json.loads(subject.text)["panels"]
        for target in panel.get("targets", [])
    ]
    found = [f"{len(targets)} queries for {len(_PANELS)} panels"] * (
        len(targets) < len(_PANELS)
    )
    for title, target in targets:
        if (datasource := target.get("datasource", {})).get("uid") != "prometheus":
            found.append(f"{title}: datasource is {datasource}")
        expr = target.get("expr", "")
        found += [f"{title}: no {term}" for term in _BOARD_TERMS if term not in expr]
        if ("sum by (" in expr or "max by (" in expr) and "by (service" not in expr:
            found.append(f"{title}: aggregates without service first")
    return found


def _service_variable_drift(subject: Subject) -> list[str]:
    [variable] = json.loads(subject.text)["templating"]["list"]
    holds = (
        variable["name"] == "service"
        and variable["definition"] == "label_values(ALERTS, service)"
        and variable.get("includeAll")
        and variable.get("allValue") == ".*"
    )
    return [] if holds else [f"the variable is {variable}"]


_LOKI_QUERY = 'sum by (name) (count_over_time({container="monitor-bridge"} |~ `^DOWN ` [$__range]))'

# ── blackbox.yml ───────────────────────────────────────────────────────────────────────

BLACKBOX = "k8s/roles/blackbox-exporter/files/blackbox.yml"
_INGRESS_MODULES = frozenset(
    {"http_2xx", "http_sso_302", "http_401", "http_body_OK", "http_body_ok"}
)


def _http_modules(subject: Subject) -> dict[str, dict]:
    modules = (_yaml(subject).get("modules") or {}).items()
    return {name: module for name, module in modules if module.get("prober") == "http"}


def _blackbox(
    *, accept: str | None = "*/*", ip: str = "ip4", skip: str = ""
) -> Subject:
    http = {
        "preferred_ip_protocol": ip,
        **({"headers": {"Accept": accept}} if accept else {}),
    }
    modules = {
        name: {"prober": "http", "http": http} for name in _INGRESS_MODULES - {skip}
    }
    return Subject(BLACKBOX, yaml.safe_dump({"modules": modules}))


def _ingress_modules_missing(subject: Subject) -> list[str]:
    return [
        f"no http module {name}"
        for name in sorted(_INGRESS_MODULES - _http_modules(subject).keys())
    ]


def _modules_without_accept(subject: Subject) -> list[str]:
    return [
        f"{name} does not send Accept: */*"
        for name, module in sorted(_http_modules(subject).items())
        if ((module.get("http") or {}).get("headers") or {}).get("Accept") != "*/*"
    ]


def _http_2xx_not_ipv4(subject: Subject) -> list[str]:
    http = _http_modules(subject).get("http_2xx", {}).get("http", {})
    return (
        []
        if http.get("preferred_ip_protocol") == "ip4"
        else [f"http_2xx prefers {http.get('preferred_ip_protocol')}"]
    )


# ── the jellyfin role's defaults ───────────────────────────────────────────────────────

JELLYFIN_DEFAULTS = "k8s/roles/jellyfin/defaults/main.yml"
_ABI_VAR = re.compile(r"^jellyfin_k8s_(?P<plugin>[a-z0-9]+)_target_abi$")
# ani-sync declares no `_target_abi` var: its ABI leads its release asset's filename.
_ABI_PLUGINS = frozenset(
    {"anisync", "introskipper", "webhook", "mergeversions", "mediacleaner"}
)
_ABI_EDIT = (
    'jellyfin_k8s_mediacleaner_target_abi: "12.0.0.0"',
    'jellyfin_k8s_mediacleaner_target_abi: "12.5.0.0"',
)


def _version(text: str) -> tuple[int, ...]:
    """`version_tuple` of `text`, padded to the four parts of a plugin's targetAbi."""
    return padded(version_tuple(text, text), (0, 0, 0, 0))[0]


def _declared_abis(defaults: dict) -> dict[str, tuple[int, ...]]:
    found = {
        match["plugin"]: _version(str(value))
        for name, value in defaults.items()
        if (match := _ABI_VAR.match(name))
    }
    if url := defaults.get("jellyfin_k8s_anisync_url"):
        found["anisync"] = _version(url.rsplit("/", 1)[-1])
    return found


def _abi_census_gaps(subject: Subject) -> list[str]:
    missing = _ABI_PLUGINS - _declared_abis(_yaml(subject)).keys()
    return [f"no targetAbi found for {sorted(missing)}"] * bool(missing)


def _plugins_newer_than_image(subject: Subject) -> list[str]:
    defaults = _yaml(subject)
    image = defaults["jellyfin_k8s_image"]
    server = _version(image_tag(image))
    return [
        f"{plugin} targets {'.'.join(map(str, abi))}, newer than {image}"
        for plugin, abi in _declared_abis(defaults).items()
        if abi > server
    ]


def _abi_floor_holders_differ(subject: Subject) -> list[str]:
    abis = _declared_abis(_yaml(subject))
    floor = max(abis.values())
    holders = sorted(plugin for plugin, abi in abis.items() if abi == floor)
    return (
        []
        if holders == ["anisync", "introskipper"]
        else [f"floor {floor} is held by {holders}"]
    )


CONFIG_FILE_ROWS = (
    Census(
        name="alert-history-board-holds-the-named-panels",
        reason=(
            "The Alert History board's four panels are named rather than counted. A dropped or "
            "retitled panel then fails here, instead of leaving the query row to pass over fewer "
            "panels."
        ),
        files=lambda: [BOARD],
        offence=_panel_titles_differ,
        red=(_board(_PANELS - {"Firing now"}), _board(_PANELS | {"Down cycles"})),
        green=(_board(),),
    ),
    Census(
        name="alert-history-queries-read-firing-alerts-by-service",
        reason=(
            "The board reads Prometheus's `ALERTS`, grouped by `service` (M6 B-5), because M6 "
            "retires Uptime Kuma. A panel left on a Loki query, or one that drops the `$service` "
            "filter, renders without error and answers a different question than its title. So "
            "each query is held to the Prometheus datasource, `ALERTS`, firing state only and the "
            "`$service` variable. `Watchdog` always fires, so the board leaves it out."
        ),
        files=lambda: [BOARD],
        offence=_queries_not_firing_alerts_by_service,
        red=(
            _board(datasource="bf4q19tuivta8e", expr=_LOKI_QUERY),
            _board(
                expr='sum by (service) (ALERTS{alertstate="firing",alertname!="Watchdog"})'
            ),
            _board(expr=f"sum by (name) ({_FIRING})"),
            _board({"Firing now"}),
        ),
        green=(_board(),),
    ),
    Census(
        name="alert-history-service-variable-lists-alerts-service-values",
        reason=(
            "The `$service` variable lists the `service` label values of `ALERTS` and offers "
            "All as `.*`. The query row filters every panel on it, so a variable fed from "
            "another series leaves the filter choosing among names no alert carries."
        ),
        files=lambda: [BOARD],
        offence=_service_variable_drift,
        red=(
            _board(
                variable={
                    **_SERVICE_VARIABLE,
                    "definition": "label_values(up, service)",
                }
            ),
            _board(variable={**_SERVICE_VARIABLE, "includeAll": False}),
        ),
        green=(_board(),),
    ),
    Census(
        name="blackbox-exporter-declares-the-ingress-http-modules",
        reason=(
            "The probe jobs name five http modules: the ingress pair, and batch B8's LiveSync and "
            "keyword modules. The reader of the modules must find them, so a reader that stops "
            "finding modules fails here instead of passing over nothing."
        ),
        files=lambda: [BLACKBOX],
        offence=_ingress_modules_missing,
        red=(_blackbox(skip="http_sso_302"),),
        green=(_blackbox(),),
    ),
    Census(
        name="blackbox-exporter-http-modules-send-accept-any",
        reason=(
            "Authelia answers an SSO-gated name with 302 only when the request's Accept header "
            "admits it, and with 401 when the request carries none. blackbox_exporter sends no "
            "Accept unless a module sets one, and Go's net/http adds none. The ingress probes count "
            "that 302 as up, so a module that lost its `headers` reads every SSO target as down "
            "(ADR-0026)."
        ),
        files=lambda: [BLACKBOX],
        offence=_modules_without_accept,
        red=(_blackbox(accept=None), _blackbox(accept="text/html")),
        green=(_blackbox(),),
    ),
    Census(
        name="blackbox-exporter-http-2xx-prefers-ipv4",
        reason=(
            "The cluster has no IPv6 route, so a module that resolves a dual-stack name to its "
            "AAAA record fails the probe. `http_2xx` prefers `ip4`."
        ),
        files=lambda: [BLACKBOX],
        offence=_http_2xx_not_ipv4,
        red=(_blackbox(ip="ip6"), _blackbox(skip="http_2xx")),
        green=(_blackbox(),),
    ),
    Census(
        name="jellyfin-plugin-abi-census-finds-every-plugin",
        reason=(
            "The targetAbi census globs `jellyfin_k8s_<plugin>_target_abi`, so a renamed var "
            "shrinks the set and every ABI row passes over fewer plugins. The five installed "
            "plugins are named here. ani-sync has no var: its ABI leads the release asset's "
            "filename, which `tests/services/jellyfin/test_anisync_pin_matches_server.py` owns, and the "
            "census reads it from the URL so the `max` is over every plugin. SSO-Auth is gone "
            "(#1674)."
        ),
        files=lambda: [JELLYFIN_DEFAULTS],
        offence=_abi_census_gaps,
        red=(
            _Edited(
                JELLYFIN_DEFAULTS,
                "jellyfin_k8s_mediacleaner_target_abi:",
                "jellyfin_k8s_mediacleaner_abi_target:",
            ),
        ),
        green=(Subject(JELLYFIN_DEFAULTS),),
    ),
    Census(
        name="jellyfin-image-satisfies-every-plugin-target-abi",
        reason=(
            "`jellyfin_k8s_image` must satisfy the targetAbi of every plugin the role installs, "
            "found by globbing the vars so a plugin added without its own guard is covered. "
            "Jellyfin's loader rejects a plugin built for a newer server without logging a "
            "failure. The directory stays on disk, `GET /Plugins` omits it and the rollout stays "
            "green, so nobody notices the plugin is gone. Raise the image, or take a release on "
            "the line the image satisfies."
        ),
        files=lambda: [JELLYFIN_DEFAULTS],
        offence=_plugins_newer_than_image,
        red=(
            _Edited(JELLYFIN_DEFAULTS, *_ABI_EDIT),
            _Edited(JELLYFIN_DEFAULTS, "jellyfin:12.4ubu2604", "jellyfin:12.3ubu2604"),
        ),
        green=(Subject(JELLYFIN_DEFAULTS),),
    ),
    Census(
        name="jellyfin-binding-abi-floor-is-held-by-anisync-and-introskipper",
        reason=(
            "The image cannot move to a new Jellyfin line until every installed plugin has a "
            "release for that ABI, so the binding constraint is the `max` over the declared "
            "targetAbi values. ani-sync and Intro Skipper hold it at 12.4.0.0. The role's "
            "CLAUDE.md states that in its *At a glance* section. When a plugin bump moves the "
            "floor, update that sentence and this row together."
        ),
        files=lambda: [JELLYFIN_DEFAULTS],
        offence=_abi_floor_holders_differ,
        red=(_Edited(JELLYFIN_DEFAULTS, *_ABI_EDIT),),
        green=(Subject(JELLYFIN_DEFAULTS),),
    ),
)
