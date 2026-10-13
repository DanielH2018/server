"""`homelab_render.py host` and `package`: C1's layout, the secret refusal, and `${` kept literal.

`hosts/` holds no package until H3.2a, so each render reads a fixture tree under `tmp_path`
against the repo's own inventory, unless a test needs an inventory collision the repo lacks.

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
from renderer.renderer_lib.environment import RenderError

SHELL = 'echo "${FOO:-x}" "${domain}" "$${HOME}" "$(date)"\n'


@pytest.fixture
def hosts(tmp_path):
    """A writer for a `hosts/` tree: one package spec and its components."""
    root = tmp_path / "hosts"

    def write(
        package: dict,
        components: dict[str, dict] | None = None,
        sources: dict[str, str] | None = None,
        where: str = "daniel-pi",
    ):
        spec = root / where / "package.yml"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(yaml.safe_dump(package))
        for name, component in (components or {}).items():
            directory = root / "components" / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "component.yml").write_text(yaml.safe_dump(component))
            for src, text in (sources or {}).items():
                (directory / src).parent.mkdir(parents=True, exist_ok=True)
                (directory / src).write_text(text)
        return root

    return write


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
            "src": "root/usr/lib/homelab/x",
            "dst": "/usr/lib/homelab/x",
            "file_info": {"mode": 0o755},
        }
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
        ({"files": [], "units": []}, "unknown keys"),
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
