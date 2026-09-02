"""Guard: no template may build a root securityContext through hardened_security_context().

WHY THIS EXISTS. test_root_needs_dac_capability.py scans raw template TEXT for a `runAsUser: 0`
block that drops ALL capabilities and adds no DAC capability back — root without DAC_OVERRIDE
cannot read or write another uid's files, which makes it WEAKER than the pod's own uid. That
guard does not render Jinja, so a root securityContext expressed as
`{{ hardened_security_context(run_as_user=0, ...) }}` is invisible to it: the guard would find
nothing and pass, which its own docstring records is indistinguishable from a guard that works.

ansible/templates/security-context.yml.j2 therefore documents that it is not for `runAsUser: 0`,
and the fleet's two root sites (code-server, loki-homelab) stay written out in full. This file
is what stops that convention from being a comment nobody reads — it is the executable half.

THE REJECT CASE IS THE EVIDENCE. There are zero violations in the tree today, so the real-tree
assertion passing proves nothing on its own; a rule matching nothing passes identically. The
synthetic cases below are what show the rule can go red.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
        "which hides them from test_root_needs_dac_capability.py — that guard reads raw "
        "template text and cannot see through a macro. Write the securityContext out in full "
        "at the call site, as code-server and loki-homelab do:\n  "
        + "\n  ".join(offenders)
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


def test_the_two_root_sites_are_still_written_out_in_full() -> None:
    """The fleet's `runAsUser: 0` sites must keep the literal text the DAC guard reads.

    Without this, converting them to the macro would leave BOTH guards matching nothing — this
    one because no macro call passes 0, and the DAC one because no literal block remains.
    """
    for rel in (
        "code-server/templates/deployment.yaml.j2",
        "loki-homelab/templates/promtail-daemonset.yaml.j2",
    ):
        text = (K8S_ROLES / rel).read_text()
        assert re.search(r"^\s*runAsUser:\s*0\s*$", text, re.MULTILINE), (
            f"{rel} no longer contains a literal `runAsUser: 0` block. It is one of the two "
            "real accept cases test_root_needs_dac_capability.py exercises; if the container "
            "genuinely stopped running as root, drop it from this list."
        )
