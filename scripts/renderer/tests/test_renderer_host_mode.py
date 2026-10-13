"""`homelab_render.py host` and `package`: C1's layout, the secret refusal, and `${` kept literal.

Each render reads a fixture tree under `tmp_path`, not the repo's `hosts/`, against the repo's
own inventory, unless a test needs an inventory collision the repo lacks.

Run: uv run pytest scripts/renderer/tests/test_renderer_host_mode.py
"""

import stat

import pytest
import yaml

from lib import yaml_fast
from lib.estate import Inventory
from renderer import homelab_render
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.host_manifest import MANIFEST
from renderer.renderer_lib.environment import RenderError

SHELL = 'echo "${FOO:-x}" "${domain}" "$${HOME}" "$(date)"\n'


def _one(src: str, text: str, defaults: dict | None = None) -> tuple[dict, dict, dict]:
    component = {"files": [{"src": src, "dst": "/usr/lib/homelab/x", "mode": "0755"}]}
    if defaults:
        component["defaults"] = defaults
    return {"components": ["beat"]}, {"beat": component}, {src: text}


def _rendered(package, dst="/usr/lib/homelab/x") -> str:
    return package.files[dst][0].decode()


def test_a_host_package_is_written_in_the_c1_layout(hosts, tmp_path):
    root = hosts(*_one("templates/x.sh.j2", "ip={{ server_ip }}\n"))
    out = tmp_path / "out"
    host_mode.write_packages([host_mode.render_host("daniel-pi", root)], out)

    installed = out / "hosts" / "daniel-pi" / "root" / "usr" / "lib" / "homelab" / "x"
    assert installed.read_text() == "ip=10.0.0.139\n"
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755
    spec = yaml_fast.safe_load((out / "hosts" / "daniel-pi" / "nfpm.yaml").read_text())
    assert (spec["name"], spec["arch"]) == ("homelab-daniel-pi", "all")
    assert spec["depends"] == ["python3 (>= 3.12)"]
    # The builder supplies the version, so two commits with one tree render the same spec.
    assert spec["version"] == "${HOMELAB_VERSION}"
    assert spec["contents"] == [
        {
            "src": "root/usr/lib/homelab/manifest.json",
            "dst": "/usr/lib/homelab/manifest.json",
            "file_info": {"mode": 0o644},
        },
        {
            "src": "root/usr/lib/homelab/x",
            "dst": "/usr/lib/homelab/x",
            "file_info": {"mode": 0o755},
        },
    ]


def test_a_shared_package_is_written_in_the_c1_layout(hosts, tmp_path):
    root = hosts(
        {"files": [{"src": "units/u.timer", "dst": "/usr/lib/systemd/system/u.timer"}]},
        where="packages/homelab-updater",
    )
    (root / "packages" / "homelab-updater" / "units").mkdir()
    (root / "packages" / "homelab-updater" / "units" / "u.timer").write_text(
        "[Timer]\n"
    )
    out = tmp_path / "out"
    host_mode.write_packages([host_mode.render_package("homelab-updater", root)], out)

    base = out / "packages" / "homelab-updater"
    assert (base / "root/usr/lib/systemd/system/u.timer").read_text() == "[Timer]\n"
    spec = yaml_fast.safe_load((base / "nfpm.yaml").read_text())
    assert (spec["name"], spec["depends"]) == ("homelab-updater", [])


def test_a_component_default_sits_beneath_host_vars(hosts):
    # daniel-pi's host_vars set server_ip. As an override the default would win, which is
    # how a component default would beat the recovery cron's disarm (*Component variables*).
    root = hosts(
        *_one(
            "templates/x.j2",
            "{{ server_ip }} {{ beat_own }}\n",
            defaults={"server_ip": "default", "beat_own": "on {{ server_ip }}"},
        )
    )
    package = host_mode.render_host("daniel-pi", root)
    assert _rendered(package) == "10.0.0.139 on 10.0.0.139\n"


def test_a_template_reading_a_secret_is_flagged(hosts):
    # The default puts the name in a layer, so only the withholding keeps it out.
    root = hosts(
        *_one("templates/x.j2", "{{ domain }}\n", defaults={"domain": "example.org"})
    )
    with pytest.raises(RenderError, match="domain is a secret key"):
        host_mode.render_host("daniel-pi", root)


