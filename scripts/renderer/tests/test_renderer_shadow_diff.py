"""`shadow_diff.py compare`, `stubcheck` and `digest` over fixture trees in both layouts.

Run: uv run pytest scripts/renderer/tests/test_renderer_shadow_diff.py
"""

import base64
import shutil

import pytest

from renderer import shadow_diff
from renderer.renderer_lib import shadow

TARGETS = ["jellyfin", "pihole", "traefik"]
# The Ansible manifests role names a deferred directory by `manifests_deferred_dir_name`.
DEFERRED_DIR = {"pihole": "pihole-instance-2"}
SECRET_VALUE = base64.b64encode(b"user=stub-jellyfin-api-key").decode()
# Each file keyed by its place in the C1 layout, the one shape both writers below start from.
FILES = {
    "traefik/deployment.yaml": "kind: Deployment\nhost: traefik.local.stub-domain\n",
    "pihole/service.yaml": "kind: Service\nname: pihole\n",
    "pihole-deferred/deployment-2.yaml": "kind: Deployment\nname: pihole-2\n",
    "jellyfin/claim-jellyfin-config.yaml": "kind: PersistentVolumeClaim\n",
    "jellyfin/secret.yaml": f"kind: Secret\ndata:\n  api: {SECRET_VALUE}\n",
}


def _write_ansible(root, files):
    for key, text in files.items():
        directory, name = key.split("/")
        tag = directory.removesuffix("-deferred")
        path = root / tag / (DEFERRED_DIR[tag] if tag != directory else "") / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _write_c1(root, files):
    for key, text in files.items():
        path = root / "cluster" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        # The renderer writes one per directory, and compare must ignore it.
        (path.parent / "kustomization.yaml").write_text("resources: []\n")
    return root


@pytest.fixture
def trees(tmp_path):
    """An Ansible tree and a C1 tree of the same render, as `(ansible, c1)`."""
    return (
        _write_ansible(tmp_path / "ansible", FILES),
        _write_c1(tmp_path / "render", FILES),
    )


@pytest.fixture
def run(capsys):
    """Run `shadow_diff.main`, with the tag sets given rather than read from the inventory."""

    def _run(*argv, targets=TARGETS, flux=()):
        code = shadow_diff.main(
            [str(a) for a in argv], tags=lambda: (list(targets), set(flux))
        )
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


# -- compare ------------------------------------------------------------------------------


def test_compare_of_identical_trees_is_clean(trees, run):
    code, out, _ = run("compare", *trees)
    assert code == 0
    assert out.splitlines() == ["3 tags, 5 files, 0 differences"]


def test_compare_of_a_one_byte_change_is_flagged(trees, run):
    ansible, c1 = trees
    (c1 / "cluster/pihole/service.yaml").write_text("kind: Service\nname: piholE\n")
    code, out, _ = run("compare", ansible, c1)
    assert code == 1
    assert "1 differences" in out.splitlines()[0]
    assert out.splitlines()[1:3] == [
        "differs in content (1):",
        "  pihole/service.yaml",
    ]


@pytest.mark.parametrize(
    ("change", "cls"),
    [
        ("delete", "only in ansible (1):"),
        ("add", "only in render (1):"),
    ],
)
def test_compare_of_a_missing_or_extra_file_is_flagged(trees, run, change, cls):
    ansible, c1 = trees
    if change == "delete":
        (c1 / "cluster/pihole/service.yaml").unlink()
    else:
        (c1 / "cluster/pihole/extra.yaml").write_text("kind: ConfigMap\n")
    code, out, _ = run("compare", ansible, c1)
    assert code == 1
    assert cls in out.splitlines()


@pytest.mark.parametrize(
    ("ansible_text", "cls"),
    [
        ("kind: Service\nname: pihole\n\n", shadow.TRAILING),
        ("kind:  Service\nname: pihole\n", shadow.WHITESPACE),
        ("kind: Service\nname: other\n", shadow.CONTENT),
    ],
)
def test_compare_groups_a_difference_by_its_class_is_flagged(
    trees, run, ansible_text, cls
):
    ansible, c1 = trees
    (ansible / "pihole/service.yaml").write_text(ansible_text)
    code, out, _ = run("compare", ansible, c1)
    assert code == 1
    assert f"{cls} (1):" in out.splitlines()


def test_compare_ignoring_a_flux_owned_render_directory_is_clean(trees, run):
    ansible, c1 = trees
    _write_c1(c1, {"valheim/deployment.yaml": "kind: Deployment\n"})
    code, out, _ = run(
        "compare", ansible, c1, targets=[*TARGETS, "valheim"], flux=["valheim"]
    )
    assert code == 0
    assert out.splitlines() == ["3 tags, 5 files, 0 differences"]


