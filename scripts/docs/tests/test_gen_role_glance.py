"""Tests for the role "At a glance" generator: the facts, the in-place writer, and the gate.

The gate is the one that matters day to day: `test_every_deployed_role_block_matches_what_the_
generator_writes_now` fails the moment a role's defaults, templates or `containers_list` entry
move without the block being regenerated, which is the drift #2058 was filed on.
"""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gen_role_glance as g

KNOWN_SERVICES = frozenset(
    {"sonarr", "traefik", "authelia", "home-assistant", "pihole"}
)


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
    # `k8s_service_entries` filters `containers_list` by platform; a filter that stopped
    # matching would make the gate above pass on nothing.
    names = {e["name"] for e in g.k8s_service_entries()}
    assert KNOWN_SERVICES <= names, sorted(KNOWN_SERVICES - names)


def test_a_hand_edited_block_is_flagged(tmp_path):
    """The gate's rejecting half: a copy of sonarr is clean, and one changed value makes it stale."""
    entry = next(e for e in g.k8s_service_entries() if e["name"] == "sonarr")
    src = g.K8S_ROLES / "sonarr"
    dst = tmp_path / "roles" / "sonarr"
    (dst / "defaults").mkdir(parents=True)
    (dst / "defaults" / "main.yml").write_text(
        (src / "defaults" / "main.yml").read_text()
    )
    (dst / "templates").mkdir()
    for tmpl in (src / "templates").glob("*.j2"):
        (dst / "templates" / tmpl.name).write_text(tmpl.read_text())
    (dst / "CLAUDE.md").write_text((src / "CLAUDE.md").read_text())
    host_vars = tmp_path / "daniel-box.yml"
    host_vars.write_text(yaml.safe_dump({"containers_list": [entry]}))
    roles = dst.parent
    assert g.stale_docs(write=False, host_vars=host_vars, k8s_roles=roles) == []

    doc = (dst / "CLAUDE.md").read_text()
    assert '`--tags "sonarr"`' in doc
    (dst / "CLAUDE.md").write_text(
        doc.replace('`--tags "sonarr"`', '`--tags "sonar"`', 1)
    )
    assert g.stale_docs(write=False, host_vars=host_vars, k8s_roles=roles) == ["sonarr"]
