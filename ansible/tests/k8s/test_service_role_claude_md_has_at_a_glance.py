"""Every deployed k8s service role's CLAUDE.md carries a `## At a glance` section.

The section is the fixed-shape summary a session reads before touching a service — deploy
tag, route, claims, autodeploy stance — and `test_k8s_roles_have_claude_md.py` only asks that
a doc exist. 52 of the roles in `containers_list` carried the section on 2026-09-17 and five
ordinary services did not (authelia, crowdsec, qbittorrent, uptime-kuma, claude-otel), each
with the same facts scattered across a bold line and two later sections. Those five gained
one in the change that added this guard.

Scope is the deployed services: the k8s `containers_list` entries. A helper role with no entry
(manifests, cronjob-gate, volume-claim, ...) is documented from its callers' side.

Run: uv run pytest ansible/tests/k8s/test_service_role_claude_md_has_at_a_glance.py
"""

import sys

from _helpers import K8S_ROLES, REPO

sys.path.insert(0, str(REPO / "scripts"))

from lib.k8s_roles import k8s_entries

HEADING = "## At a glance"

# Deployed, but documented at a different shape on purpose. Each is a few paragraphs about
# one fact, and a glance table would restate the paragraph.
EXEMPT = frozenset(
    {
        "deploy-ui",  # route-only: a Service and an IngressRoute onto a host systemd unit
        "gpu-exporter",  # a DaemonSet exporter with no route, claim or autodeploy choice
        "nut-exporter",  # same shape, scraped by prometheus and reachable by nothing else
        "pihole-exporter",  # same shape; its doc is about which instance is scraped
    }
)

KNOWN_SERVICES = frozenset({"sonarr", "traefik", "authelia", "home-assistant"})


def missing_glance(names, roles_dir=K8S_ROLES) -> list[str]:
    return sorted(
        name
        for name in names
        if name not in EXEMPT and HEADING not in _doc_text(roles_dir / name)
    )


def test_every_deployed_service_role_opens_with_at_a_glance():
    names = set(k8s_entries())
    assert KNOWN_SERVICES <= names
    assert missing_glance(names) == []


def test_every_exemption_is_still_a_deployed_role():
    """A retired role must leave this list too, or the list grows names nobody can justify."""
    assert EXEMPT <= set(k8s_entries()), sorted(EXEMPT - set(k8s_entries()))


def test_a_doc_without_the_heading_is_flagged(tmp_path):
    for name, body in (
        ("with", f"# with\n\n{HEADING}\n- x\n"),
        ("without", "# without\n\n## Traps\n"),
    ):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text(body)
    assert missing_glance(["with", "without"], tmp_path) == ["without"]


def _doc_text(role_dir) -> str:
    doc = role_dir / "CLAUDE.md"
    # A missing doc is test_k8s_roles_have_claude_md.py's finding; here it reads as "no heading".
    return doc.read_text() if doc.is_file() else ""
