"""The tag rule's publish arm, `promote.py check-bundle --against-prod`, against a fake GHCR.

`prod` is a `_promote_fakes.World` bundle, and the head is a bundle on disk. The two tests
through `promote.main` read the tag rule's own line, so the census's verdict on the fixture does
not decide them.

Run: uv run pytest scripts/deploy_tools/tests/test_tag_rule.py
"""

import pytest

import promote
import tag_rule
from _bundle_fixtures import bundle
from _promote_fakes import World, digest_of
from lib.exit_codes import FAILED, OK, RENDER_OK
from lib.repo_paths import K8S_ROLES
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE, write_bundle

DEPLOYMENT = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {name}\n"
VOLUME = "apiVersion: v1\nkind: PersistentVolume\nmetadata:\n  name: media-data\n"
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"


def _cluster(*tags):
    return {tag: {"deployment.yaml": DEPLOYMENT.format(name=tag)} for tag in tags}


def _encoded(cluster):
    return {path: text.encode() for path, text in bundle(cluster).items()}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path, size=1)


@pytest.fixture
def roles(tmp_path):
    """A roles directory holding `flux` and `bento-pdf`, which the head keeps."""
    root = tmp_path / "roles"
    for name in ("flux", "bento-pdf"):
        (root / name).mkdir(parents=True)
        (root / name / "CLAUDE.md").write_text("role")
    return root


def _set_prod(world, cluster):
    """Point `prod` at a bundle holding `cluster`, its tag's manifest carrying the layer."""
    sha = world.commits[0]
    world.set_prod(sha, files=_encoded(cluster))
    manifests = world.transport.manifests
    manifests["prod"]["layers"] = manifests[digest_of(sha)]["layers"]


def _judge(world, tmp_path, roles, prod_cluster, head_cluster):
    _set_prod(world, prod_cluster)
    out = tmp_path / "out"
    write_bundle(bundle(head_cluster), out)
    return tag_rule.check_publish(out, world.clients().registry, roles)


def test_a_tag_at_prod_missing_while_its_role_stays_is_flagged(
    world, tmp_path, roles, capsys
):
    code = _judge(
        world, tmp_path, roles, _cluster("flux", "bento-pdf"), _cluster("flux")
    )
    assert code == FAILED
    assert (
        "tag rule: bento-pdf leaves the bundle while ansible/roles/k8s/bento-pdf/"
        in capsys.readouterr().err
    )


def test_a_tag_dropped_together_with_its_role_is_clean(world, tmp_path, roles):
    prod = _cluster("flux", "bento-pdf")
    (roles / "bento-pdf" / "CLAUDE.md").unlink()
    (roles / "bento-pdf").rmdir()
    assert _judge(world, tmp_path, roles, prod, _cluster("flux")) == OK


def test_flux_missing_at_the_head_is_flagged_whatever_its_directory(
    world, tmp_path, roles, capsys
):
    (roles / "flux" / "CLAUDE.md").unlink()
    (roles / "flux").rmdir()
    code = _judge(world, tmp_path, roles, _cluster("bento-pdf"), _cluster("bento-pdf"))
    assert code == FAILED
    assert "tag rule: flux is not in the render" in capsys.readouterr().err


def test_a_refused_drop_stays_refused_on_an_unrelated_commit_while_prod_is_unmoved(
    world, tmp_path, roles, capsys
):
    """The refused commit is published and already lacks the tag, so only `prod` still holds it."""
    refused = world.commit("Drop bento-pdf's entry")
    world.publish(refused, files=_encoded(_cluster("flux")))
    manifests = world.transport.manifests
    manifests[f"sha-{refused}"] = manifests[digest_of(refused)]
    world.commit("An unrelated commit")
    head = _cluster("flux")
    head["flux"]["unrelated.yaml"] = DEPLOYMENT.format(name="unrelated")
    assert _judge(world, tmp_path, roles, _cluster("flux", "bento-pdf"), head) == FAILED
    assert "tag rule: bento-pdf leaves the bundle" in capsys.readouterr().err


def test_a_failed_pull_of_prod_fails_the_publish(world, tmp_path, roles, capsys):
    world.transport.forced[tag_rule.PROD_TAG] = 500
    out = tmp_path / "out"
    write_bundle(bundle(_cluster("flux", "bento-pdf")), out)
    assert tag_rule.check_publish(out, world.clients().registry, roles) == FAILED
    assert "nothing may be published" in capsys.readouterr().err


def test_no_prod_in_the_registry_is_the_first_run_and_is_clean(
    world, tmp_path, roles, capsys
):
    out = tmp_path / "out"
    write_bundle(bundle(_cluster("flux", "bento-pdf")), out)
    assert tag_rule.check_publish(out, world.clients().registry, roles) == OK
    assert "the first run" in capsys.readouterr().out


def test_a_tag_whose_prod_render_holds_a_volume_is_flagged_with_its_role_gone(
    world, tmp_path, roles, capsys
):
    prod = _cluster("flux") | {"media-volume": {"pv.yaml": VOLUME}}
    assert _judge(world, tmp_path, roles, prod, _cluster("flux")) == FAILED
    assert "media-volume leaves the bundle and its render holds" in (
        capsys.readouterr().err
    )


def test_a_tag_whose_prod_render_holds_no_volume_is_clean_with_its_role_gone(
    world, tmp_path, roles
):
    prod = _cluster("flux") | {
        "media-volume": {"pvc.yaml": VOLUME.replace("Volume", "VolumeClaim")}
    }
    assert _judge(world, tmp_path, roles, prod, _cluster("flux")) == OK


def test_media_volume_is_the_rules_named_member(tmp_path):
    """The live render of media-volume holds the PersistentVolume the rule reads."""
    assert (K8S_ROLES / "media-volume").is_dir()
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text(DOTENV)
    out = tmp_path / "out"
    assert (
        homelab_render.cluster(out, ["media-volume"], None, cluster_file=cluster_file)
        == RENDER_OK
    )
    files = {
        path.relative_to(out).as_posix(): path.read_bytes()
        for path in out.rglob("*.yaml")
    }
    assert len(files) >= 2
    objects = (
        (ident, directory) for ident, directory, _ in tag_rule.bundle_documents(files)
    )
    assert tag_rule.persistent_volume_tags(objects) == {"media-volume"}


def _main(world, tmp_path, *flags):
    _set_prod(world, _cluster("flux", "bento-pdf"))
    out = tmp_path / "out"
    write_bundle(bundle(_cluster("flux")), out)
    host_vars = tmp_path / "daniel-box.yml"
    host_vars.write_text("containers_list: []\n")
    argv = ["check-bundle", str(out), "--host-vars", str(host_vars), *flags]
    return promote.main(argv, f=world.clients())


def test_check_bundle_against_prod_refuses_the_drop(world, tmp_path, capsys):
    assert _main(world, tmp_path, "--against-prod") == FAILED
    assert "tag rule: bento-pdf leaves the bundle" in capsys.readouterr().err


def test_check_bundle_without_the_flag_reads_no_registry(world, tmp_path, capsys):
    _main(world, tmp_path)
    assert "tag rule" not in capsys.readouterr().err
    assert world.transport.registry_requests() == []
