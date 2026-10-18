"""Which diff the takeover check runs for a suffixed directory, found by its Kustomization (#5575).

A `<tag>-verify`, `-snapshot` or `-deferred` directory has no flux file of its own: its
Kustomization is a later document of `flux/<base>.yaml`. The wrapper finds the tag's document
by `metadata.name`, runs `flux diff` when that document carries `postBuild` and `kubectl diff`
otherwise. The rig and its fake binaries are test_takeover_check.py's.

Run: uv run pytest ansible/roles/setup/claude_code/tests/test_takeover_check_flux_selection.py
"""

from pathlib import Path

import pytest
from test_takeover_check import Rig, assert_accepted, root_calls
from test_takeover_check import home_dir  # noqa: F401 (the fixture, found by name)


@pytest.fixture
def rig(request) -> Rig:
    return Rig(request.getfixturevalue("home_dir"))


def kustomization(name: str, *, substitutes: bool) -> str:
    """One Kustomization document of a rendered `flux/<base>.yaml`."""
    doc = (
        "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
        "kind: Kustomization\n"
        f"metadata:\n  name: {name}\n  namespace: flux-system\n"
        f"spec:\n  path: ./cluster/{name}\n"
    )
    if substitutes:
        doc += "  postBuild:\n    substituteFrom:\n      - kind: Secret\n        name: cluster-vars\n"
    return doc


def kustomization_file_flux_read(rig: Rig) -> str:
    """The document the wrapper handed `flux diff` as `--kustomization-file`."""
    words = next(
        x for x in rig.calls().splitlines() if x.startswith("flux argv: ")
    ).split()
    return Path(words[words.index("--kustomization-file") + 1]).read_text()


SONARR_NOTIFICATION = (
    "apiVersion: v1\nkind: Secret\nmetadata:\n  name: sonarr-notification\n"
    "stringData:\n  url: ${arr_discord_webhook_url}\n"
)


def test_a_verify_directory_whose_document_substitutes_takes_flux_diff(rig) -> None:
    # #5575: sonarr-verify's Kustomization is the third document of flux/sonarr.yaml.
    rig.tag("sonarr", flux="---\n".join([
        kustomization("sonarr", substitutes=True),
        kustomization("sonarr-snapshot", substitutes=False),
        kustomization("sonarr-verify", substitutes=True),
    ]))  # fmt: skip
    rig.tag("sonarr-verify", {"notification.yaml": SONARR_NOTIFICATION})
    result = rig.run(tags="sonarr-verify")
    assert_accepted(result)
    assert "=== sonarr-verify: flux diff kustomization" in result.stdout
    assert "=== sonarr-verify: kubectl diff" not in result.stdout
    assert "diff kustomization sonarr-verify " in rig.calls()
    own = kustomization_file_flux_read(rig)
    assert "name: sonarr-verify\n" in own
    assert "name: sonarr\n" not in own and "name: sonarr-snapshot\n" not in own


@pytest.mark.parametrize("suffix", ["snapshot", "deferred"])
def test_a_suffixed_directory_whose_document_substitutes_takes_flux_diff(
    rig, suffix
) -> None:
    tag = f"demo-{suffix}"
    rig.tag("demo", flux="---\n".join([
        kustomization("demo", substitutes=False),
        kustomization(tag, substitutes=True),
    ]))  # fmt: skip
    rig.tag(tag)
    result = rig.run(tags=tag)
    assert_accepted(result)
    assert f"=== {tag}: flux diff kustomization" in result.stdout
    assert f"diff kustomization {tag} " in rig.calls()
    assert f"name: {tag}\n" in kustomization_file_flux_read(rig)


def test_a_tag_whose_own_document_lacks_postbuild_takes_kubectl_diff(rig) -> None:
    # The tag's Kustomization is found by name, so a substituting sibling ahead of it in the
    # file does not decide the tag's diff.
    rig.tag("demo", flux="---\n".join([
        kustomization("demo-verify", substitutes=True),
        kustomization("demo", substitutes=False),
    ]))  # fmt: skip
    result = rig.run()
    assert_accepted(result)
    assert "=== demo: kubectl diff" in result.stdout
    assert "flux argv: " not in rig.calls()


@pytest.mark.parametrize("shape", ["directory", "symlink-out-of-home"])
def test_a_verify_tag_whose_base_flux_file_is_not_regular_is_refused(
    rig, shape
) -> None:
    rig.tag("demo")
    rig.tag("demo-verify")
    flux = rig.render / "flux"
    flux.mkdir()
    if shape == "directory":
        (flux / "demo.yaml").mkdir()
    else:
        (flux / "demo.yaml").symlink_to("/etc/hostname")
    result = rig.run(tags="demo-verify")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "=== demo-verify: refused, flux/demo.yaml" in result.stdout
    assert root_calls(rig) == []
    assert "flux argv: " not in rig.calls()
