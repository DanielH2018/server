"""`promote.py check-bundle`'s census against fixture bundles the bundle writer builds.

Each fixture carries every shared kind the renderer writes, its `cluster-vars` copy holding
`DOTENV`, fake ciphertext, rather than the tree's cluster file.

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
    bundle_files,
    digests,
    digests_text,
    kustomization_text,
    write_bundle,
)

TIMEOUT = "600s"
DEPLOYMENT = "kind: Deployment\nmetadata:\n  name: {name}\n"
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"


def _bundle(tmp_path, tags):
    """Write the bundle the renderer builds publishing `tags`, and return its root."""
    directories = {
        tag: {
            "deployment.yaml": DEPLOYMENT.format(name=tag),
            "kustomization.yaml": kustomization_text(["deployment.yaml"]),
        }
        for tag in tags
    }
    files = bundle_files(directories, {}, tags, TIMEOUT, DOTENV)
    for tag, held in directories.items():
        files |= {f"cluster/{tag}/{name}": text for name, text in held.items()}
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
    (out / FLUX_INDEX).write_text(
        kustomization_text(["bento-pdf.yaml", "cluster-vars.yaml"])
    )
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


def test_a_renderer_patched_to_drop_cluster_vars_yaml_is_flagged(tmp_path):
    """The bundle such a renderer writes: no `flux/cluster-vars.yaml`, and a root that
    agrees with what `flux/` holds, so only the allowlist can catch it."""
    out = _bundle(tmp_path, ["bento-pdf"])
    (out / "flux" / "cluster-vars.yaml").unlink()
    (out / FLUX_INDEX).write_text(kustomization_text(["bento-pdf.yaml"]))
    assert bundle_census.census(out, ["bento-pdf"]) == [
        "flux/cluster-vars.yaml is missing: cluster-vars is published and renders no file"
    ]


def test_a_second_document_in_cluster_vars_yaml_is_flagged(tmp_path):
    out = _bundle(tmp_path, ["bento-pdf"])
    path = out / "flux" / "cluster-vars.yaml"
    path.write_text(
        path.read_text() + "---\n" + (out / "flux" / "bento-pdf.yaml").read_text()
    )
    found = bundle_census.census(out, ["bento-pdf"])
    assert len(found) == 1
    assert "not exactly the Kustomization cluster-vars" in found[0]


def test_a_shared_kind_with_no_directory_is_flagged(tmp_path):
    out = _bundle(tmp_path, ["bento-pdf"])
    (out / "cluster" / "cluster-vars" / "kustomization.yaml").unlink()
    assert bundle_census.census(out, ["bento-pdf"]) == [
        "cluster/cluster-vars/ is missing: cluster-vars is published and renders no "
        "directory"
    ]


def test_digests_json_naming_another_set_is_flagged(tmp_path):
    out = _bundle(tmp_path, ["bento-pdf"])
    (out / DIGESTS).write_text(digests_text(digests({}, [])))
    found = bundle_census.census(out, ["bento-pdf"])
    assert found == [
        "digests.json names ['_unclassified', 'cluster-vars'], not "
        "['_unclassified', 'bento-pdf', 'cluster-vars']"
    ]


def test_a_shared_only_bundle_with_no_published_tag_is_clean(tmp_path):
    """The bundle `--shared-only` writes when no tag is published."""
    assert bundle_census.census(_bundle(tmp_path, []), []) == []


def test_d1s_empty_root_with_no_published_tag_is_flagged(tmp_path):
    """The bundle the render step wrote before D3.3a lacks the cluster-vars kind."""
    out = tmp_path / "out"
    write_bundle(
        {FLUX_INDEX: kustomization_text([]), DIGESTS: digests_text(digests({}, []))},
        out,
    )
    assert any(
        line.startswith("flux/cluster-vars.yaml is missing")
        for line in bundle_census.census(out, [])
    )


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
