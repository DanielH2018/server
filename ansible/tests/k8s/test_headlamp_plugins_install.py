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
from _manifest_guards import K8S, _k8s_entries, _render, _role_defaults

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _init_script() -> tuple[list[dict], str]:
    defaults = _role_defaults("headlamp")
    doc = yaml_fast.safe_load(
        _render(
            K8S / "headlamp" / "templates" / "deployment.yaml.j2",
            container_item=next(c for c in _k8s_entries() if c["name"] == "headlamp"),
            **defaults,
        )
    )
    spec = doc["spec"]["template"]["spec"]
    (init,) = [c for c in spec["initContainers"] if c["name"] == "fetch-plugins"]
    return defaults["headlamp_k8s_plugins"], init["command"][-1]


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
    plugins, script = _init_script()
    assert plugins, (
        "headlamp_k8s_plugins is empty — the init container would install nothing"
    )
    assert _install_problems(plugins, script) == []


def test_the_checksum_guard_rejects_a_missing_or_malformed_digest():
    plugins, script = _init_script()
    good = plugins[0]
    for bad in (
        {**good, "sha256": ""},
        {**good, "sha256": "deadbeef"},
        {"name": good["name"], "version": good["version"]},
    ):
        assert _install_problems([bad], script), f"accepted: {bad}"
    assert _install_problems(plugins, script.replace("set -eu", "true"))
