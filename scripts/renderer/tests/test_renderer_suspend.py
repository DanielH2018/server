"""D5.1b's suspend lever in the renderer: `suspend: true`, the dropped edges, no `suspend: false`.

Each fixture role is a `defaults/main.yml` under `tmp_path`, which `bundle_files` reads as
`roles`, so no role in the repo sets `flux_suspend`. The plan is
docs/target-architecture/deploy/d5-bulk-move.md, *The stop lever (D5.1b)*.

Run: uv run pytest scripts/renderer/tests/test_renderer_suspend.py
"""

import pytest

from lib import yaml_fast
from lib.k8s_roles import role_fields
from renderer.renderer_lib.bundle import bundle_files
from renderer.renderer_lib.suspension import suspend_false
from renderer.renderer_lib.ordering import (
    SUSPENDED_DEPS,
    Edges,
    Ordering,
    drop_suspended,
    kustomization_edges,
)

TIMEOUT = "600s"
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
CONFIGMAP = "kind: ConfigMap\nmetadata:\n  name: x\n  namespace: homelab\n"
TAGS = ["netpol-baseline", "sonarr", "texbrain"]
# sonarr renders a deferred directory, so netpol-baseline, which applies last, depends on
# `sonarr-deferred` as well as on the two tags its map names.
ORDERING = Ordering(
    deps={"texbrain": ["sonarr"], "netpol-baseline": ["sonarr", "texbrain"]},
    applies_last=frozenset({"netpol-baseline"}),
)
DIRECTORIES = {
    name: {"x.yaml": CONFIGMAP}
    for name in ("netpol-baseline", "sonarr", "sonarr-deferred", "texbrain")
}


def _roles(tmp_path, **defaults: str):
    for tag, text in defaults.items():
        (tmp_path / tag / "defaults").mkdir(parents=True)
        (tmp_path / tag / "defaults" / "main.yml").write_text(text)
    return tmp_path


def _bundle(roles, directories=DIRECTORIES, tags=TAGS, ordering=ORDERING):
    return bundle_files(
        directories, {}, tags, TIMEOUT, DOTENV, (), ordering, roles=roles
    )


def _docs(files) -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc
        for path, text in files.items()
        if path.startswith("flux/") and path != "flux/kustomization.yaml"
        for doc in yaml_fast.safe_load_all(text)
    }


def _depends(doc: dict) -> list[str]:
    return [edge["name"] for edge in doc["spec"].get("dependsOn", [])]


def _suspended_deps(doc: dict) -> str | None:
    return (doc["metadata"].get("annotations") or {}).get(SUSPENDED_DEPS)


def test_a_suspended_tag_is_in_no_depends_on_and_its_dependents_list_it(tmp_path):
    docs = _docs(_bundle(_roles(tmp_path, sonarr="flux_suspend: true\n")))
    for name, doc in docs.items():
        assert not {"sonarr", "sonarr-deferred"} & set(_depends(doc)), name
    assert _suspended_deps(docs["texbrain"]) == "sonarr"
    assert _suspended_deps(docs["netpol-baseline"]) == "sonarr,sonarr-deferred"
    assert _depends(docs["netpol-baseline"]) == ["texbrain"]


def test_an_unsuspended_tag_keeps_its_edges_and_renders_no_suspend(tmp_path):
    """The red half: the same fixture with no `flux_suspend` keeps every edge."""
    docs = _docs(_bundle(_roles(tmp_path)))
    assert _depends(docs["netpol-baseline"]) == [
        "sonarr",
        "sonarr-deferred",
        "texbrain",
    ]
    assert _depends(docs["texbrain"]) == ["sonarr"]
    assert all("suspend" not in doc["spec"] for doc in docs.values())
    assert all(_suspended_deps(doc) is None for doc in docs.values())


def test_every_kustomization_of_a_suspended_tag_renders_suspend_true(tmp_path):
    roles = _roles(tmp_path, sonarr="flux_suspend: true\n")
    files = _bundle(roles, DIRECTORIES | {"sonarr-verify": {"x.yaml": CONFIGMAP}})
    docs = _docs(files)
    assert {n for n, d in docs.items() if d["spec"].get("suspend") is True} == {
        "sonarr",
        "sonarr-deferred",
        "sonarr-verify",
    }
    # An edge inside the suspended tag drops too; the resume PR restores every edge at once.
    assert "dependsOn" not in docs["sonarr-verify"]["spec"]
    assert _suspended_deps(docs["sonarr-verify"]) == "sonarr-deferred"
    assert yaml_fast.safe_load(files["digests.json"])["sonarr"]["suspended"] is True


def test_a_soak_shaped_kustomization_drops_the_suspended_tags_kustomizations():
    """D5.3b2's `soak` depends on every Kustomization and goes through the same step."""
    edges = kustomization_edges(TAGS, DIRECTORIES, ORDERING)
    with_soak = {**edges, "soak": Edges(frozenset(edges))}
    soak = drop_suspended(with_soak, {"sonarr"})["soak"]
    assert soak.suspended == {"sonarr", "sonarr-deferred"}
    assert soak.kept == {"netpol-baseline", "texbrain"}


def test_a_suspended_flux_owned_tag_outside_the_render_is_dropped(tmp_path):
    """The suspended set comes from every flux-owned role, so a `--role` subset agrees."""
    ordering = Ordering(
        deps={"texbrain": ["sonarr"]},
        flux_owned=frozenset({"sonarr", "texbrain"}),
        suspended=frozenset({"sonarr"}),
    )
    doc = _docs(_bundle(tmp_path, {}, ["texbrain"], ordering))["texbrain"]
    assert "dependsOn" not in doc["spec"]
    assert _suspended_deps(doc) == "sonarr"


def test_no_document_in_the_fixture_bundle_renders_suspend_false(tmp_path):
    files = _bundle(_roles(tmp_path, sonarr="flux_suspend: true\n"))
    cluster = {
        f"cluster/{n}/{f}": t for n, d in DIRECTORIES.items() for f, t in d.items()
    }
    assert suspend_false({**files, **cluster}) == []


def test_a_fixture_template_that_writes_suspend_false_fails_the_render(tmp_path):
    template = (
        "apiVersion: kustomize.toolkit.fluxcd.io/v1\nkind: Kustomization\n"
        "metadata:\n  name: inner\nspec:\n  suspend: false\n"
    )
    directories = DIRECTORIES | {"texbrain": {"inner.yaml": template}}
    with pytest.raises(ValueError, match=r"suspend: false on .*Kustomization/inner"):
        _bundle(_roles(tmp_path), directories)


def test_a_cronjob_with_suspend_false_is_clean():
    """A CronJob's `spec.suspend` is the batch field, not Flux's."""
    cronjob = "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: c\nspec:\n  suspend: false\n"
    assert suspend_false({"cluster/texbrain/cronjob.yaml": cronjob}) == []


def test_a_flux_suspend_that_is_not_a_literal_bool_is_a_problem(tmp_path):
    _roles(tmp_path, sonarr='flux_suspend: "{{ sonarr_paused }}"\n')
    found, problems = role_fields(["sonarr"], tmp_path)
    assert found == {}
    assert problems == [
        "sonarr's flux_suspend is '{{ sonarr_paused }}', not a literal bool"
    ]
