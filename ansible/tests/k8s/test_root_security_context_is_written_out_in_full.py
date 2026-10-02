"""Convention guard: a root securityContext is written out in full, never built by the macro.

WHY THIS EXISTS. `ansible/templates/security-context.yml.j2` documents that it is not for
`runAsUser: 0`, and the fleet's six root sites (code-server, loki-homelab, and the four CrowdSec
seeding init containers in authelia and traefik) stay written out in full. This file is what
stops that convention from being a comment nobody reads — it is the executable half.

The convention is about READABILITY, not about what the cluster admits. Each root site needs a
DAC capability for a reason only that container's own comment can state: which uid owns the
files, at what mode, and why root is reached for at all. A one-line macro call has nowhere to
put that, and the capability list is written literally at every call site so a grep for
`DAC_OVERRIDE` still finds every container granted it.

THIS IS NOT THE DAC CENSUS'S EYES. `test_root_needs_dac_capability.py` reads each template as
the deploy renders it (#3223), so a macro-built securityContext arrives expanded and is scanned
like any other, and its accept coverage has its own render-side non-vacuity pin
(`test_every_documented_root_site_still_renders_a_clean_root_block`). This file once also pinned
the six root sites to literal `runAsUser: 0` blocks, which was that census's non-vacuity half
before the census had its own; #3228 retired that pin as duplicated.

`test_container_security_context_uses_the_macro.py` holds the mirror of the rule below: every
hand-written securityContext at pod-template depth must BE a root block. Together the two say a
root block is written out and nothing else is.

THE REJECT CASE IS THE EVIDENCE. There are zero violations in the tree today, so the real-tree
assertion passing proves nothing on its own; a rule matching nothing passes identically. The
synthetic cases below are what show the rule can go red, and
`test_the_scanned_corpus_still_holds_the_macro_call_sites` is what shows the glob still has a
corpus to scan.

Run: uv run pytest ansible/tests/k8s/test_root_security_context_is_written_out_in_full.py
"""

import re
from pathlib import Path


from _helpers import K8S_ROLES, REPO

# `run_as_user=0` anywhere in a hardened_security_context() call, tolerant of whitespace and of
# whichever other arguments sit around it. Deliberately textual, matching the guard it protects.
_ROOT_CALL = re.compile(
    r"hardened_security_context\s*\([^)]*\brun_as_user\s*=\s*0\b", re.DOTALL
)

_MACRO_CALL = re.compile(r"hardened_security_context\s*\(")

# Templates the glob must still reach: the four holding a root site, which are the conversions
# this rule exists to refuse, and two ordinary macro callers. A census that stopped matching
# returns an empty corpus, and a scan over nothing passes.
_KNOWN_MEMBERS = frozenset(
    {
        "authelia/templates/deployment.yaml.j2",
        "code-server/templates/deployment.yaml.j2",
        "loki-homelab/templates/alloy-daemonset.yaml.j2",
        "traefik/templates/deployment.yaml.j2",
    }
)

# A floor, not a count: the fleet had 115 macro calls across 76 templates on 2026-10-02, and the
# number moves with every new workload. Well under it means the glob or the call shape moved.
_MACRO_CALL_SITE_FLOOR = 50


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


def test_the_scanned_corpus_still_holds_the_macro_call_sites() -> None:
    """Non-vacuity: the glob reaches the root sites, and the fleet still calls the macro.

    The rule above expects zero matches, so an empty or narrowed corpus reads exactly like a
    clean tree. This names the four templates a conversion would most likely happen in, and
    floors the number of call sites the rule has to scan.
    """
    corpus = {str(p.relative_to(K8S_ROLES)) for p in _manifest_files()}
    missing = sorted(_KNOWN_MEMBERS - corpus)
    assert not missing, (
        "the template glob no longer reaches these root-site templates, so a macro call added "
        f"in one would not be seen: {missing}"
    )

    call_sites = sum(
        len(_MACRO_CALL.findall(path.read_text())) for path in _manifest_files()
    )
    assert call_sites >= _MACRO_CALL_SITE_FLOOR, (
        f"only {call_sites} hardened_security_context() call sites in the corpus, under the "
        f"floor of {_MACRO_CALL_SITE_FLOOR}. Either the macro was renamed or the glob moved — "
        "a rule about macro calls over a corpus with none passes without checking anything"
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