def test_compare_of_an_ansible_directory_outside_the_owned_set_is_flagged(trees, run):
    ansible, c1 = trees
    _write_ansible(ansible, {"valheim/deployment.yaml": "kind: Deployment\n"})
    code, out, _ = run(
        "compare", ansible, c1, targets=[*TARGETS, "valheim"], flux=["valheim"]
    )
    assert code == 1
    assert "in ansible for a tag outside the expected set (1):" in out.splitlines()
    assert "  valheim/deployment.yaml" in out.splitlines()


def test_compare_of_a_tag_rendered_on_neither_side_is_flagged(trees, run):
    code, out, _ = run("compare", *trees, targets=[*TARGETS, "littlelink"])
    assert code == 1
    assert out.splitlines()[0] == "4 tags, 5 files, 1 differences"
    assert "  littlelink/" in out.splitlines()


def test_compare_over_an_empty_tag_set_is_flagged(trees, run):
    code, out, _ = run("compare", *trees, targets=[])
    assert code == 2
    assert "refused: the expected tag set is empty" in out.splitlines()


def test_compare_of_two_empty_trees_is_flagged(tmp_path, run):
    (tmp_path / "ansible").mkdir()
    (tmp_path / "render" / "cluster").mkdir(parents=True)
    code, out, _ = run("compare", tmp_path / "ansible", tmp_path / "render")
    assert code == 2
    assert out.splitlines()[0] == "3 tags, 0 files, 3 differences"
    assert "refused: neither tree holds a file for an expected tag" in out.splitlines()


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("traefik/deployment.yaml", "traefik/deployment.yaml"),
        ("pihole-deferred/deployment-2.yaml", "pihole-instance-2"),
        ("jellyfin/claim-jellyfin-config.yaml", "a claim-*.yaml file"),
    ],
)
def test_compare_without_a_named_member_is_flagged(tmp_path, run, key, label):
    files = {k: v for k, v in FILES.items() if k != key}
    ansible = _write_ansible(tmp_path / "ansible", files)
    c1 = _write_c1(tmp_path / "render", files)
    code, out, _ = run("compare", ansible, c1)
    assert code == 2
    assert f"refused: no file for named member {label} in ansible" in out.splitlines()
    assert f"refused: no file for named member {label} in render" in out.splitlines()


def test_compare_dropping_a_flux_owned_named_member_is_clean(tmp_path, run):
    ansible = _write_ansible(
        tmp_path / "ansible",
        {k: v for k, v in FILES.items() if not k.startswith("traefik/")},
    )
    c1 = _write_c1(tmp_path / "render", FILES)
    code, out, _ = run("compare", ansible, c1, flux=["traefik"])
    assert code == 0
    assert out.splitlines() == [
        "2 tags, 4 files, 0 differences",
        "named member traefik/deployment.yaml dropped: traefik is flux-owned",
    ]


def test_compare_of_a_misplaced_file_is_flagged(trees, run):
    ansible, c1 = trees
    nested = ansible / "pihole/pihole-instance-2/deeper/x.yaml"
    nested.parent.mkdir()
    nested.write_text("kind: Service\n")
    code, _, err = run("compare", ansible, c1)
    assert code == 2
    assert "pihole/pihole-instance-2/deeper/x.yaml is not where" in err


# -- what compare prints ------------------------------------------------------------------


def test_compare_prints_a_hunk_with_its_stub_tokens_redacted(trees, run):
    ansible, c1 = trees
    text = "kind: Deployment\nhost: traefik.local.stub-domain\nport: 80\n"
    (ansible / "traefik/deployment.yaml").write_text(text)
    code, out, _ = run("compare", ansible, c1)
    assert code == 1
    assert " host: traefik.local.<stub>" in out.splitlines()
    assert "-port: 80" in out
    assert "stub-" not in out


def test_compare_withholds_a_file_lacking_a_stub_token_is_flagged(trees, run):
    ansible, c1 = trees
    (ansible / "traefik/deployment.yaml").write_text(
        "kind: Deployment\nhost: traefik.local.example-real-name\n"
    )
    code, out, _ = run("compare", ansible, c1)
    assert code == 1
    assert f"{shadow.STUB_CHECK} (1):" in out.splitlines()
    assert "  traefik/deployment.yaml" in out.splitlines()
    assert "example-real-name" not in out
    assert "@@" not in out


