"""Headlamp's plugins are fetched at pod start by an init container, one tarball per entry of
`headlamp_k8s_plugins`. A tarball that downloads but does not match its recorded sha256 must
fail the pod, not install: the plugin is JavaScript the browser runs as the operator, behind
Authelia, with the dashboard's read-only cluster token.

This guards the shape that makes a bad download fatal — `set -eu`, one `sha256sum -c` per
plugin, a 64-hex digest recorded for each — and proves the check can go red on an entry
whose digest is missing or malformed.
"""

import re

from lib import yaml_fast
from _manifest_guards import K8S, _k8s_entries, _render, _role_context

HEX64 = re.compile(r"^[0-9a-f]{64}$")


# A real release, so the accept half exercises the same shape an operator would add. The image
# bundles this one already (defaults/main.yml explains), so it is never in the committed list.
SAMPLE_PLUGIN = {
    "name": "prometheus",
    "version": "0.9.1",
    "sha256": "06afabe42da69e8e0c5c2632acc3fee020566825d2ae71ff8e4f3dd7f78fc4ae",
}


def _pod_spec(plugins: list[dict]) -> dict:
    doc = yaml_fast.safe_load(
        _render(
            K8S / "headlamp" / "templates" / "deployment.yaml.j2",
            **_role_context(
                "headlamp",
                container_item=next(
                    c for c in _k8s_entries() if c["name"] == "headlamp"
                ),
                headlamp_k8s_plugins=plugins,
            ),
        )
    )
    return doc["spec"]["template"]["spec"]


def _init_script(plugins: list[dict]) -> str:
    inits = _pod_spec(plugins)["initContainers"]
    (init,) = [c for c in inits if c["name"] == "fetch-plugins"]
    return init["command"][-1]


def _install_problems(plugins: list[dict], script: str) -> list[str]:
    """Every way the init script could install a plugin it did not verify."""
    problems = []
    if not script.lstrip().startswith("set -eu"):
        problems.append("script does not start with `set -eu`")
    for plugin in plugins:
        digest = str(plugin.get("sha256", ""))
        if not HEX64.match(digest):
            problems.append(f"{plugin['name']}: sha256 {digest!r} is not 64 hex digits")
        if f'"{digest}  {plugin["name"]}.tgz" | sha256sum -c -' not in script:
            problems.append(
                f"{plugin['name']}: no sha256sum -c against its recorded digest"
            )
    return problems


def test_every_plugin_is_checksummed_before_install():
    plugins = [SAMPLE_PLUGIN]
    assert _install_problems(plugins, _init_script(plugins)) == []


def test_the_checksum_guard_rejects_a_missing_or_malformed_digest():
    script = _init_script([SAMPLE_PLUGIN])
    for bad in (
        {**SAMPLE_PLUGIN, "sha256": ""},
        {**SAMPLE_PLUGIN, "sha256": "deadbeef"},
        {"name": SAMPLE_PLUGIN["name"], "version": SAMPLE_PLUGIN["version"]},
    ):
        assert _install_problems([bad], script), f"accepted: {bad}"
    assert _install_problems([SAMPLE_PLUGIN], script.replace("set -eu", "true"))


def test_an_empty_plugin_list_renders_no_init_container():
    """An init container that downloads nothing would still be a GitHub dependency at every
    pod start."""
    assert "initContainers" not in _pod_spec([])


def test_the_committed_list_holds_no_plugin_the_image_ships():
    """The image bundles prometheus (defaults/main.yml explains); a second copy lists twice
    in GET /plugins."""
    committed = _role_context("headlamp")["headlamp_k8s_plugins"]
    assert SAMPLE_PLUGIN["name"] not in {p["name"] for p in committed}
    assert _install_problems(committed, _init_script(committed)) == []


def test_asset_prefix_names_the_release_asset():
    """flux publishes `headlamp-k8s-flux-<ver>.tar.gz`, not `<name>-<ver>.tar.gz`; a URL
    built without the prefix 404s and the pod never starts."""
    flux = {
        "name": "flux",
        "version": "0.7.0",
        "sha256": "0" * 64,
        "asset_prefix": "headlamp-k8s-",
    }
    base = "https://github.com/headlamp-k8s/plugins/releases/download"
    assert f"{base}/flux-0.7.0/headlamp-k8s-flux-0.7.0.tar.gz" in _init_script([flux])
    assert f"{base}/prometheus-0.9.1/prometheus-0.9.1.tar.gz" in _init_script(
        [SAMPLE_PLUGIN]
    )
