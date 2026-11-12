"""Tests for the role "At a glance" generator: the facts, the in-place writer, and the gate.

The gate is the one that matters day to day: `test_every_deployed_role_block_matches_what_the_
generator_writes_now` fails the moment a role's defaults, templates, tasks, playbook entry or
`containers_list` entry move without the block being regenerated. Each shape has its own
red-proof pair over a fixture role under `tmp_path`, and a non-vacuity pin naming members its
census must find.
"""

from pathlib import Path

import pytest
import yaml

import gen_role_glance as g
from lib.estate import Inventory

KNOWN_SERVICES = frozenset(
    {"sonarr", "traefik", "authelia", "home-assistant", "pihole"}
)
# H5.9 moved wg-easy, the last Pi service, into the host package, so no Pi role takes a block.


# --- facts -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "repository"),
    [
        ("lscr.io/linuxserver/sonarr:4.0.19.2979-ls322", "lscr.io/linuxserver/sonarr"),
        (
            "ghcr.io/gethomepage/homepage:latest@sha256:a0b71c8e",
            "ghcr.io/gethomepage/homepage",
        ),
        ("alpine", "alpine"),
        ("localhost:5000/valheim:latest", "localhost:5000/valheim"),
        ("{{ k8s_registry_pull_host }}/n8n:latest", "<k8s_registry_pull_host>/n8n"),
    ],
)
def test_image_repository_drops_the_tag_and_digest(ref, repository):
    assert g.image_repository(ref) == repository


def test_image_vars_reads_the_role_defaults_then_a_hoisted_group_var():
    defaults = {"crowdsec_k8s_lapi_port": 8080, "crowdsec_k8s_bouncer_image": "b:1"}
    group_vars = {
        "crowdsec_k8s_image": "crowdsecurity/crowdsec:v1",
        "traefik_k8s_image": "t:1",
    }
    assert g.image_vars("crowdsec", defaults, group_vars) == [
        ("crowdsec_k8s_bouncer_image", "b:1"),
        ("crowdsec_k8s_image", "crowdsecurity/crowdsec:v1"),
    ]


@pytest.mark.parametrize(
    ("owner", "name", "repository"),
    [
        ("flux", "terraria", "ghcr.io/danielh2018/terraria"),
        (None, "terraria", "<k8s_registry_pull_host>/terraria"),
        # Only a built image moves to GHCR; no role builds this one.
        ("flux", "selftest", "<k8s_registry_pull_host>/selftest"),
    ],
)
def test_pulled_repository_moves_only_a_flux_owned_built_image(owner, name, repository):
    ref = f"{{{{ k8s_registry_pull_host }}}}/{name}:sha-0123456789ab"
    built = frozenset({"terraria"})
    assert g.pulled_repository(ref, owner=owner, built=built) == repository


def test_the_ghcr_prefix_matches_the_renderers():
    from renderer.renderer_lib import images

    assert g.GHCR == images.GHCR


# --- the in-place writer -----------------------------------------------------------------

BLOCK = g.render_block(['- **Deploy tag:** `--tags "x"`'])
DOC = (
    "# x\n\nIntro.\n\n## At a glance\n- **Why:** hand-written reasoning.\n\n## Traps\n"
)


def test_render_doc_inserts_the_block_under_the_heading_and_keeps_the_prose():
    out = g.render_doc(DOC, BLOCK)
    assert out == (
        "# x\n\nIntro.\n\n## At a glance\n" + BLOCK + "\n"
        "- **Why:** hand-written reasoning.\n\n## Traps\n"
    )


def test_render_doc_replaces_a_stale_block_and_is_idempotent():
    first = g.render_doc(DOC, g.render_block(['- **Deploy tag:** `--tags "old"`']))
    second = g.render_doc(first, BLOCK)
    assert second == g.render_doc(DOC, BLOCK)
    assert g.render_doc(second, BLOCK) == second


def test_render_doc_refuses_a_doc_without_the_heading():
    with pytest.raises(g.MissingHeading):
        g.render_doc("# x\n\n## Traps\n", BLOCK)


def test_render_doc_refuses_an_unterminated_block():
    broken = "## At a glance\n" + g.BEGIN + "\n- x\n\n## Traps\n"
    with pytest.raises(ValueError, match="no `<!-- /generated_from -->`"):
        g.render_doc(broken, BLOCK)


