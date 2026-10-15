"""`promote.py check-bundle`'s census against fixture bundles the bundle writer builds.

Run: uv run pytest scripts/deploy_tools/tests/test_bundle_census.py
"""

import bundle_census
import bundle_gates
import promote
from _bundle_fixtures import route
from lib.exit_codes import FAILED, OK
from renderer.renderer_lib.bundle import (
    DIGESTS,
    FLUX_INDEX,
    digests,
    digests_text,
    flux_files,
    kustomization_text,
    write_bundle,
)

TIMEOUT = "600s"
DEPLOYMENT = "kind: Deployment\nmetadata:\n  name: {name}\n"


def _bundle(tmp_path, tags, kinds=()):
    """Write a bundle publishing `tags` and the shared kinds `kinds`, and return its root."""
    names = sorted({*tags, *kinds})
    files = flux_files(dict.fromkeys(names, False), TIMEOUT)
    for name in names:
        files[f"cluster/{name}/deployment.yaml"] = DEPLOYMENT.format(name=name)
        files[f"cluster/{name}/kustomization.yaml"] = kustomization_text(
            ["deployment.yaml"]
        )
    rules = {kind: lambda facts: () for kind in kinds}
    encoded = {path: text.encode() for path, text in files.items()}
    files[DIGESTS] = digests_text(digests(encoded, tags, kinds=kinds, rules=rules))
    out = tmp_path / "out"
    write_bundle(files, out)
    return out


def test_a_bundle_holding_exactly_the_published_tags_is_clean(tmp_path):
    out = _bundle(tmp_path, ["bento-pdf"])
    assert bundle_census.census(out, ["bento-pdf"]) == []


def test_a_published_tag_that_renders_no_file_is_flagged(tmp_path):
    out = _bundle(tmp_path, ["bento-pdf"])
    found = bundle_census.census(out, ["bento-pdf", "sonarr"])
    assert (
        "flux/sonarr.yaml is missing: sonarr is published and renders no file" in found
    )
    assert any(line.startswith("cluster/sonarr/ is missing") for line in found)


def test_an_extra_file_in_flux_is_flagged(tmp_path):
    out = _bundle(tmp_path, ["bento-pdf"])
    (out / "flux" / "stray.yaml").write_text("kind: ConfigMap\n")
    found = bundle_census.census(out, ["bento-pdf"])
    assert "flux/stray.yaml is not a published tag's or a shared kind's file" in found


def test_a_root_list_that_omits_a_file_is_flagged(tmp_path):
    out = _bundle(tmp_path, ["bento-pdf", "sonarr"])
    (out / FLUX_INDEX).write_text(kustomization_text(["bento-pdf.yaml"]))
    found = bundle_census.census(out, ["bento-pdf", "sonarr"])
    assert found == [
        "flux/kustomization.yaml does not list sonarr.yaml, which flux/ holds"
    ]


def test_a_tag_file_holding_another_kustomization_is_flagged(tmp_path):
    out = _bundle(tmp_path, ["bento-pdf"])
    text = (out / "flux" / "bento-pdf.yaml").read_text()
    (out / "flux" / "bento-pdf.yaml").write_text(
        text.replace("name: bento-pdf", "name: x")
    )
    found = bundle_census.census(out, ["bento-pdf"])
    assert any("not exactly the Kustomization bento-pdf" in line for line in found)


def test_a_shared_kind_with_its_own_file_is_clean(tmp_path):
    out = _bundle(tmp_path, ["bento-pdf"], kinds=["cluster-vars"])
    assert bundle_census.census(out, ["bento-pdf"], kinds={"cluster-vars"}) == []


def test_a_shared_kind_with_no_file_is_flagged(tmp_path):
    out = _bundle(tmp_path, ["bento-pdf"])
    found = bundle_census.census(out, ["bento-pdf"], kinds={"cluster-vars"})
    assert (
        "flux/cluster-vars.yaml is missing: cluster-vars is published and renders no file"
        in found
    )


def test_digests_json_naming_another_set_is_flagged(tmp_path):
    out = _bundle(tmp_path, ["bento-pdf"])
    (out / DIGESTS).write_text(digests_text(digests({}, [])))
    found = bundle_census.census(out, ["bento-pdf"])
    assert found == [
        "digests.json names ['_unclassified'], not ['_unclassified', 'bento-pdf']"
    ]


def test_d1s_empty_root_with_no_published_tag_is_clean(tmp_path):
    """The bundle the render step writes itself when no tag is published."""
    out = tmp_path / "out"
    write_bundle(
        {FLUX_INDEX: kustomization_text([]), DIGESTS: digests_text(digests({}, []))},
        out,
    )
    assert bundle_census.census(out, []) == []


def _host_vars(tmp_path, owners):
    path = tmp_path / "daniel-box.yml"
    path.write_text(
        "containers_list:\n"
        + "".join(
            f"  - name: {name}\n    platform: k8s\n    owner: {owner}\n"
            for name, owner in owners.items()
        )
    )
    return path


def test_a_released_tag_is_absent_from_the_bundle_and_the_census_is_clean(tmp_path):
    host_vars = _host_vars(tmp_path, {"bento-pdf": "flux", "sonarr": "released"})
    assert bundle_census.published_tags(host_vars) == ["bento-pdf"]
    out = _bundle(tmp_path, ["bento-pdf"])
    # The CLI runs the publish gates too, and the host gate's named member is this route.
    domain = bundle_gates.inventory_domain()
    (out / "cluster" / "bento-pdf" / "ingressroute.yaml").write_text(route(domain))
    assert promote.main(["check-bundle", str(out), "--host-vars", str(host_vars)]) == OK


def test_a_flux_owned_tag_missing_from_the_bundle_is_flagged(tmp_path, capsys):
    host_vars = _host_vars(tmp_path, {"bento-pdf": "flux", "sonarr": "flux"})
    out = _bundle(tmp_path, ["bento-pdf"])
    code = promote.main(["check-bundle", str(out), "--host-vars", str(host_vars)])
    assert code == FAILED
    assert "flux/sonarr.yaml is missing" in capsys.readouterr().err


def test_published_tags_prints_nothing_when_no_tag_is_flux_owned(tmp_path, capsys):
    host_vars = _host_vars(tmp_path, {"sonarr": "released"})
    assert promote.main(["published-tags", "--host-vars", str(host_vars)]) == OK
    assert capsys.readouterr().out == ""
