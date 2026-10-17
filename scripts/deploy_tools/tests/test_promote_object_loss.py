"""`promote.py decide`'s object-loss check, each case a refused and an admitted pair.

The named member is bento-pdf's `.local.` IngressRoute, because the check exists for kinds no
workload rollout covers. Each test publishes bundles from `_bundle_fixtures.py` on a real
throwaway master (`_promote_fakes.World`), whose commit messages carry the trailers.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_object_loss.py
"""

import pytest
from _bundle_fixtures import LOCAL_ROUTE, bundle, route
from _promote_fakes import World
from object_loss import bundle_objects, undeclared_losses
from renderer.renderer_lib.bundle import bundle_files as render_bundle

DEPLOYMENT = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: bento-pdf\n  namespace: homelab\n"
CLUSTER_ROLE = "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: bento-reader\n"
WITH_ROUTE = {"deployment.yaml": DEPLOYMENT, "ingressroute.yaml": route("example.test")}
# bento-pdf with its public route alone: the `.local.` one is the object lost.
WITHOUT_ROUTE = {
    "deployment.yaml": DEPLOYMENT,
    "ingressroute.yaml": route("example.test").split("---\n", 1)[1],
}


def _files(cluster: dict[str, dict[str, str]]) -> dict[str, bytes]:
    return {path: text.encode() for path, text in bundle(cluster).items()}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path, size=1)


def _promote_from(world, prod_cluster, target_cluster, *messages):
    """Point `prod` at master's root holding `prod_cluster`, add a commit per message, and
    publish the last one holding `target_cluster`. Returns the last commit.
    """
    world.set_prod(world.commits[0], files=_files(prod_cluster))
    world.set_last_good(world.commits[0])
    for message in messages:
        target = world.commit(message)
    world.publish(target, files=_files(target_cluster))
    return target


def _skip_line(prod, target, lost):
    return (
        f"promote.py: skip {target}: it loses {lost}, which no Removes-Object line in "
        f"({prod}, {target}] declares"
    )


def test_a_candidate_that_loses_the_route_undeclared_is_refused(
    world, monkeypatch, capsys
):
    target = _promote_from(
        world, {"bento-pdf": WITH_ROUTE}, {"bento-pdf": WITHOUT_ROUTE}, "drop it"
    )
    result = world.decide(monkeypatch, capsys, trigger=target)
    assert (result.exit_code, result.outputs["verdict"], result.outputs["target"]) == (
        0,
        "current",
        "",
    )
    assert _skip_line(world.commits[0], target, LOCAL_ROUTE) in result.stderr


def test_a_candidate_whose_range_declares_the_route_is_promoted(
    world, monkeypatch, capsys
):
    target = _promote_from(
        world,
        {"bento-pdf": WITH_ROUTE},
        {"bento-pdf": WITHOUT_ROUTE},
        f"Drop the local route\n\nRemoves-Object: {LOCAL_ROUTE}",
        "An unrelated commit",
    )
    result = world.decide(monkeypatch, capsys, trigger=target)
    assert (result.outputs["verdict"], result.outputs["target"]) == (
        "promote",
        target,
    ), result.stderr


def test_a_trailer_mid_body_of_a_squash_message_counts(world, monkeypatch, capsys):
    """GitHub's squash body keeps each PR commit's message, so a trailer can sit mid-body."""
    message = f"Squash\n\n* First\n\nremoves-object: {LOCAL_ROUTE}\n\n* Second\n\nCo-Authored-By: x"
    target = _promote_from(
        world, {"bento-pdf": WITH_ROUTE}, {"bento-pdf": WITHOUT_ROUTE}, message
    )
    assert world.decide(monkeypatch, capsys).outputs["target"] == target