def test_render_block_wraps_long_bullets_with_a_two_space_hang():
    block = g.render_block(["- **Auto-deploy:** " + "word " * 40])
    body = block.splitlines()[1:-1]
    assert len(body) > 1
    assert all(len(line) <= g.WIDTH for line in body)
    assert all(line.startswith("  ") for line in body[1:])


# --- the gate ----------------------------------------------------------------------------


def test_every_deployed_role_block_matches_what_the_generator_writes_now():
    """A committed block that differs from a fresh render is a hand edit or a moved source."""
    stale = g.stale_docs(write=False)
    assert stale == [], (
        f"stale At a glance block in {stale}; run `uv run python {g.SELF}` and commit"
    )


def test_the_gate_covers_the_known_services():
    # `k8s_entries` filters `containers_list` by platform; a filter that stopped
    # matching would make the gate above pass on nothing.
    names = {e["name"] for e in g.Estate().k8s_entries().values()}
    assert KNOWN_SERVICES <= names, sorted(KNOWN_SERVICES - names)


def _copy_role(name: str, roles: Path) -> Path:
    """`roles/<name>` with the real role's defaults, templates, tasks and doc copied in."""
    src = g.K8S_ROLES / name
    dst = roles / name
    for sub in ("defaults", "templates", "tasks"):
        if not (src / sub).is_dir():
            continue
        (dst / sub).mkdir(parents=True)
        # Files only: a subdirectory such as sonarr's `templates/verify-inert/` is no input
        # the glance reads.
        for path in (src / sub).glob("*"):
            if path.is_file():
                (dst / sub / path.name).write_text(path.read_text())
    if (src / "CLAUDE.md").is_file():
        (dst / "CLAUDE.md").write_text((src / "CLAUDE.md").read_text())
    return dst


def test_a_hand_edited_block_is_flagged(tmp_path):
    """The gate's rejecting half: a copy of sonarr is clean, and one changed value makes it stale."""
    entry = g.Estate().k8s_entries()["sonarr"]
    dst = _copy_role("sonarr", tmp_path / "roles")
    # sonarr's claims resolve through two other roles: volume-claim's default StorageClass
    # names `sonarr-config`'s class, and media-volume declares the `media-data` it mounts.
    _copy_role("volume-claim", tmp_path / "roles")
    _copy_role("media-volume", tmp_path / "roles")
    (tmp_path / "daniel-box.yml").write_text(
        yaml.safe_dump({"containers_list": [entry]})
    )
    estate = g.Estate(Inventory(host_vars=tmp_path))
    roles = dst.parent
    assert g.stale_k8s_docs(write=False, estate=estate, k8s_roles=roles) == []

    doc = (dst / "CLAUDE.md").read_text()
    assert "`land.sh --tags=sonarr`" in doc
    (dst / "CLAUDE.md").write_text(
        doc.replace("`land.sh --tags=sonarr`", "`land.sh --tags=sonar`", 1)
    )
    assert g.stale_k8s_docs(write=False, estate=estate, k8s_roles=roles) == [
        "k8s/sonarr"
    ]


# An entry that carries `owner` is not Ansible's to deploy (D2.4b): the deploy tag and the
# stance would both mislead, so one Applied by line names what applies it.


def _littlelink_block(tmp_path):
    """The block `stale_k8s_docs` writes for a copy of littlelink, its entry without `owner`."""
    entry = dict(g.Estate().k8s_entries()["littlelink"])
    entry.pop("owner", None)
    dst = _copy_role("littlelink", tmp_path / "roles")
    (tmp_path / "daniel-box.yml").write_text(
        yaml.safe_dump({"containers_list": [entry]})
    )
    estate = g.Estate(Inventory(host_vars=tmp_path))
    g.stale_k8s_docs(write=True, estate=estate, k8s_roles=dst.parent)
    return (dst / "CLAUDE.md").read_text().split("<!-- /generated_from -->")[0]


def test_a_k8s_role_glance_names_flux_in_place_of_the_deploy_tag(tmp_path):
    """No `owner` key marks the entry: `platform: k8s` alone makes it Flux's (H1.9)."""
    block = _littlelink_block(tmp_path)
    assert "- **Applied by:** Flux, through `promote`" in block
    # The block is wrapped, so match the gate across a line break.
    unwrapped = " ".join(block.split())
    assert "`land.sh --tags=littlelink` gates the rollout" in unwrapped
    assert "Deploy tag" not in block and "Auto-deploy" not in block
