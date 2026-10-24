"""The `libvirt:`, `users:` and `lines:` keys: what the renderer accepts, and what reaches reconcile.

The shape matches `test_renderer_host_arms.py`, which holds the `activate:` and `ufw:` cases.
Reconcile's own parsers judge each manifest section, so the renderer cannot accept a value the
arm would refuse.

Run: uv run pytest scripts/renderer/tests/test_renderer_host_arms_state.py
"""

import json

import pytest
from test_renderer_host_arms import _manifest, _reconcile, _render

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.cluster import RenderRefused

UUID = "6f1c8e2a-0b1d-4c3e-8a5f-1234567890ab"
NET = f"<network><name>staging</name><uuid>{UUID}</uuid><bridge name='{{{{ bridge }}}}'/></network>\n"
CLAUDE = {
    "name": "claude",
    "uid": 1001,
    "home": "/home/claude",
    "shell": "/bin/bash",
    "comment": "Claude agent",
}
SYSCTL = {
    "path": "/etc/sysctl.conf",
    "line": "vm.swappiness=10",
    "regexp": r"^\s*vm\.swappiness\s*=",
}
ZSHENV = {
    "path": "/etc/zsh/zshenv",
    "block": "export HOMELAB=1",
    "marker": "# {mark} MANAGED",
}


def libvirt(xml: str = NET, **entry) -> tuple[dict, dict]:
    spec = {"networks": [{"name": "staging", "src": "net.xml.j2"} | entry]}
    return {"libvirt": spec}, {"net.xml.j2": xml}


# libvirt


def _render_xml(hosts, component: dict, sources: dict):
    root = hosts({"components": ["c"]}, {"c": component}, sources)
    return host_mode.render_host("daniel-pi", root)


def test_a_network_renders_with_the_host_context_and_reconcile_accepts_the_section(
    hosts,
):
    component, sources = libvirt()
    root = hosts(
        {"components": ["c"]},
        {"c": {"defaults": {"bridge": "virbr-stage"}} | component},
        sources,
    )

    package = host_mode.render_host("daniel-pi", root)
    manifest = _manifest(package)

    path = "/usr/lib/homelab/libvirt/networks/staging.xml"
    assert b"virbr-stage" in package.files[path][0]
    assert manifest["libvirt"] == {
        "networks": [{"name": "staging", "path": path}],
        "nwfilters": [],
    }
    assert manifest["reconcilers"] == ["libvirt"]
    parsed = _reconcile().parse_manifest(json.dumps(manifest))
    assert parsed["arms"]["libvirt"] == manifest["libvirt"]


@pytest.mark.parametrize(
    "xml",
    [
        pytest.param("<network><name>staging</name></network>", id="no-uuid"),
        pytest.param(
            "<network><name>staging</name><uuid>not-a-uuid</uuid></network>",
            id="bad-uuid",
        ),
        pytest.param(
            f"<network><name>other</name><uuid>{UUID}</uuid></network>", id="name"
        ),
        pytest.param(
            f"<filter><name>staging</name><uuid>{UUID}</uuid></filter>", id="root"
        ),
        pytest.param("<network><name>staging</name>", id="not-xml"),
    ],
)
def test_xml_that_pins_no_uuid_or_misnames_the_object_fails_the_render(hosts, xml):
    component, sources = libvirt(xml)

    with pytest.raises(RenderRefused, match="libvirt"):
        _render_xml(hosts, component, sources)


def test_a_libvirt_entry_with_a_path_in_its_name_fails_the_render(hosts):
    component, sources = libvirt(name="../x")

    with pytest.raises(RenderRefused, match="plain name"):
        _render_xml(hosts, component, sources)


# users


def test_users_reach_the_manifest_and_a_sysusers_file_and_reconcile_accepts_them(hosts):
    other = {**CLAUDE, "name": "bot", "uid": 1002, "comment": ""}

    package = _render(hosts, {"users": [other, CLAUDE]})
    manifest = _manifest(package)

    assert package.files["/usr/lib/sysusers.d/homelab.conf"][0].decode() == (
        'u claude 1001 "Claude agent" /home/claude /bin/bash\n'
        'u bot 1002 "-" /home/claude /bin/bash\n'
    )
    assert [u["name"] for u in manifest["users"]["users"]] == ["claude", "bot"]
    assert manifest["reconcilers"] == ["users"]
    assert _reconcile().users.valid(manifest["users"])