def test_a_trailer_outside_the_range_does_not_count(world, monkeypatch, capsys):
    """One trailer on `prod`'s own commit and one on a commit after T: neither is in (prod, T]."""
    declares = f"Drop it\n\nRemoves-Object: {LOCAL_ROUTE}"
    prod = world.commit(declares)
    world.set_prod(prod, files=_files({"bento-pdf": WITH_ROUTE}))
    world.set_last_good(prod)
    target = world.commit("no trailer")
    world.publish(target, files=_files({"bento-pdf": WITHOUT_ROUTE}))
    world.commit(declares)
    result = world.decide(monkeypatch, capsys)
    assert result.outputs["verdict"] == "current"
    assert _skip_line(prod, target, LOCAL_ROUTE) in result.stderr


@pytest.mark.parametrize(
    ("message", "verdict"),
    [
        ("no trailer", "current"),
        ("Drop it\n\nRemoves-Object: ClusterRole//bento-reader", "promote"),
    ],
    ids=["undeclared", "declared"],
)
def test_a_lost_cluster_scoped_object(world, monkeypatch, capsys, message, verdict):
    prod = {"bento-pdf": {**WITHOUT_ROUTE, "clusterrole.yaml": CLUSTER_ROLE}}
    _promote_from(world, prod, {"bento-pdf": WITHOUT_ROUTE}, message)
    result = world.decide(monkeypatch, capsys)
    assert result.outputs["verdict"] == verdict
    assert ("it loses ClusterRole//bento-reader," in result.stderr) == (
        verdict == "current"
    )


def test_a_handback_that_drops_the_whole_kustomization_is_promoted(
    world, monkeypatch, capsys
):
    target = _promote_from(world, {"bento-pdf": WITH_ROUTE}, {}, "Hand bento-pdf back")
    result = world.decide(monkeypatch, capsys)
    assert (result.outputs["verdict"], result.outputs["target"]) == (
        "promote",
        target,
    ), result.stderr


def test_the_walk_skips_a_losing_candidate_for_an_older_one(world, monkeypatch, capsys):
    older = _promote_from(
        world, {"bento-pdf": WITH_ROUTE}, {"bento-pdf": WITH_ROUTE}, "keeps it"
    )
    tip = world.commit("drops it")
    world.publish(tip, files=_files({"bento-pdf": WITHOUT_ROUTE}))
    result = world.decide(monkeypatch, capsys, trigger=tip)
    assert (result.outputs["verdict"], result.outputs["target"]) == ("promote", older)
    assert _skip_line(world.commits[0], tip, LOCAL_ROUTE) in result.stderr


def test_every_candidate_losing_an_object_is_current(world, monkeypatch, capsys):
    older = _promote_from(
        world, {"bento-pdf": WITH_ROUTE}, {"bento-pdf": WITHOUT_ROUTE}, "drops it"
    )
    tip = world.commit("still drops it")
    world.publish(tip, files=_files({"bento-pdf": WITHOUT_ROUTE}))
    result = world.decide(monkeypatch, capsys, trigger=tip)
    assert (result.exit_code, result.outputs["verdict"]) == (0, "current")
    assert "2 lost an object no trailer declares" in result.stderr
    assert all(
        _skip_line(world.commits[0], sha, LOCAL_ROUTE) in result.stderr
        for sha in (tip, older)
    )


# -- a rename by digest is not a removal (D5.1f) --------------------------------------------


def _rendered(revision: str) -> dict[str, bytes]:
    """The shared kinds alone, as the render job writes them for `revision`."""
    files = render_bundle({}, {}, [], "10m", "x=ENC[y]\n", revision=revision)
    return {path: text.encode() for path, text in files.items()}


def test_the_soak_job_renamed_by_the_next_revision_is_no_loss():
    prod, target = _rendered("1" * 40), _rendered("2" * 40)
    assert bundle_objects(prod) != bundle_objects(target), "the soak Job kept its name"
    assert undeclared_losses(prod, target, frozenset()) == []


def test_a_soak_job_the_next_revision_drops_is_a_loss():
    """The red half: a Job whose stem renders nowhere in T is lost."""
    prod = _rendered("1" * 40)
    target = {k: v for k, v in prod.items() if k != "cluster/soak/job.yaml"}
    (job,) = (i for i in bundle_objects(prod) if i.startswith("Job/"))
    assert undeclared_losses(prod, target, frozenset()) == [job]
