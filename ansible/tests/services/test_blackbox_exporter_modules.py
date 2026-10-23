#!/usr/bin/env python3
"""Every blackbox_exporter http module sends `Accept: */*`.

Authelia answers an SSO-gated name with 302 only when the request's Accept header admits it,
and with 401 when the request carries none. blackbox_exporter sends no Accept unless a module
sets one, and Go's net/http adds none. Kuma's ingress tiles draw the 302, so a module that
lost its `headers` would read every one of the 23 SSO targets as down while Kuma read them up
(docs/target-architecture/monitoring/m4-blackbox-ingress.md, *The exporter*).

The check is a `..._is_clean` / `..._is_flagged` pair over the same reader, and the real file
must yield the two modules the ingress probes use, so a reader that stopped finding modules
fails rather than passing over nothing.

Run: uv run pytest ansible/tests/services/test_blackbox_exporter_modules.py
"""

from _helpers import ROLES
from lib import yaml_fast

CONFIG = ROLES / "k8s" / "blackbox-exporter" / "files" / "blackbox.yml"

# The http modules the probe jobs name: the ingress pair, and batch B8's LiveSync and keyword modules.
REQUIRED_MODULES = frozenset(
    {"http_2xx", "http_sso_302", "http_401", "http_body_OK", "http_body_ok"}
)


def http_modules(config: dict) -> dict[str, dict]:
    """The http-prober modules of a parsed blackbox.yml, by name."""
    return {
        name: module
        for name, module in (config.get("modules") or {}).items()
        if module.get("prober") == "http"
    }


def modules_without_accept(config: dict) -> list[str]:
    """Return the http modules that do not send `Accept: */*`."""
    return sorted(
        name
        for name, module in http_modules(config).items()
        if ((module.get("http") or {}).get("headers") or {}).get("Accept") != "*/*"
    )


def _config() -> dict:
    return yaml_fast.safe_load(CONFIG.read_text())


def test_the_real_config_declares_the_ingress_modules():
    missing = REQUIRED_MODULES - http_modules(_config()).keys()
    assert not missing, f"blackbox.yml lost the ingress modules {sorted(missing)}"


def test_the_real_config_is_clean():
    assert modules_without_accept(_config()) == []


def test_a_module_without_the_header_is_flagged():
    config = _config()
    del config["modules"]["http_sso_302"]["http"]["headers"]
    assert modules_without_accept(config) == ["http_sso_302"]


def test_http_2xx_prefers_ipv4_because_the_cluster_has_no_ipv6_route():
    module = http_modules(_config())["http_2xx"]["http"]
    assert module["preferred_ip_protocol"] == "ip4"