def test_a_template_reading_an_inventory_alias_of_a_secret_is_flagged(hosts, tmp_path):
    # The repo's inventory defines no secret name, so this one does, to prove the alias is
    # dropped even when the name it reads has a value in a layer.
    inventory = Inventory(all_vars=tmp_path / "all.yml", host_vars=tmp_path / "hv")
    inventory.host_vars.mkdir()
    inventory.all_vars.write_text(
        yaml.safe_dump({"domain": "example.org", "site": "https://{{ domain }}"})
    )
    root = hosts(*_one("templates/x.j2", "{{ site }}\n"))
    with pytest.raises(RenderError, match=r"site is its inventory value.*'domain'"):
        host_mode.render_host("daniel-pi", root, inventory=inventory)


def test_a_template_reading_no_secret_is_clean(hosts):
    root = hosts(*_one("templates/x.j2", "{{ inventory_hostname }}\n"))
    assert _rendered(host_mode.render_host("daniel-pi", root)) == "daniel-pi\n"


def test_a_dollar_brace_in_a_template_renders_byte_for_byte(hosts):
    # Cluster mode's escape rewrites every one of these runs; host mode has no Flux step.
    root = hosts(*_one("templates/x.sh.j2", SHELL))
    assert _rendered(host_mode.render_host("daniel-pi", root)) == SHELL


def test_a_static_file_is_copied_without_rendering(hosts):
    text = "{{ not_a_variable }} ${domain}\n"
    root = hosts(*_one("files/x.conf", text))
    assert _rendered(host_mode.render_host("daniel-pi", root)) == text


@pytest.mark.parametrize(
    ("component", "message"),
    [
        ({"files": [], "reconcilers": []}, "unknown keys"),
        ({"units": {"homelab-u.timer": {"policy": "never"}}}, "is not a unit name"),
        ({"units": {"u.conf": {"policy": "manual"}}}, "is not a unit name"),
        ({"legacy": [{"cron": "x"}]}, "is not one of"),
        ({"legacy": [{"path": "/etc/../x"}]}, "is malformed"),
        ({"legacy": [{"cron_file": "../x"}]}, "is malformed"),
        (
            {"files": [{"src": "a", "dst": MANIFEST}]},
            "is the rendered manifest",
        ),
        ({"files": [{"src": "a", "dst": "usr/x"}]}, "not a plain absolute path"),
        (
            {"files": [{"src": "a", "dst": "/x"}, {"src": "a", "dst": "/x"}]},
            "two files install /x",
        ),
        # An unquoted 0644 loads as the int 420, which would install as mode 0o420.
        ({"files": [{"src": "a", "dst": "/x", "mode": 0o644}]}, "not a quoted octal"),
        ({"files": [{"src": "a"}]}, "lacks src or dst"),
        ({"files": [{"src": "missing", "dst": "/x"}]}, "does not exist"),
    ],
)
def test_a_malformed_component_is_flagged(hosts, component, message):
    root = hosts({"components": ["beat"]}, {"beat": component}, {"a": ""})
    with pytest.raises(RenderRefused, match=message):
        host_mode.render_host("daniel-pi", root)


def test_a_component_default_reading_inventory_hostname_is_clean(hosts):
    root = hosts(
        *_one(
            "templates/x.j2",
            "{{ tag }}\n",
            defaults={"tag": "{{ inventory_hostname }}-a"},
        )
    )
    assert _rendered(host_mode.render_host("daniel-pi", root)) == "daniel-pi-a\n"


@pytest.mark.parametrize("src", ["outside", "../outside", "link"])
def test_a_static_src_outside_the_component_is_flagged(hosts, tmp_path, src):
    outside = tmp_path / "outside"
    outside.write_text("not the component's\n")
    root = hosts(*_one("files/in", ""))
    beat = root / "components" / "beat"
    (beat / "link").symlink_to(outside)
    entry = str(outside) if src == "outside" else src
    spec = {"files": [{"src": entry, "dst": "/x"}]}
    (beat / "component.yml").write_text(yaml.safe_dump(spec))
    with pytest.raises(RenderRefused, match="is outside the component"):
        host_mode.render_host("daniel-pi", root)


def _link_in_beat(hosts, tmp_path, target, src="files/k.j2", text="x\n"):
    """A `hosts/` tree whose `beat` component lists `src`, a symlink to `target`."""
    root = hosts(*_one("files/in.j2", text))
    link = root / "components" / "beat" / src
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    spec = {"files": [{"src": src, "dst": "/x"}]}
    (root / "components" / "beat" / "component.yml").write_text(yaml.safe_dump(spec))
    return root


