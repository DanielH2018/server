"""`render_sops_manifests` and the `sops-manifest-entries` check (target slice R2, PRs 5 to 7).

A placeholder render leaves out each file a role lists in `render_sops_manifests`, and the
check holds every entry to one Secret of `SOPS_MANIFEST_KEYS` keys. The fixtures build a
`PlaceholderRender` directly, with sentinel renders standing in for the dropped files. The
third clause's fixtures run rule 5 over a template under `tmp_path`.

Run: uv run pytest scripts/renderer/tests/test_renderer_sops_manifests.py
"""

import pytest

from lib import yaml_fast
from renderer import homelab_render, shadow_diff
from renderer.renderer_lib.bundle import KUSTOMIZATION, SUBSTITUTE_OPT_OUT
from renderer.renderer_lib.cluster import RenderedTag, select_tags
from renderer.renderer_lib.placeholders import (
    PlaceholderRender,
    render_placeholders,
    sentinel,
    sops_manifests,
)
from renderer.renderer_lib.secret_checks import (
    SOPS_MANIFEST_KEYS,
    check,
    rule5,
    sops_entry_templates,
    sops_manifest_entries,
    sops_manifest_namers,
)
from renderer.renderer_lib.sops_entries import copy_sops_manifests
from secrets_mgmt.cluster_vars_check import ManifestEntry

HA = "home-assistant"
ENTRY = "google-sa-secret.yaml"
GA = "google_assistant_service_account"

# Each role's one entry and the SOPS_MANIFEST_KEYS key its Secret holds.
ENTRIES = {
    HA: (ENTRY, GA),
    "pi-peer-backup": ("ssh-key-secret.yaml", "pi_peer_backup_ssh_key"),
    "qbittorrent": ("mullvad-account-secret.yaml", "mullvad_account"),
    "authelia": ("oidc-jwk-secret.yaml", "authelia_oidc_rsa_key_content"),
}


def _secret(*keys: str) -> str:
    data = "".join(f"  k{i}: {sentinel(key)}\n" for i, key in enumerate(keys))
    return (
        f"---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: x\nstringData:\n{data}"
    )


def _check(entries: dict[str, str | None]) -> list[str]:
    render = PlaceholderRender([], {}, {}, {"fixture": entries})
    return sops_manifest_entries(render, [GA, "domain", "fixture_token"])


def test_the_real_tree_holds_four_entries_is_clean():
    entries = {tag: sops_manifests(tag) for tag in select_tags(None)}
    assert {tag: names for tag, names in entries.items() if names} == {
        role: [entry] for role, (entry, _key) in ENTRIES.items()
    }


@pytest.mark.parametrize("role", sorted(ENTRIES))
def test_a_role_leaves_its_sops_manifest_secret_out_is_clean(role):
    entry, key = ENTRIES[role]
    render = render_placeholders([role], homelab_render.secret_keys())
    (tag,) = render.tags
    assert entry not in tag.files
    assert entry not in tag.files["kustomization.yaml"]
    assert "secret.yaml" in tag.files["kustomization.yaml"]
    assert key not in render.key_sets[role]
    assert key not in render.standalone[role]
    assert sops_manifest_entries(render, homelab_render.secret_keys()) == []
    # The named member: the check must have read the role's entry, not an empty list.
    assert sentinel(key) in (render.sops_manifests[role][entry] or "")


def test_a_stub_render_keeps_the_entries(tmp_path):
    stubs = tmp_path / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(stubs)]) == 0
    out = tmp_path / "out"
    roles = [arg for role in sorted(ENTRIES) for arg in ("--role", role)]
    args = ["cluster", "--out", str(out), *roles, "--secret-stubs", str(stubs)]
    assert homelab_render.main(args) == 0
    for role, (entry, _key) in ENTRIES.items():
        assert (out / "cluster" / role / entry).is_file()


