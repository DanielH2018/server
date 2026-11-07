"""`shadow_diff.py digest` over a fixture C1 tree, and the two subcommands R4.8 retired.

Run: uv run pytest scripts/renderer/tests/test_renderer_shadow_diff.py
"""

import shutil

import pytest

from renderer import shadow_diff

# Each file keyed by its place in the C1 layout.
FILES = {
    "traefik/deployment.yaml": "kind: Deployment\nhost: traefik.local.stub-domain\n",
    "pihole/service.yaml": "kind: Service\nname: pihole\n",
    "pihole-deferred/deployment-2.yaml": "kind: Deployment\nname: pihole-2\n",
    "jellyfin/claim-jellyfin-config.yaml": "kind: PersistentVolumeClaim\n",
}


@pytest.fixture
def c1(tmp_path):
    """A C1 tree, with `cluster/` at the top as `homelab_render.py cluster` writes it."""
    root = tmp_path / "render"
    for key, text in FILES.items():
        path = root / "cluster" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


@pytest.fixture
def run(capsys):
    """Run `shadow_diff.main`."""

    def _run(*argv):
        code = shadow_diff.main([str(a) for a in argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


@pytest.mark.parametrize("command", ["compare", "stubcheck"])
def test_the_ansible_tree_subcommands_are_gone_is_flagged(c1, capsys, command):
    with pytest.raises(SystemExit) as caught:
        shadow_diff.main([command, str(c1), str(c1)])
    assert caught.value.code == 64
    assert "invalid choice" in capsys.readouterr().err


def test_digest_of_one_tree_under_two_directories_is_clean(c1, tmp_path, run):
    copy = shutil.copytree(c1, tmp_path / "elsewhere" / "render")
    first, second = run("digest", c1), run("digest", copy)
    assert first[0] == 0
    assert first[1] == second[1]
    assert [line.split()[0] for line in first[1].splitlines()] == [
        "jellyfin",
        "pihole",
        "pihole-deferred",
        "traefik",
        "total",
    ]


def test_digest_of_a_one_byte_change_moves_one_directory_and_the_total(
    c1, tmp_path, run
):
    copy = shutil.copytree(c1, tmp_path / "changed")
    (copy / "cluster/pihole/service.yaml").write_text("kind: Service\nname: piholE\n")
    before = run("digest", c1)[1].splitlines()
    after = run("digest", copy)[1].splitlines()
    moved = [b.split()[0] for b, a in zip(before, after, strict=True) if a != b]
    assert moved == ["pihole", "total"]


def test_digest_of_a_partial_render_matches_the_full_one_for_its_tags(
    c1, tmp_path, run
):
    partial = shutil.copytree(c1, tmp_path / "partial")
    shutil.rmtree(partial / "cluster/traefik")
    full = run("digest", c1)[1].splitlines()
    half = run("digest", partial)[1].splitlines()
    assert half[:-1] == [line for line in full[:-1] if not line.startswith("traefik ")]
    assert half[-1] != full[-1]


def test_digest_of_a_tree_without_a_cluster_directory_is_flagged(tmp_path, run):
    code, _, err = run("digest", tmp_path)
    assert code == 2
    assert "holds no cluster/ directory" in err
