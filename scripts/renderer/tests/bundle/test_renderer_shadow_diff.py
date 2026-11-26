"""`shadow_diff.py digest` and `diff` over fixture trees.

Run: uv run pytest scripts/renderer/tests/bundle/test_renderer_shadow_diff.py
"""

import shutil
import sys

import pytest

from lib.proc_testing import path_with, write_exec
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


# Prints the dotted path of each leaf that differs, as `dyff between --omit-header` does.
FAKE_DYFF = """\
import sys, yaml
base, head = sys.argv[-2:]
if base.endswith("broken.yaml"):
    sys.exit("cannot load")
def leaves(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, f"{path}.{key}" if path else key)
    else:
        yield path, node
old, new = (dict(leaves(yaml.safe_load(open(f)))) for f in (base, head))
for path in sorted(old.keys() | new.keys()):
    if old.get(path) != new.get(path):
        print(f"{path}\\n  ± value change")
"""


@pytest.fixture
def trees(tmp_path, monkeypatch):
    """Two nested render trees alike, and a stand-in dyff first on PATH."""
    write_exec(tmp_path / "bin/dyff", f"#!{sys.executable}\n{FAKE_DYFF}")
    monkeypatch.setenv("PATH", path_with(tmp_path / "bin"))
    base, head = tmp_path / "base", tmp_path / "head"
    for root in (base, head):
        (root / "cluster/app").mkdir(parents=True)
        (root / "cluster/app/deploy.yaml").write_text(
            "kind: Deployment\nspec:\n  replicas: 1\n  paused: false\n"
        )
        (root / "images/app").mkdir(parents=True)
        (root / "images/app/Dockerfile").write_text("FROM x\n")
        (root / "images/tags.json").write_text('{"app": "sha-1", "web": "sha-2"}')
    return base, head


def test_diff_of_a_reorder_only_change_prints_nothing(trees, run):
    base, head = trees
    (head / "cluster/app/deploy.yaml").write_text(
        "spec:\n  paused: false\n  replicas: 1\nkind: Deployment\n"
    )
    (head / "images/tags.json").write_text('{"web": "sha-2", "app": "sha-1"}')
    (base / "digests.json").write_text('{"app": {"own": "aaa"}}')
    (head / "digests.json").write_text('{"app": {"own": "bbb"}}')
    assert run("diff", base, head) == (0, "files that differ: 0\n", "")


def test_diff_of_a_one_field_change_names_the_field_path(trees, run):
    base, head = trees
    (head / "cluster/app/deploy.yaml").write_text(
        "kind: Deployment\nspec:\n  replicas: 2\n  paused: false\n"
    )
    code, out, _ = run("diff", base, head)
    assert code == 0
    assert out.splitlines() == [
        "files that differ: 1",
        "=== cluster/app/deploy.yaml",
        "spec.replicas",
        "  ± value change",
    ]


def test_diff_names_a_text_change_an_added_file_and_a_removed_file(trees, run):
    base, head = trees
    (head / "images/app/Dockerfile").write_text("FROM y\n")
    (head / "cluster/app/service.yaml").write_text("kind: Service\n")
    (base / "images/tags.json").rename(base / "images/old.json")
    lines = run("diff", base, head)[1].splitlines()
    assert lines[0] == "files that differ: 4"
    assert {"-FROM x", "+FROM y"} <= set(lines)
    assert "Only in head: cluster/app/service.yaml" in lines
    assert "Only in base: images/old.json" in lines


def test_diff_prints_the_rest_then_refuses_when_dyff_fails_on_a_pair(trees, run):
    base, head = trees
    for root in (base, head):
        (root / "cluster/app/broken.yaml").write_text(f"x: {root.name}\n")
    (head / "images/app/Dockerfile").write_text("FROM y\n")
    code, out, err = run("diff", base, head)
    assert code == 2
    assert "+FROM y" in out
    assert "dyff could not compare cluster/app/broken.yaml: cannot load" in err


def test_diff_without_dyff_on_path_is_refused(trees, run, monkeypatch, tmp_path):
    base, head = trees
    (head / "cluster/app/deploy.yaml").write_text("kind: Service\n")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    code, _, err = run("diff", base, head)
    assert code == 2
    assert "shadow_diff diff: refused" in err