def test_the_live_qbittorrent_kustomization_renders_decryption(tmp_path):
    """The named member D5's qbittorrent flip requires: its Kustomization decrypts its entry.

    Rendered with the live inventory's ownership, so the tag publishes a Kustomization only
    once qbittorrent carries `owner: flux`, and `bundle.py` adds `decryption` to it because
    the role lists `render_sops_manifests`.
    """
    out = tmp_path / "out"
    assert homelab_render.cluster(out, ["qbittorrent"], None) == 0
    entry, _key = ENTRIES["qbittorrent"]
    sops_copy = f"qbittorrent.{entry.removesuffix('.yaml')}.sops.yaml"
    assert (out / "cluster" / "qbittorrent" / sops_copy).is_file()
    flux = out / "flux" / "qbittorrent.yaml"
    docs = {d["metadata"]["name"]: d for d in yaml_fast.safe_load_all(flux.read_text())}
    assert docs["qbittorrent"]["spec"]["decryption"] == {
        "provider": "sops",
        "secretRef": {"name": "sops-age"},
    }
    # Only the tag whose directory holds the SOPS copy decrypts.
    assert "decryption" not in docs["qbittorrent-verify"]["spec"]


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


# -- the third clause: a template outside the lists ------------------------------------------

MULLVAD = "mullvad_account"


def _unlisted(template: str, key: str) -> str:
    return (
        f"sops-manifest-entries: {template} names {key} and is in no role's "
        "render_sops_manifests"
    )


def test_the_real_tree_names_each_key_only_in_an_entry_is_clean():
    # The clause is an equality: the namers are exactly the four entries' templates.
    result = rule5(homelab_render.secret_keys())
    entry_templates = sorted(
        f"ansible/roles/k8s/{role}/templates/{entry}.j2"
        for role, (entry, _key) in ENTRIES.items()
    )
    namers = sorted(template for _tag, template, _key in result.sops_namers)
    assert namers == entry_templates
    assert sorted(sops_entry_templates()) == entry_templates
    assert sops_manifest_namers(result) == []


def _rule5_over(tmp_path, source: str):
    template = tmp_path / "t.yaml.j2"
    template.write_text(source)
    return rule5([MULLVAD, "fixture_token"], [template], frozenset(), root=tmp_path)


def test_a_listed_template_naming_a_sops_manifest_key_is_clean(tmp_path):
    result = _rule5_over(tmp_path, "a: {{ mullvad_account }}\n")
    assert sops_manifest_namers(result, ["t.yaml.j2"]) == []


def test_an_unlisted_template_naming_a_sops_manifest_key_is_flagged(tmp_path):
    result = _rule5_over(tmp_path, "a: {{ mullvad_account }}\n")
    assert result.violations == []
    assert sops_manifest_namers(result, []) == [_unlisted("t.yaml.j2", MULLVAD)]


def test_an_entry_whose_template_names_no_sops_manifest_key_is_flagged(tmp_path):
    result = _rule5_over(tmp_path, "a: {{ fixture_token }}\n")
    assert sops_manifest_namers(result, ["t.yaml.j2"]) == [
        "sops-manifest-entries: t.yaml.j2 is a render_sops_manifests entry and names no "
        "key in SOPS_MANIFEST_KEYS"
    ]


def test_the_secret_check_runs_the_third_clause_whatever_the_render_is_flagged(
    tmp_path,
):
    result = _rule5_over(tmp_path, "a: {{ mullvad_account }}\n")
    render = PlaceholderRender([], {}, {}, {})
    report = check(render, homelab_render.secret_keys(), rule5_result=result, listed=[])
    assert _unlisted("t.yaml.j2", MULLVAD) in report.missing


def test_copying_a_sops_manifest_keeps_the_tags_verify_directory(tmp_path):
    """The copy rebuilds `<tag>/` alone, so `-verify` and `-snapshot` survive it."""
    name = ManifestEntry(HA, ENTRY, "", frozenset()).name
    (tmp_path / name).write_text(
        f"kind: Secret\nmetadata:\n  annotations:\n    {SUBSTITUTE_OPT_OUT}: disabled\n"
    )
    verify = {"job.yaml": "kind: Job\n", KUSTOMIZATION: "resources: [job.yaml]\n"}
    tag = RenderedTag(HA, {KUSTOMIZATION: "resources: []\n"}, verify=verify)
    (copied,) = copy_sops_manifests([tag], {HA: {ENTRY: None}}, [HA], tmp_path)
    assert name in copied.files
    assert copied.verify == verify
