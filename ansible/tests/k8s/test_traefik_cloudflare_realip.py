"""The https entrypoint attributes a Cloudflare request to CF-Connecting-IP before anything reads it.

Cloudflare APPENDS the client address to any X-Forwarded-For the client sent, and the
entrypoint's `forwardedHeaders.trustedIPs` keeps that chain from a Cloudflare source, so the
LEFTMOST entry is a value the client chose. Authelia logs the leftmost entry as `remote_ip`,
and CrowdSec bans on it. The `cloudflare-realip` Middleware replaces XFF with CF-Connecting-IP
for a Cloudflare source; it only helps if it runs BEFORE crowdsec and every router's
forwardAuth, which is the entrypoint chain's first slot.

The plugin is local (files/cloudflare-realip/, projected from a ConfigMap), and four names
have to agree for Traefik to load it. A mismatch renders and applies cleanly, passes
`--dry-run`, and at startup disables EVERY plugin — the crowdsec bouncer included, which is
the #1322 whole-edge 404. The last test pins that agreement.

Checked on the RENDERED templates, the same way as the sibling
ansible/tests/services/test_traefik_http_entrypoint_crowdsec.py.
"""

from pathlib import Path

import pytest

from lib import yaml_fast
from validate.k8s_manifests import K8S_ROLES

from _k8s_render import host_context, render_role_template

_ROLE = "traefik"
_HOST = "daniel-box"


def _render(template: str) -> str:
    return render_role_template(_ROLE, template, host=_HOST)


def _static_config() -> dict:
    doc = yaml_fast.safe_load(_render("static-config.yaml.j2"))
    return yaml_fast.safe_load(doc["data"]["traefik.yml"])


def _ref(name: str) -> str:
    return f"{host_context(_HOST)['k8s_namespace']}-{name}@kubernetescrd"


def realip_chain_gaps(
    chain: list[str], realip_ref: str, crowdsec_ref: str
) -> list[str]:
    """Why the https chain fails to run the real-IP rewrite first; empty means it holds."""
    if realip_ref not in chain:
        return [f"{realip_ref} is not on the https entrypoint chain {chain}"]
    if chain.index(realip_ref) != 0:
        return [
            f"{realip_ref} is at position {chain.index(realip_ref)}, not first: whatever runs "
            f"before it ({chain[: chain.index(realip_ref)]}) sees the client-chosen XFF"
        ]
    if crowdsec_ref not in chain:
        return [f"{crowdsec_ref} is not on the https entrypoint chain {chain}"]
    return []


def test_the_https_chain_rewrites_before_crowdsec() -> None:
    """The accepting half, on daniel-box's rendered static config."""
    chain = _static_config()["entryPoints"]["https"]["http"]["middlewares"]
    problems = realip_chain_gaps(chain, _ref("cloudflare-realip"), _ref("crowdsec"))
    assert not problems, problems


@pytest.mark.parametrize(
    "chain",
    [
        pytest.param(["ns-crowdsec@kubernetescrd"], id="rewrite_missing"),
        pytest.param(
            ["ns-crowdsec@kubernetescrd", "ns-cloudflare-realip@kubernetescrd"],
            id="rewrite_after_crowdsec",
        ),
        pytest.param(
            [
                "ns-default-headers@kubernetescrd",
                "ns-cloudflare-realip@kubernetescrd",
                "ns-crowdsec@kubernetescrd",
            ],
            id="rewrite_after_default_headers",
        ),
    ],
)
def test_a_late_or_missing_rewrite_is_flagged(chain: list[str]) -> None:
    """The rejecting half. Every chain here serves traffic normally."""
    realip, crowdsec = "ns-cloudflare-realip@kubernetescrd", "ns-crowdsec@kubernetescrd"
    assert not realip_chain_gaps([realip, crowdsec], realip, crowdsec), (
        "control must be clean"
    )
    assert realip_chain_gaps(chain, realip, crowdsec), f"{chain} was not flagged"


def test_only_cloudflare_is_a_trusted_forwarder() -> None:
    """No entrypoint keeps a client-sent XFF from the LAN (see the DECIDED comment on
    static-config.yaml.j2's trustedIPs), and the Middleware trusts exactly Cloudflare."""
    ctx = host_context(_HOST)
    expected = set(ctx["cloudflare_ips"]) | {"127.0.0.1/32"}
    for name in ("http", "https"):
        trusted = set(
            _static_config()["entryPoints"][name]["forwardedHeaders"]["trustedIPs"]
        )
        assert trusted == expected, f"{name}: {sorted(trusted ^ expected)}"
    middleware = next(
        d
        for d in yaml_fast.safe_load_all(_render("dynamic.yaml.j2"))
        if d
        and d["kind"] == "Middleware"
        and d["metadata"]["name"] == "cloudflare-realip"
    )
    (config,) = middleware["spec"]["plugin"].values()
    assert config["trustedIPs"] == ctx["cloudflare_ips"]


def test_the_local_plugin_names_agree() -> None:
    """localPlugins key == the Middleware's plugin key; moduleName == the projected directory
    and prefixes the manifest's `import`; the ConfigMap carries the files the items name."""
    (local_name, local) = next(
        iter(_static_config()["experimental"]["localPlugins"].items())
    )
    module = local["moduleName"]

    dynamic = list(yaml_fast.safe_load_all(_render("dynamic.yaml.j2")))
    middleware = next(
        d for d in dynamic if d and d["metadata"]["name"] == "cloudflare-realip"
    )
    assert list(middleware["spec"]["plugin"]) == [local_name]

    pod = yaml_fast.safe_load(_render("deployment.yaml.j2"))["spec"]["template"]["spec"]
    volume = next(v for v in pod["volumes"] if v["name"] == "cloudflare-realip-plugin")
    paths = {i["key"]: i["path"] for i in volume["configMap"]["items"]}
    assert set(paths.values()) == {
        f"src/{module}/cloudflarerealip.go",
        f"src/{module}/.traefik.yml",
    }, paths
    mount = next(
        m
        for c in pod["containers"]
        if c["name"] == "traefik"
        for m in c["volumeMounts"]
        if m["name"] == "cloudflare-realip-plugin"
    )
    assert mount["mountPath"] == "/plugins-local"

    configmap = yaml_fast.safe_load(_render("cloudflare-realip-plugin.yaml.j2"))
    assert configmap["metadata"]["name"] == volume["configMap"]["name"]
    assert set(configmap["data"]) == set(paths)

    manifest = yaml_fast.safe_load(configmap["data"]["traefik-plugin.yml"])
    assert manifest["import"].startswith(module), (manifest["import"], module)
    source = Path(
        K8S_ROLES / _ROLE / "files" / "cloudflare-realip" / "cloudflarerealip.go"
    )
    assert f"package {manifest['import'].rsplit('/', 1)[-1]}\n" in source.read_text()
