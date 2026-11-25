"""`shadow_diff.py digest` over a fixture C1 tree, and the two subcommands R4.8 retired.

Run: uv run pytest scripts/renderer/tests/test_renderer_shadow_diff.py
"""

import shutil

import pytest

from lib.proc_testing import write_exec
from renderer import shadow_diff
from renderer.renderer_lib import tree_diff

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


FAKE_DYFF = """\
#!/usr/bin/env python3
import sys, yaml
base, head = sys.argv[-2:]
if base.endswith("broken.yaml"):
    sys.exit("cannot load")
old, new = (yaml.safe_load(open(f)) for f in (base, head))
if old != new:
    print("spec.replicas")
"""


@pytest.fixture
def trees(tmp_path):
    """Two nested render trees and a stand-in dyff that compares parsed YAML."""
    fake = write_exec(tmp_path / "dyff", FAKE_DYFF)
    base, head = tmp_path / "base", tmp_path / "head"
    for root in (base, head):
        (root / "cluster/app").mkdir(parents=True)
        (root / "README.md").write_text("# not: valid: yaml\n")
        (root / "cluster/app/deploy.yaml").write_text("a: 1\nb: 2\n")
        (root / "cluster/app/Dockerfile").write_text("FROM x\n")
    return base, head, fake


def test_a_reordered_key_prints_no_difference(trees):
    base, head, fake = trees
    (head / "cluster/app/deploy.yaml").write_text("b: 2\na: 1\n")
    assert tree_diff.diff_trees(base, head, str(fake)) == []


def test_a_changed_field_prints_its_path(trees):
    base, head, fake = trees
    (head / "cluster/app/deploy.yaml").write_text("a: 1\nb: 3\n")
    assert tree_diff.diff_trees(base, head, str(fake)) == [
        "=== cluster/app/deploy.yaml",
        "spec.replicas",
    ]


def test_a_changed_dockerfile_and_an_added_file_are_reported(trees):
    base, head, fake = trees
    (head / "cluster/app/Dockerfile").write_text("FROM y\n")
    (head / "cluster/app/new.yaml").write_text("a: 1\n")
    lines = tree_diff.diff_trees(base, head, str(fake))
    assert "-FROM x" in lines
    assert "+FROM y" in lines
    assert "Only in head: cluster/app/new.yaml" in lines


def test_a_dyff_error_is_raised_not_hidden(trees):
    base, head, fake = trees
    for root in (base, head):
        (root / "cluster/app/broken.yaml").write_text(f"x: {root.name}\n")
    with pytest.raises(tree_diff.DyffFailed):
        tree_diff.diff_trees(base, head, str(fake))
