"""`homelab_render.py cluster`: its refusals, the C1 layout of a stub render, and the mode call.

Run: uv run pytest scripts/renderer/tests/test_renderer_homelab_render.py
"""

import os
import sys
import tempfile

import pytest

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from lib.proc_testing import run
from lib.repo_paths import ALL_VARS, REPO
from renderer import homelab_render, shadow_diff
from renderer.renderer_lib import cluster as cluster_mod


@pytest.fixture
def stub_file(tmp_path):
    path = tmp_path / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return path


@pytest.fixture
def out(tmp_path):
    path = tmp_path / "out"
    path.mkdir()
    return path


def _cluster(out, *args):
    return homelab_render.main(["cluster", "--out", str(out), *args])


def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        homelab_render.main(["--help"])
    assert exc.value.code == 0


def test_cluster_without_secret_stubs_renders_placeholders(out, capsys):
    assert _cluster(out, "--role", "speedtest") == 0
    assert "placeholder mode" in capsys.readouterr().out
    text = (out / "cluster" / "speedtest" / "secret.yaml").read_text()
    # A secret key renders as its placeholder, not its stub.
    assert "APP_KEY: ${speedtest_app_key}\n" in text
    assert "stub-" not in text and "HOMELAB_SECRET" not in text


def test_cluster_renders_littlelink_in_the_c1_layout(out, stub_file):
    assert _cluster(out, "--secret-stubs", str(stub_file), "--role", "littlelink") == 0

    assert sorted(p.name for p in (out / "cluster").iterdir()) == ["littlelink"]
    tag = out / "cluster" / "littlelink"
    files = sorted(p.name for p in tag.iterdir())
    assert files == [
        "deployment.yaml",
        "ingressroute.yaml",
        "kustomization.yaml",
        "service.yaml",
    ]
    kustomization = yaml_fast.safe_load((tag / "kustomization.yaml").read_text())
    assert kustomization["kind"] == "Kustomization"
    assert kustomization["resources"] == [f for f in files if f != "kustomization.yaml"]

    text = (tag / "deployment.yaml").read_text()
    assert "STUB" not in text and "{{" not in text
    # `domain` is published, so the stub file holds no value for it and the route renders
    # its inventory value.
    domain = yaml_fast.safe_load(ALL_VARS.read_text())["domain"]
    assert f"value: https://www.{domain}\n" in text


def test_a_stub_file_missing_a_key_is_flagged(out, stub_file, tmp_path, capsys):
    stubs = yaml_fast.safe_load(stub_file.read_text())
    del stubs["email"]
    short = tmp_path / "short.yml"
    short.write_text(shadow_diff.yaml.safe_dump(stubs))
    assert _cluster(out, "--secret-stubs", str(short)) == 2
    assert "['email']" in capsys.readouterr().err
    assert list(out.iterdir()) == []


def test_a_tag_directory_left_by_an_earlier_render_is_flagged(out, stub_file, capsys):
    assert _cluster(out, "--secret-stubs", str(stub_file), "--role", "littlelink") == 0
    assert _cluster(out, "--secret-stubs", str(stub_file), "--role", "littlelink") == 2
    assert "render into an empty --out" in capsys.readouterr().err


def test_the_cli_writes_nothing_under_the_home_directory(out, tmp_path):
    # ansible-core creates its local temp directory when the render imports it, and its
    # default is `~/.ansible/tmp`. The variable is dropped so the CLI's own choice is tested.
    home = tmp_path / "home"
    home.mkdir()
    # Proves the glob sees the directory, so an empty result cannot pass vacuously.
    (home / "sentinel").write_text("")
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_LOCAL_TEMP"}
    script = REPO / "scripts/renderer/homelab_render.py"
    argv = [sys.executable, str(script), "cluster", "--out", str(out)]
    done = run(
        [*argv, "--role", "littlelink"], cwd=REPO, env={**env, "HOME": str(home)}
    )
    assert done.returncode == 0, done.stderr
    written = sorted(p.relative_to(home).as_posix() for p in home.rglob("*"))
    assert len(written) == 1, written
    assert written == ["sentinel"]


def test_ansible_local_temp_is_set_only_inside_the_block(monkeypatch):
    monkeypatch.delenv("ANSIBLE_LOCAL_TEMP", raising=False)
    with homelab_render.ansible_local_temp():
        assert os.environ["ANSIBLE_LOCAL_TEMP"] == tempfile.gettempdir()
    assert "ANSIBLE_LOCAL_TEMP" not in os.environ


def test_ansible_local_temp_keeps_a_value_the_caller_set(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIBLE_LOCAL_TEMP", str(tmp_path))
    with homelab_render.ansible_local_temp():
        assert os.environ["ANSIBLE_LOCAL_TEMP"] == str(tmp_path)
    assert os.environ["ANSIBLE_LOCAL_TEMP"] == str(tmp_path)


def test_main_leaves_no_ansible_local_temp_in_its_caller(tmp_path, monkeypatch):
    # An in-process call must not leave the variable in the pytest worker (#5002). The
    # missing host refuses before any render, which keeps this test cheap.
    monkeypatch.delenv("ANSIBLE_LOCAL_TEMP", raising=False)
    argv = ["host", "--host", "no-such-host", "--out", str(tmp_path / "out")]
    assert homelab_render.main(argv) == 2
    assert "ANSIBLE_LOCAL_TEMP" not in os.environ


def _host_vars(tmp_path, entries):
    path = tmp_path / "host_vars"
    path.mkdir()
    (path / "daniel-box.yml").write_text("containers_list:\n" + entries)
    return path


def test_a_flux_owned_tag_with_role_is_clean(out, tmp_path):
    """`render_targets()` stays the full universe, so `cluster --role` still renders a tag
    Flux applies (D2.4b), where the hourly record list leaves it out."""
    host_vars = _host_vars(
        tmp_path, "  - name: littlelink\n    platform: k8s\n    owner: flux\n"
    )
    assert cluster_mod.select_tags(["littlelink"], host_vars=host_vars) == [
        "littlelink"
    ]
    flux_owned = cluster_mod.flux_owned_tags(host_vars=host_vars)
    assert flux_owned == {"littlelink"}
    # A fixture cluster file, so the bundle copies fake ciphertext rather than the tree's.
    dotenv = tmp_path / "cluster-vars"
    dotenv.write_text("x=ENC[AES256_GCM,data:eA==,type:str]\n")
    code = homelab_render.cluster(
        out,
        ["littlelink"],
        None,
        flux_owned=lambda: flux_owned,
        cluster_file=dotenv,
    )
    assert code == RENDER_OK
    assert (out / "cluster" / "littlelink" / "deployment.yaml").is_file()


def test_a_tag_the_host_does_not_declare_with_role_is_flagged(tmp_path):
    host_vars = _host_vars(tmp_path, "  - name: sonarr\n    platform: k8s\n")
    with pytest.raises(cluster_mod.RenderRefused, match="not k8s render targets"):
        cluster_mod.select_tags(["littlelink"], host_vars=host_vars)
