"""D5.1a2's two fields on each tag's object in `digests.json`: `claims` and `suspended`.

`claims` comes from `snapshot_claims`, the reader the publish census's `-snapshot` rule reads
too, and `suspended` from the rendered Kustomization `<tag>`. A shared kind's object and
`_unclassified` carry neither (C1 *Cluster output*).

Run: uv run pytest scripts/renderer/tests/test_renderer_digest_fields.py
"""

import json

import yaml

from changed_set import changed_set
from deploy_tools.bundle_census import snapshot_tags
from lib.k8s_roles import snapshot_claims
from renderer.renderer_lib.bundle_readme import BUNDLE_README
from renderer.renderer_lib.bundle import (
    CLUSTER_VARS,
    DIGESTS,
    UNCLASSIFIED,
    bundle_files,
    digests,
)

TIMEOUT = "600s"
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
DIRECTORIES = {
    "sonarr": {"deployment.yaml": "kind: Deployment\nmetadata:\n  name: sonarr\n"},
    "bento-pdf": {
        "deployment.yaml": "kind: Deployment\nmetadata:\n  name: bento-pdf\n"
    },
}


def _digests(roles, claims: dict[str, list[str]] | None = None) -> dict:
    """`digests.json` for `DIRECTORIES`, from a role tree declaring `claims`."""
    for tag, names in (claims or {}).items():
        defaults = roles / tag / "defaults"
        defaults.mkdir(parents=True, exist_ok=True)
        (defaults / "main.yml").write_text(
            yaml.safe_dump({"k8s_autodeploy_snapshot_pvcs": names})
        )
    files = bundle_files(
        DIRECTORIES, {}, list(DIRECTORIES), TIMEOUT, DOTENV, roles=roles
    )
    return json.loads(files[DIGESTS])


def test_each_tags_object_carries_its_claims_and_suspended(tmp_path):
    result = _digests(tmp_path, {"sonarr": ["sonarr-config"]})
    assert result["sonarr"]["claims"] == ["sonarr-config"]
    assert result["bento-pdf"]["claims"] == []
    assert result["sonarr"]["suspended"] is False
    assert result["bento-pdf"]["suspended"] is False


def test_a_shared_kind_and_unclassified_carry_neither_field(tmp_path):
    result = _digests(tmp_path, {"sonarr": ["sonarr-config"]})
    for name in (CLUSTER_VARS, UNCLASSIFIED):
        assert "claims" not in result[name] and "suspended" not in result[name], name


def test_a_tag_whose_kustomization_sets_suspend_reads_as_suspended():
    files = {"flux/sonarr.yaml": yaml.safe_dump({"spec": {"suspend": True}}).encode()}
    files["flux/bento-pdf.yaml"] = yaml.safe_dump({"spec": {"prune": True}}).encode()
    result = digests(files, ["sonarr", "bento-pdf"])
    assert result["sonarr"]["suspended"] is True
    assert result["bento-pdf"]["suspended"] is False


def test_the_fields_move_no_digest_and_changed_set_still_reads_the_file(tmp_path):
    plain = _digests(tmp_path / "plain")
    claimed = _digests(tmp_path / "claimed", {"sonarr": ["sonarr-config"]})
    for name in DIRECTORIES:
        assert plain[name]["own"] == claimed[name]["own"]
        assert plain[name]["footprint"] == claimed[name]["footprint"]
    graph = {name: [] for name in DIRECTORIES}
    moved = changed_set(claimed, plain, graph)
    assert moved.names == frozenset() and moved.widened == ""


def test_live_claims_agree_with_the_censuss_snapshot_rule():
    # Three roles that declare snapshot claims and one that declares none.
    tags = ["code-server", "sonarr", "tdarr", "peanut"]
    claims, problems = snapshot_claims(tags)
    assert problems == []
    assert claims["code-server"] == ["code-server-config", "code-server-workspace"]
    assert claims["sonarr"] == ["sonarr-config"]
    assert claims["peanut"] == []
    assert snapshot_tags(tags)[0] == {t for t, c in claims.items() if c}


def test_a_claims_value_that_is_not_a_literal_list_is_flagged(tmp_path):
    defaults = tmp_path / "sonarr" / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "main.yml").write_text("k8s_autodeploy_snapshot_pvcs: '{{ x }}'\n")
    claims, problems = snapshot_claims(["sonarr"], tmp_path)
    assert claims == {}
    assert problems and "not a literal list" in problems[0]


def test_the_bundle_readme_is_the_one_unclassified_path_and_a_change_widens_the_set():
    files = bundle_files(DIRECTORIES, {}, list(DIRECTORIES), TIMEOUT, DOTENV)
    assert files["README.md"] == BUNDLE_README
    old = json.loads(files[DIGESTS])
    assert old[UNCLASSIFIED]["paths"] == ["README.md"]
    new = {**old, UNCLASSIFIED: {**old[UNCLASSIFIED], "footprint": "other"}}
    graph = {name: [] for name in DIRECTORIES}
    assert changed_set(old, old, graph).names == frozenset()
    widened = changed_set(new, old, graph)
    assert widened.names == {*DIRECTORIES, CLUSTER_VARS, "soak"}
    assert UNCLASSIFIED in widened.widened
