"""`homelab_render.py cluster`: its refusals, the C1 layout of a stub render, and the mode call.

Run: uv run pytest scripts/renderer/tests/test_renderer_homelab_render.py
"""

import os
import sys

import pytest

from lib import yaml_fast
from lib.proc_testing import run
from lib.repo_paths import REPO
from renderer import homelab_render, shadow_diff


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
    assert _cluster(out, "--role", "littlelink") == 0
    assert "placeholder mode" in capsys.readouterr().out
    text = (out / "cluster" / "littlelink" / "deployment.yaml").read_text()
    # `domain` is a secret key, so the route renders as its placeholder, not its stub.
    assert "value: https://www.${domain}\n" in text
    assert "stub-" not in text and "HOMELAB_SECRET" not in text


def test_a_placeholder_render_of_a_deferred_tag_alone_is_flagged(out, capsys):
    assert _cluster(out, "--role", "home-assistant") == 2
    assert "PLACEHOLDER_DEFERRED" in capsys.readouterr().err
    assert list(out.iterdir()) == []


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
    # The stub file reached the render: `domain` is a secret key.
    assert "value: https://www.stub-domain\n" in text


def test_a_stub_file_missing_a_key_is_flagged(out, stub_file, tmp_path, capsys):
    stubs = yaml_fast.safe_load(stub_file.read_text())
    del stubs["domain"]
    short = tmp_path / "short.yml"
    short.write_text(shadow_diff.yaml.safe_dump(stubs))
    assert _cluster(out, "--secret-stubs", str(short)) == 2
    assert "['domain']" in capsys.readouterr().err
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