@pytest.mark.parametrize(
    "users",
    [
        pytest.param([{**CLAUDE, "uid": 0}], id="root-uid"),
        pytest.param([{**CLAUDE, "uid": "1001"}], id="string-uid"),
        pytest.param([{**CLAUDE, "name": "Claude"}], id="name"),
        pytest.param([{**CLAUDE, "home": "home/claude"}], id="relative-home"),
        pytest.param([{**CLAUDE, "shell": "/bin/ba sh"}], id="space-in-shell"),
        pytest.param([{**CLAUDE, "comment": 'a"b'}], id="quote"),
        pytest.param([{**CLAUDE, "extra": 1}], id="unknown-key"),
        pytest.param([CLAUDE, {**CLAUDE, "name": "other"}], id="duplicate-uid"),
        pytest.param([CLAUDE, {**CLAUDE, "uid": 1002}], id="duplicate-name"),
    ],
)
def test_a_user_the_arm_would_refuse_fails_the_render(hosts, users):
    with pytest.raises(RenderRefused):
        _render(hosts, {"users": users})


# lines


def test_lines_render_with_the_host_context_and_their_activation_joins_the_manifest(
    hosts,
):
    edit = {**SYSCTL, "line": "vm.swappiness={{ swappiness }}", "activate": "sysctl"}
    root = hosts(
        {"components": ["c"]},
        {"c": {"defaults": {"swappiness": 10}, "lines": [edit, ZSHENV]}},
    )

    manifest = _manifest(host_mode.render_host("daniel-pi", root))

    assert manifest["lines"]["edits"] == [SYSCTL, ZSHENV]
    assert manifest["activations"] == {"/etc/sysctl.conf": "sysctl"}
    assert manifest["reconcilers"] == ["lines"]
    reconcile = _reconcile()
    assert reconcile.lines.valid(manifest["lines"])
    assert reconcile.parse_manifest(json.dumps(manifest))["activations"]


@pytest.mark.parametrize(
    "edits",
    [
        pytest.param(
            [{**SYSCTL, "regexp": "nothing-matches"}], id="line-misses-its-regexp"
        ),
        pytest.param([{**SYSCTL, "regexp": "("}], id="regexp-does-not-compile"),
        pytest.param([{**SYSCTL, "mode": "9"}], id="mode"),
        pytest.param([{**SYSCTL, "path": "etc/sysctl.conf"}], id="relative-path"),
        pytest.param([{**SYSCTL, "path": "/etc/../sysctl.conf"}], id="climbing-path"),
        pytest.param([{**SYSCTL, "line": "a=1\nb=2", "regexp": "a="}], id="two-lines"),
        pytest.param([{**ZSHENV, "marker": "# MANAGED"}], id="marker-without-mark"),
        pytest.param([{**SYSCTL, "block": "x"}], id="line-and-block"),
        pytest.param([{**SYSCTL, "extra": 1}], id="unknown-key"),
        pytest.param([{**SYSCTL, "activate": "restart:x.service"}], id="action"),
        pytest.param(
            [
                {**SYSCTL, "activate": "sysctl"},
                {**ZSHENV, "path": SYSCTL["path"], "activate": "reboot"},
            ],
            id="two-actions-for-one-path",
        ),
    ],
)
def test_an_edit_the_arm_would_refuse_fails_the_render(hosts, edits):
    with pytest.raises(RenderRefused):
        _render(hosts, {"lines": edits})


def test_two_edits_of_one_path_may_name_one_action_and_a_third_may_name_none(hosts):
    other = {**SYSCTL, "line": "vm.dirty_ratio=5", "regexp": "^vm.dirty_ratio"}

    manifest = _manifest(
        _render(
            hosts,
            {
                "lines": [
                    {**SYSCTL, "activate": "sysctl"},
                    {**other, "activate": "sysctl"},
                    other | {"line": "vm.dirty_ratio=6"},
                ]
            },
        )
    )

    assert manifest["activations"] == {"/etc/sysctl.conf": "sysctl"}


# Where the keys may appear


PLAIN_NET = f"<network><name>staging</name><uuid>{UUID}</uuid></network>\n"


@pytest.mark.parametrize(
    "component",
    [
        pytest.param(libvirt(PLAIN_NET), id="libvirt"),
        pytest.param(({"users": [CLAUDE]}, {}), id="users"),
        pytest.param(({"lines": [SYSCTL]}, {}), id="lines"),
    ],
)
def test_a_shared_package_declares_no_arm_state(hosts, component):
    spec, sources = component
    root = hosts(
        {"components": ["c"]}, {"c": spec}, sources, where="packages/homelab-updater"
    )

    with pytest.raises(RenderRefused, match="declares no ufw, libvirt, users or lines"):
        host_mode.render_package("homelab-updater", root)


def test_every_host_package_in_the_tree_renders_a_manifest_reconcile_accepts():
    hosts_root = REPO / "hosts"
    names = sorted(spec.parent.name for spec in hosts_root.glob("*/package.yml"))
    assert len(names) >= 2 and "daniel-pi" in names

    for name in names:
        manifest = _manifest(host_mode.render_host(name))
        _reconcile().parse_manifest(json.dumps(manifest))
