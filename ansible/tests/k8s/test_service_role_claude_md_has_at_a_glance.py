"""Every deployed k8s service role's CLAUDE.md opens `## At a glance` with the generated block.

The section is the fixed-shape summary a session reads before touching a service. Until
2026-09-19 every line of it was hand-typed and this guard checked only that the heading
existed, so a doc could pin an image its defaults no longer pinned (#2058). The mechanical
half — deploy tag, images, route, claims, auto-deploy stance — is now written by
`scripts/docs/gen_role_glance.py` between two `generated_from` markers directly under the
heading, and this guard asserts that structure for every deployed role. Whether the block's
CONTENT matches the tree is `scripts/docs/tests/test_gen_role_glance.py`'s gate; keeping the
two apart means a missing heading and a stale value fail with different messages.

Scope is the deployed services: the k8s `containers_list` entries. A helper role with no entry
(manifests, cronjob-gate, volume-claim, ...) is documented from its callers' side. The four
exporter/route-only roles this guard used to exempt carry the generated block like every
other — there is nothing to hand-restate any more, so nothing to exempt.

Run: uv run pytest ansible/tests/k8s/test_service_role_claude_md_has_at_a_glance.py
"""

from _helpers import K8S_ROLES


from lib.k8s_roles import k8s_entries

HEADING = "## At a glance"
BANNER = "<!-- generated_from: scripts/docs/gen_role_glance.py"

KNOWN_SERVICES = frozenset(
    {"sonarr", "traefik", "authelia", "home-assistant", "gpu-exporter"}
)


def missing_glance(names, roles_dir=K8S_ROLES) -> list[str]:
    """Roles whose doc lacks the heading, or whose heading is not followed by the banner."""
    return sorted(
        name for name in names if not _opens_with_generated_block(roles_dir / name)
    )


def _opens_with_generated_block(role_dir) -> bool:
    doc = role_dir / "CLAUDE.md"
    # A missing doc is test_k8s_roles_have_claude_md.py's finding; here it reads as "no heading".
    text = doc.read_text() if doc.is_file() else ""
    lines = text.split("\n")
    at = next((i for i, line in enumerate(lines) if line.rstrip() == HEADING), None)
    return at is not None and at + 1 < len(lines) and lines[at + 1].startswith(BANNER)


def test_every_deployed_service_role_opens_with_the_generated_glance_block():
    names = set(k8s_entries())
    assert KNOWN_SERVICES <= names
    assert missing_glance(names) == []


def test_a_doc_without_the_heading_or_the_banner_is_flagged(tmp_path):
    for name, body in (
        ("with", f"# with\n\n{HEADING}\n{BANNER} -->\n- x\n<!-- /generated_from -->\n"),
        ("no-heading", "# no-heading\n\n## Traps\n"),
        ("hand-typed", f"# hand-typed\n\n{HEADING}\n- **Image:** `x:latest`\n"),
    ):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text(body)
    assert missing_glance(["with", "no-heading", "hand-typed"], tmp_path) == [
        "hand-typed",
        "no-heading",
    ]
