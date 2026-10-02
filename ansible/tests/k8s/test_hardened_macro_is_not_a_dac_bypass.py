"""Guard: no template may build a root securityContext through hardened_security_context().

WHY THIS EXISTS. ansible/templates/security-context.yml.j2 documents that it is not for
`runAsUser: 0`, and the fleet's six root sites (code-server, loki-homelab, and the four CrowdSec
seeding init containers in authelia and traefik) stay written out in full. This file is what
stops that convention from being a comment nobody reads — it is the executable half.

The convention exists because each root site needs a DAC capability for a reason only that
container's own comment can state: which uid owns the files, at what mode, and why root is
reached for at all. A one-line macro call has nowhere to put that, and the capability list is
written literally at every call site so a grep for `DAC_OVERRIDE` still finds every container
granted it.

This guard is NOT what makes test_root_needs_dac_capability.py able to see a root block. That
guard reads each template as the deploy renders it (#3223), so a macro-built securityContext
arrives expanded and is scanned like any other. Retiring this pin is #3228.

THE REJECT CASE IS THE EVIDENCE. There are zero violations in the tree today, so the real-tree
assertion passing proves nothing on its own; a rule matching nothing passes identically. The
synthetic cases below are what show the rule can go red.
"""

import re
from pathlib import Path


from _helpers import K8S_ROLES, REPO

# `run_as_user=0` anywhere in a hardened_security_context() call, tolerant of whitespace and of
# whichever other arguments sit around it. Deliberately textual, matching the guard it protects.
_ROOT_CALL = re.compile(
    r"hardened_security_context\s*\([^)]*\brun_as_user\s*=\s*0\b", re.DOTALL
)


def _manifest_files() -> list[Path]:
    return sorted(
        p for p in K8S_ROLES.rglob("templates/*.j2") if "archive" not in p.parts
    )


def root_via_macro(text: str) -> list[int]:
    """Line numbers of hardened_security_context() calls that pass run_as_user=0."""
    return [text[: m.start()].count("\n") + 1 for m in _ROOT_CALL.finditer(text)]


def test_no_template_builds_a_root_context_through_the_macro() -> None:
    """The real tree. See the module docstring on what this passing does and does not prove."""
    offenders = []
    for path in _manifest_files():
        for line_no in root_via_macro(path.read_text()):
            offenders.append(f"{path.relative_to(REPO)}:{line_no}")

    assert not offenders, (
        "these templates build a root securityContext through hardened_security_context(), "
        "which leaves no room for the comment saying which uid owns the files and why root is "
        "reached for. Write the securityContext out in full at the call site, as code-server "
        "and loki-homelab do:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_matches_a_root_call() -> None:
    """Reject case: the rule fires on the shape it exists to catch."""
    assert root_via_macro(
        "{{ hardened_security_context(run_as_user=0, add=['DAC_OVERRIDE']) }}"
    ) == [1]
    assert root_via_macro(
        "spec:\n{{ hardened_security_context(non_root=false, run_as_user=0) }}"
    ) == [2]


def test_the_guard_ignores_a_non_root_call() -> None:
    """Accept case: an ordinary call, and a uid that merely starts with 0, must not match."""
    assert root_via_macro("{{ hardened_security_context(read_only=true) }}") == []
    assert root_via_macro("{{ hardened_security_context(run_as_user=1000) }}") == []
    assert root_via_macro("{{ hardened_security_context(run_as_user=65534) }}") == []


# Each template holding a root site, with how many literal `runAsUser: 0` blocks it carries.
# authelia and traefik each hold the crowdsec-hub-install and crowdsec-data-install pair.
_ROOT_SITES = {
    "authelia/templates/deployment.yaml.j2": 2,
    "code-server/templates/deployment.yaml.j2": 1,
    "loki-homelab/templates/alloy-daemonset.yaml.j2": 1,
    "traefik/templates/deployment.yaml.j2": 2,
}


def test_every_root_site_is_still_written_out_in_full() -> None:
    """The fleet's `runAsUser: 0` sites must keep their literal blocks.

    A count rather than a presence check, so one container's conversion to the macro fails here
    instead of hiding behind its siblings. The DAC census counts the same six off the render in
    `test_every_documented_root_site_still_renders_a_clean_root_block`, which is why retiring
    this half is #3228 — the two now overlap.
    """
    for rel, expected in _ROOT_SITES.items():
        text = (K8S_ROLES / rel).read_text()
        found = len(re.findall(r"^\s*runAsUser:\s*0\s*$", text, re.MULTILINE))
        assert found == expected, (
            f"{rel} holds {found} literal `runAsUser: 0` block(s), not {expected}. Each is a "
            "real accept case test_root_needs_dac_capability.py exercises on the render; if a container "
            "genuinely stopped running as root, lower its count in _ROOT_SITES."
        )
