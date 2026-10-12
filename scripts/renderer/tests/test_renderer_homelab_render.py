"""`homelab_render.py cluster`: its refusals and the C1 layout of a stub render.

Run: uv run pytest scripts/renderer/tests/test_renderer_homelab_render.py
"""

import pytest

from lib import yaml_fast
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


def test_cluster_without_secret_stubs_exits_2_and_writes_nothing(out, capsys):
    assert _cluster(out) == 2
    # Every refusal exits 2, so the message is what proves it is this one.
    assert "placeholder mode" in capsys.readouterr().err
    assert list(out.iterdir()) == []


def test_cluster_renders_littlelink_in_the_c1_layout(out, stub_file):
    assert _cluster(out, "--secret-stubs", str(stub_file)) == 0

    assert sorted(p.name for p in (out / "cluster").iterdir()) == ["littlelink"]
    tag = out / "cluster" / "littlelink"
    files = sorted(p.name for p in tag.iterdir())
    assert files == ["deployment.yaml", "kustomization.yaml"]
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


def test_a_tag_that_does_not_render_yet_is_flagged(out, stub_file, capsys):
    assert _cluster(out, "--secret-stubs", str(stub_file), "--role", "authelia") == 2
    assert "do not render yet" in capsys.readouterr().err
    assert list(out.iterdir()) == []


def test_a_tag_directory_left_by_an_earlier_render_is_flagged(out, stub_file, capsys):
    assert _cluster(out, "--secret-stubs", str(stub_file), "--role", "littlelink") == 0
    assert _cluster(out, "--secret-stubs", str(stub_file), "--role", "littlelink") == 2
    assert "render into an empty --out" in capsys.readouterr().err