def test_compare_withholds_the_hunk_of_a_differing_secret_is_flagged(trees, run):
    ansible, c1 = trees
    extra = base64.b64encode(b"stub-jellyfin-api-key and more").decode()
    (ansible / "jellyfin/secret.yaml").write_text(
        f"kind: Secret\ndata:\n  api: {SECRET_VALUE}\n  more: {extra}\n"
    )
    code, out, _ = run("compare", ansible, c1)
    assert code == 1
    assert "jellyfin/secret.yaml: holds a Secret; hunk withheld" in out.splitlines()
    assert extra not in out and SECRET_VALUE not in out


# -- stubcheck ----------------------------------------------------------------------------


def test_stubcheck_of_identical_trees_is_clean(trees, run):
    code, out, _ = run("stubcheck", *trees)
    assert code == 0
    assert out.splitlines() == ["5 paired files, 0 lacking a stub token"]


def test_stubcheck_of_a_replaced_stub_token_is_flagged(trees, run):
    ansible, c1 = trees
    (ansible / "traefik/deployment.yaml").write_text(
        "kind: Deployment\nhost: traefik.local.example-real-name\n"
    )
    code, out, _ = run("stubcheck", ansible, c1)
    assert code == 1
    assert "traefik/deployment.yaml: lacks stub-domain" in out.splitlines()
    assert "example-real-name" not in out


def test_stubcheck_of_a_longer_token_in_place_of_a_stub_is_flagged(trees, run):
    ansible, c1 = trees
    (ansible / "traefik/deployment.yaml").write_text(
        "kind: Deployment\nhost: traefik.local.stub-domain-x\n"
    )
    code, out, _ = run("stubcheck", ansible, c1)
    assert code == 1
    assert "traefik/deployment.yaml: lacks stub-domain" in out.splitlines()


def test_stubcheck_of_a_replaced_stub_inside_secret_data_is_flagged(trees, run):
    ansible, c1 = trees
    real = base64.b64encode(b"user=example-real-key").decode()
    (ansible / "jellyfin/secret.yaml").write_text(
        f"kind: Secret\ndata:\n  api: {real}\n"
    )
    code, out, _ = run("stubcheck", ansible, c1)
    assert code == 1
    assert "jellyfin/secret.yaml: lacks stub-jellyfin-api-key" in out.splitlines()
    assert real not in out and "example-real-key" not in out


def test_stubcheck_with_the_trees_swapped_is_flagged(trees, run):
    ansible, c1 = trees
    code, _, err = run("stubcheck", c1, ansible)
    assert code == 2
    assert "pass an Ansible shadow tree first and a C1 tree second" in err


# -- digest -------------------------------------------------------------------------------


def test_digest_of_one_tree_under_two_directories_is_clean(trees, tmp_path, run):
    _, c1 = trees
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
    trees, tmp_path, run
):
    _, c1 = trees
    copy = shutil.copytree(c1, tmp_path / "changed")
    (copy / "cluster/pihole/service.yaml").write_text("kind: Service\nname: piholE\n")
    before = run("digest", c1)[1].splitlines()
    after = run("digest", copy)[1].splitlines()
    moved = [b.split()[0] for b, a in zip(before, after, strict=True) if a != b]
    assert moved == ["pihole", "total"]


def test_digest_of_a_partial_render_matches_the_full_one_for_its_tags(
    trees, tmp_path, run
):
    _, c1 = trees
    partial = shutil.copytree(c1, tmp_path / "partial")
    shutil.rmtree(partial / "cluster/traefik")
    full = run("digest", c1)[1].splitlines()
    half = run("digest", partial)[1].splitlines()
    assert half[:-1] == [line for line in full[:-1] if not line.startswith("traefik ")]
    assert half[-1] != full[-1]


def test_digest_of_a_tree_without_a_cluster_directory_is_flagged(trees, run):
    ansible, _ = trees
    code, _, err = run("digest", ansible)
    assert code == 2
    assert "holds no cluster/ directory" in err


# -- the tag sets -------------------------------------------------------------------------


def test_tag_sets_drop_an_entry_without_owner_flux_from_the_flux_set_is_clean(tmp_path):
    (tmp_path / "daniel-box.yml").write_text(
        "containers_list:\n"
        "  - name: littlelink\n    platform: k8s\n    owner: flux\n"
        "  - name: traefik\n    platform: k8s\n"
    )
    targets, flux = shadow_diff.tag_sets("daniel-box", host_vars=tmp_path)
    assert targets == ["littlelink", "traefik"]
    assert flux == {"littlelink"}