def test_a_template_src_symlinked_out_of_the_component_is_flagged(hosts, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("AGE-SECRET-KEY-not-a-real-key\n")
    root = _link_in_beat(hosts, tmp_path, outside)
    with pytest.raises(RenderRefused, match="is outside the component") as caught:
        host_mode.render_host("daniel-pi", root)
    assert "AGE-SECRET-KEY" not in str(caught.value)


def test_a_template_src_symlinked_within_the_component_is_clean(hosts, tmp_path):
    root = hosts(*_one("files/real.j2", "{{ 1 + 1 }}\n"))
    inside = root / "components" / "beat" / "files" / "real.j2"
    _link_in_beat(hosts, tmp_path, inside)
    assert _rendered(host_mode.render_host("daniel-pi", root), "/x") == "2\n"


def test_an_include_symlinked_out_of_the_component_is_flagged(hosts, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("AGE-SECRET-KEY-not-a-real-key\n")
    root = hosts(*_one("files/main.j2", "{% include 'files/part.j2' %}\n"))
    (root / "components" / "beat" / "files" / "part.j2").symlink_to(outside)
    with pytest.raises(RenderRefused, match="is outside the component") as caught:
        host_mode.render_host("daniel-pi", root)
    assert "AGE-SECRET-KEY" not in str(caught.value)


def test_an_include_inside_the_component_is_clean(hosts):
    root = hosts(*_one("files/main.j2", "a {% include 'files/part.j2' %}\n"))
    (root / "components" / "beat" / "files" / "part.j2").write_text("b")
    assert _rendered(host_mode.render_host("daniel-pi", root)) == "a b\n"


@pytest.mark.parametrize("mode", ["4755", "2755", "1777", "07777"])
def test_a_mode_with_a_special_bit_is_flagged(hosts, mode):
    component = {"files": [{"src": "a", "dst": "/x", "mode": mode}]}
    root = hosts({"components": ["beat"]}, {"beat": component}, {"a": ""})
    with pytest.raises(RenderRefused, match="bits above 0o777"):
        host_mode.render_host("daniel-pi", root)


@pytest.mark.parametrize("mode", ["0644", "0755"])
def test_a_mode_with_no_special_bit_is_clean(hosts, mode):
    component = {"files": [{"src": "a", "dst": "/x", "mode": mode}]}
    root = hosts({"components": ["beat"]}, {"beat": component}, {"a": ""})
    package = host_mode.render_host("daniel-pi", root)
    assert package.files["/x"][1] == int(mode, 8)


@pytest.mark.parametrize("name", ["../x", "/etc", "a/b"])
def test_a_component_name_that_is_not_plain_is_flagged(hosts, name):
    root = hosts({"components": [name]})
    with pytest.raises(RenderRefused, match="is not a plain name"):
        host_mode.render_host("daniel-pi", root)


@pytest.mark.parametrize("name", ["../x", "/etc", "a/b"])
def test_a_package_or_host_name_that_is_not_plain_is_flagged(hosts, name):
    root = hosts({})
    with pytest.raises(RenderRefused, match="is not a plain name"):
        host_mode.render_package(name, root)
    with pytest.raises(RenderRefused, match="is not a plain name"):
        host_mode.render_host(name, root)


def test_a_plain_component_and_package_name_is_clean(hosts):
    root = hosts({"components": ["homelab-updater"]}, {"homelab-updater": {}})
    assert set(host_mode.render_host("daniel-pi", root).files) == {MANIFEST}
    spec = root / "packages" / "homelab-updater" / "package.yml"
    spec.parent.mkdir(parents=True)
    spec.write_text("description: x\n")
    assert host_mode.render_package("homelab-updater", root).name == "homelab-updater"


def test_a_host_without_a_package_is_flagged_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "out"
    argv = ["host", "--host", "no-such-host", "--out", str(out)]
    assert homelab_render.main(argv) == 2
    assert "hosts/no-such-host/package.yml does not exist" in capsys.readouterr().err
    assert not out.exists()


def test_a_package_directory_left_by_an_earlier_render_is_flagged(hosts, tmp_path):
    root = hosts(*_one("files/x", ""))
    out = tmp_path / "out"
    host_mode.write_packages([host_mode.render_host("daniel-pi", root)], out)
    with pytest.raises(RenderRefused, match="render into an empty --out"):
        host_mode.write_packages([host_mode.render_host("daniel-pi", root)], out)


def _lib(root, name="homelab-x", text="#!/bin/sh\necho ${HOME}\n"):
    """Write `hosts/lib/<name>` beside a fixture tree, and return its text."""
    (root / "lib").mkdir(exist_ok=True)
    (root / "lib" / name).write_text(text)
    return text


def test_a_lib_script_is_copied_byte_for_byte(hosts):
    root = hosts({"files": [{"lib": "homelab-x", "dst": "/x", "mode": "0755"}]})
    text = _lib(root)
    assert host_mode.render_host("daniel-pi", root).files["/x"] == (
        text.encode(),
        0o755,
    )


@pytest.mark.parametrize("name", ["../homelab-x", "sub/homelab-x", "homelab-x.j2"])
def test_a_lib_name_that_is_not_plain_is_flagged(hosts, name):
    root = hosts({"files": [{"lib": name, "dst": "/x"}]})
    _lib(root)
    with pytest.raises(RenderRefused, match="is not a plain name"):
        host_mode.render_host("daniel-pi", root)


def test_a_lib_script_symlinked_out_of_lib_is_flagged(hosts, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("AGE-SECRET-KEY-not-a-real-key\n")
    root = hosts({"files": [{"lib": "homelab-x", "dst": "/x"}]})
    (root / "lib").mkdir()
    (root / "lib" / "homelab-x").symlink_to(outside)
    with pytest.raises(RenderRefused, match="is outside the component") as caught:
        host_mode.render_host("daniel-pi", root)
    assert "AGE-SECRET-KEY" not in str(caught.value)


def test_an_entry_naming_both_src_and_lib_is_flagged(hosts):
    root = hosts({"files": [{"lib": "homelab-x", "src": "a", "dst": "/x"}]})
    _lib(root)
    with pytest.raises(RenderRefused, match="names both src and lib"):
        host_mode.render_host("daniel-pi", root)


def _maintainer(root, name, text):
    directory = root / "lib" / "maintainer-scripts"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.j2").write_text(text)


def test_a_maintainer_script_is_written_outside_root_and_named_in_the_spec(
    hosts, tmp_path
):
    units = [
        {"src": "u", "dst": "/usr/lib/systemd/system/homelab-u.timer"},
        {"src": "u", "dst": "/usr/lib/systemd/system/homelab-u.service"},
        {
            "src": "u",
            "dst": "/usr/lib/systemd/system/timers.target.wants/homelab-u.timer",
        },
        {"src": "u", "dst": "/usr/lib/systemd/system/k3s.service.d/50-homelab.conf"},
    ]
    root = hosts(
        {"maintainer_scripts": ["prerm"], "files": units},
        where="packages/homelab-updater",
    )
    (root / "packages" / "homelab-updater" / "u").write_text("")
    _maintainer(root, "prerm", "#!/bin/sh\n{{ package_units | join(' ') }}\n")
    out = tmp_path / "out"

    host_mode.write_packages([host_mode.render_package("homelab-updater", root)], out)

    base = out / "packages" / "homelab-updater"
    script = base / "scripts" / "prerm"
    # Only the package's own units: no enable link, no drop-in.
    assert script.read_text() == "#!/bin/sh\nhomelab-u.service homelab-u.timer\n"
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert not (base / "root" / "scripts").exists()
    spec = yaml_fast.safe_load((base / "nfpm.yaml").read_text())
    assert spec["scripts"] == {"preremove": "scripts/prerm"}


@pytest.mark.parametrize(
    ("scripts", "where", "message"),
    [
        (["postrm"], "daniel-pi", "is not one of"),
        (["preinst"], "packages/homelab-updater", "only a host package lists it"),
        (["postinst"], "daniel-pi", "does not exist"),
    ],
)
def test_a_maintainer_script_the_renderer_cannot_ship_is_flagged(
    hosts, scripts, where, message
):
    root = hosts({"maintainer_scripts": scripts}, where=where)
    _maintainer(root, "preinst", "#!/bin/sh\n")
    render = host_mode.render_host if where == "daniel-pi" else host_mode.render_package
    with pytest.raises(RenderRefused, match=message):
        render(where.rsplit("/", 1)[-1], root)
