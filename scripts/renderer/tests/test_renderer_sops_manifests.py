"""`render_sops_manifests` and the `sops-manifest-entries` check (target slice R2, PR 5).

A placeholder render leaves out each file a role lists in `render_sops_manifests`, and the
check holds every entry to one Secret of `SOPS_MANIFEST_KEYS` keys. The fixtures build a
`PlaceholderRender` directly, with sentinel renders standing in for the dropped files.

Run: uv run pytest scripts/renderer/tests/test_renderer_sops_manifests.py
"""

import pytest

from renderer import homelab_render, shadow_diff
from renderer.renderer_lib.placeholders import (
    PlaceholderRender,
    render_placeholders,
    sentinel,
)
from renderer.renderer_lib.secret_checks import (
    SOPS_MANIFEST_KEYS,
    sops_manifest_entries,
)

HA = "home-assistant"
ENTRY = "google-sa-secret.yaml"
GA = "google_assistant_service_account"


def _secret(*keys: str) -> str:
    data = "".join(f"  k{i}: {sentinel(key)}\n" for i, key in enumerate(keys))
    return (
        f"---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: x\nstringData:\n{data}"
    )


def _check(entries: dict[str, str | None]) -> list[str]:
    render = PlaceholderRender([], {}, {}, [], {"fixture": entries})
    return sops_manifest_entries(render, [GA, "domain", "fixture_token"])


@pytest.fixture(scope="module")
def ha_render():
    return render_placeholders([HA], homelab_render.secret_keys())


def test_home_assistant_leaves_its_service_account_secret_out_is_clean(ha_render):
    (tag,) = ha_render.tags
    assert ENTRY not in tag.files
    assert ENTRY not in tag.files["kustomization.yaml"]
    assert "secret.yaml" in tag.files["kustomization.yaml"]
    assert GA not in ha_render.key_sets[HA]
    assert GA not in ha_render.standalone[HA]
    assert sops_manifest_entries(ha_render, homelab_render.secret_keys()) == []
    # The named member: the check must have read HA's entry, not an empty list.
    assert sentinel(GA) in (ha_render.sops_manifests[HA][ENTRY] or "")


def test_a_stub_render_keeps_the_entry(tmp_path):
    stubs = tmp_path / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(stubs)]) == 0
    out = tmp_path / "out"
    args = ["cluster", "--out", str(out), "--role", HA, "--secret-stubs", str(stubs)]
    assert homelab_render.main(args) == 0
    assert (out / "cluster" / HA / ENTRY).is_file()


def test_an_entry_of_one_secret_of_a_sops_manifest_key_is_clean():
    assert GA in SOPS_MANIFEST_KEYS
    assert _check({"a-secret.yaml": _secret(GA)}) == []


def test_an_entry_naming_an_ordinary_secret_is_flagged():
    problems = _check({"secret.yaml": _secret("fixture_token")})
    assert problems == [
        "sops-manifest-entries: fixture/secret.yaml names ['fixture_token'], which are "
        "not in SOPS_MANIFEST_KEYS",
        "sops-manifest-entries: fixture/secret.yaml names no key in SOPS_MANIFEST_KEYS",
    ]


def test_an_entry_beside_an_ordinary_key_is_flagged():
    problems = _check({"a-secret.yaml": _secret(GA, "fixture_token")})
    assert problems == [
        "sops-manifest-entries: fixture/a-secret.yaml names ['fixture_token'], which are "
        "not in SOPS_MANIFEST_KEYS"
    ]


def test_an_entry_rendering_two_documents_is_flagged():
    problems = _check({"a-secret.yaml": _secret(GA) + _secret(GA)})
    assert len(problems) == 1
    assert "renders 2 documents of kinds ['Secret', 'Secret']" in problems[0]


def test_an_entry_rendering_a_configmap_is_flagged():
    text = _secret(GA).replace("kind: Secret", "kind: ConfigMap")
    assert "renders 1 documents of kinds ['ConfigMap']" in _check({"a.yaml": text})[0]


def test_an_entry_the_role_renders_no_file_for_is_flagged():
    assert _check({"gone-secret.yaml": None}) == [
        "sops-manifest-entries: fixture/gone-secret.yaml is listed, and the role renders "
        "no such file"
    ]


def test_a_placeholder_render_with_a_wrong_entry_exits_1(tmp_path, capsys):
    def render(tags, keys):
        return render_placeholders(
            tags, keys, entries=lambda tag: [ENTRY, "secret.yaml"]
        )

    out = tmp_path / "out"
    assert homelab_render.cluster(out, [HA], None, render) == 1
    assert (
        "sops-manifest-entries: home-assistant/secret.yaml" in capsys.readouterr().err
    )
    assert not out.exists()
