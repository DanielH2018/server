"""The `activate:` field and the `ufw:` key: what the renderer accepts, and what reaches the manifest.

Each render reads a fixture tree under `tmp_path`, written by `conftest.py`'s `hosts`. Reconcile's
own parsers judge the manifest, so the renderer cannot accept a value reconcile would refuse.

Run: uv run pytest scripts/renderer/tests/test_renderer_host_arms.py
"""

import importlib.machinery
import json
import types

import pytest

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.host_manifest import MANIFEST

LIB = REPO / "hosts" / "lib"
SSHD = "/etc/ssh/sshd_config.d/50-homelab.conf"


def _reconcile():
    """`hosts/lib/homelab-reconcile` as a module, so its own parsers judge the manifest."""
    loader = importlib.machinery.SourceFileLoader(
        "homelab_reconcile", str(LIB / "homelab-reconcile")
    )
    module = types.ModuleType(loader.name)
    loader.exec_module(module)
    return module


def _render(hosts, component: dict, **kwargs):
    root = hosts({"components": ["c"]}, {"c": component}, {"u": "x\n"})
    return host_mode.render_host("daniel-pi", root, **kwargs)


def _manifest(package) -> dict:
    return json.loads(package.files[MANIFEST][0])


def _entry(action) -> dict:
    return {"files": [{"src": "u", "dst": SSHD, "activate": action}]}


def test_an_activate_field_reaches_the_manifest_and_reconcile_accepts_it(hosts):
    package = _render(hosts, _entry("reload:ssh.service"))

    manifest = _manifest(package)

    assert manifest["activations"] == {SSHD: "reload:ssh.service"}
    assert _reconcile().parse_manifest(json.dumps(manifest))["activations"] == {
        SSHD: "reload:ssh.service"
    }


def test_a_manifest_with_no_activation_and_no_rule_carries_neither_key(hosts):
    manifest = _manifest(_render(hosts, {"files": [{"src": "u", "dst": SSHD}]}))

    assert "activations" not in manifest
    assert "ufw" not in manifest
    assert manifest["reconcilers"] == []


ACCEPTED = [
    "none",
    "sysctl",
    "modules-load",
    "reboot",
    "reload:ssh.service",
    "try-restart:fail2ban.service",
    "manual:docker.service",
]
REFUSED = [
    "restart:ssh.service",
    "reload:",
    "reload:ssh",
    "reload:../x.service",
    "manual:a b.service",
    "reboot:now",
    "",
    "udev",
]


@pytest.mark.parametrize("action", ACCEPTED)
def test_every_action_the_renderer_accepts_reconcile_accepts(hosts, action):
    manifest = _manifest(_render(hosts, _entry(action)))

    assert _reconcile().parse_manifest(json.dumps(manifest))["activations"] == {
        SSHD: action
    }


@pytest.mark.parametrize("action", REFUSED)
def test_an_action_reconcile_would_refuse_fails_the_render(hosts, action):
    with pytest.raises(RenderRefused, match="activate"):
        _render(hosts, _entry(action))
    assert not _reconcile().activate.valid_activations({SSHD: action})


def test_a_shared_package_s_activations_join_the_host_manifest(hosts):
    root = hosts(
        {"files": [{"src": "u", "dst": "/etc/u.conf", "activate": "sysctl"}]},
        where="packages/homelab-updater",
        sources=None,
    )
    (root / "packages" / "homelab-updater" / "u").write_text("")
    hosts({"files": [{"src": "u", "dst": "/etc/h.conf", "activate": "reboot"}]})
    (root / "daniel-pi" / "u").write_text("")

    manifest = _manifest(host_mode.render_host("daniel-pi", root))

    assert manifest["activations"] == {"/etc/h.conf": "reboot", "/etc/u.conf": "sysctl"}


LAN = "allow from {{ lan_subnet }} to any port 22 proto tcp"
UFW = {
    "ufw": {
        "rules": [
            {
                "id": "lan-ssh",
                "spec": LAN,
                "keep": True,
                "position": "first",
                "before": "ssh-limit",
            },
            {"id": "ssh-limit", "spec": "limit 22/tcp", "keep": True},
        ],
        "legacy": [{"spec": "allow 22/tcp", "replaced_by": "ssh-limit"}],
    }
}


def test_ufw_rules_render_with_the_host_context_and_reconcile_accepts_the_section(
    hosts,
):
    manifest = _manifest(_render(hosts, UFW))

    rules = manifest["ufw"]["rules"]
    assert manifest["reconcilers"] == ["ufw"]
    assert "{{" not in rules[0]["spec"] and rules[0]["spec"].startswith("allow from ")
    assert rules[1] == {
        "id": "ssh-limit",
        "spec": "limit 22/tcp",
        "keep": True,
        "position": "last",
        "before": None,
    }
    assert manifest["ufw"]["legacy"] == [
        {"spec": "allow 22/tcp", "comment": "", "replaced_by": "ssh-limit"}
    ]
    reconcile = _reconcile()
    assert reconcile.ufw.valid(manifest["ufw"])
    assert reconcile.parse_manifest(json.dumps(manifest))["ufw"] == manifest["ufw"]


def _with(**changes) -> dict:
    rule = {"id": "r", "spec": "allow 80/tcp"} | changes
    return {"ufw": {"rules": [rule], "legacy": []}}


@pytest.mark.parametrize(
    "component",
    [
        pytest.param(_with(spec="allow 80/tcp; reboot"), id="shell-metacharacter"),
        pytest.param(_with(spec="allow 80/tcp comment 'x'"), id="quote"),
        pytest.param(_with(spec="flush"), id="not-a-rule-verb"),
        pytest.param(_with(id="Bad_Id"), id="id"),
        pytest.param(_with(position="middle"), id="position"),
        pytest.param(_with(keep="yes"), id="keep"),
        pytest.param(_with(before="ghost"), id="before-unknown-rule"),
        pytest.param(_with(extra=1), id="unknown-key"),
        pytest.param(
            {
                "ufw": {
                    "rules": [],
                    "legacy": [{"spec": "allow 1/tcp", "replaced_by": "ghost"}],
                }
            },
            id="replaced-by-unknown-rule",
        ),
        pytest.param(
            {"ufw": {"rules": [{"id": "a", "spec": "allow 1/tcp"}] * 2}},
            id="duplicate-id",
        ),
    ],
)
def test_a_ufw_value_the_arm_would_refuse_fails_the_render(hosts, component):
    with pytest.raises(RenderRefused):
        _render(hosts, component)


def test_a_shared_package_declares_no_ufw_rule(hosts):
    root = hosts(
        {"components": ["c"]}, {"c": _with()}, where="packages/homelab-updater"
    )

    with pytest.raises(RenderRefused, match="ufw"):
        host_mode.render_package("homelab-updater", root)


def test_a_lib_entry_may_name_a_package_module_and_nothing_that_climbs(hosts):
    ok = hosts({"files": [{"lib": "reconcile_lib/ufw.py", "dst": "/x/ufw.py"}]})
    (ok / "lib" / "reconcile_lib").mkdir(parents=True)
    (ok / "lib" / "reconcile_lib" / "ufw.py").write_text("x = 1\n")
    assert host_mode.render_host("daniel-pi", ok).files["/x/ufw.py"][0] == b"x = 1\n"

    for name in (
        "reconcile_lib/../homelab-reconcile",
        "../homelab-reconcile",
        "a/b/c.py",
    ):
        bad = hosts({"files": [{"lib": name, "dst": "/x"}]})
        with pytest.raises(RenderRefused):
            host_mode.render_host("daniel-pi", bad)
