"""This role's CLAUDE.md enumerates `PROM_DEPENDENT`, and the enumeration must stay true.

`test_check_gate_dependents.py` pins the SET to the live registry, so the set itself cannot
drift. The prose describing it could, and did: the paragraph said "the ten prom-dependent
checks" and listed ten of them while the set held fourteen (#1359). Nothing reads the prose to
decide anything, so nothing failed — a reader just underestimated what a Prometheus outage
suppresses.

A fragment under `docs/assets/generated/fragments/` is the repo's usual way to stop prose
drifting, but a `--8<--` include only resolves in the mkdocs build. A role CLAUDE.md is read raw
by whoever opens it, so an include there would render as its own literal text. A test that reads
the sentence and compares it to the set is the durable rung available here.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_claude_md_prom_dependent_enumeration.py
"""

import re
from pathlib import Path

import gates

CLAUDE_MD = Path(__file__).resolve().parents[1] / "CLAUDE.md"

# The parenthesised slash-separated list in the Prometheus Reachable bullet. `[a-z0-9_]`, not
# `[a-z]`: traefik5xx, traefik_404 and kubelet_plugin_readonly all carry a digit or an
# underscore, and a narrower class would drop them and leave a comparison that passes on less.
ENUMERATION = re.compile(
    r"every\s+\n?\s*prom-dependent check\s*\(\s*([a-z0-9_/\s]+?)\s*\)", re.MULTILINE
)


def enumerated_names(text: str) -> set[str]:
    match = ENUMERATION.search(text)
    assert match, (
        "no prom-dependent enumeration found in CLAUDE.md — the sentence was reworded past "
        "this guard's regex, which then compares an empty set and passes on nothing"
    )
    return {name for name in re.split(r"[/\s]+", match.group(1)) if name}


def test_claude_md_enumerates_exactly_prom_dependent():
    named = enumerated_names(CLAUDE_MD.read_text())
    # Non-vacuity, before the set comparison: an empty or one-name match would otherwise only
    # surface as a confusing diff.
    assert len(named) >= 10, (
        f"enumeration matched too little to be the real list: {named}"
    )
    assert named == set(gates.PROM_DEPENDENT), (
        "CLAUDE.md's prom-dependent enumeration is stale.\n"
        f"  missing from the prose: {sorted(set(gates.PROM_DEPENDENT) - named)}\n"
        f"  named but not in PROM_DEPENDENT: {sorted(named - set(gates.PROM_DEPENDENT))}"
    )


def test_a_short_enumeration_is_flagged():
    """The red half: the exact sentence this guard was written against (#1359)."""
    stale = (
        "when Prometheus is unreachable, every\n"
        "    prom-dependent check (disk/cert/memory) is\n"
        "    **suppressed**"
    )
    assert enumerated_names(stale) == {"disk", "cert", "memory"}
    assert enumerated_names(stale) != set(gates.PROM_DEPENDENT)
