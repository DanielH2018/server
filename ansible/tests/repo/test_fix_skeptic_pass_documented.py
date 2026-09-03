"""The fix-skeptic pass stays documented in the homelab-review skill.

Recurring-failure class 1 (docs/failure-classes.md): "findings get an adversarial pass, fixes
get none." Its remedy is a review PROCEDURE — a reviewer dispatching a `skeptic` agent against
every recommended remediation, per `.claude/skills/homelab-review/SKILL.md` step 7 — not a
property of any file this repo ships. No test can verify a future review session actually ran
that pass; whether the adversarial rigor was genuinely applied to a given PR's fixes is a
human judgment call, made by whoever reads that session's report.

What a test CAN verify is that the instruction to run the pass has not quietly regressed out of
the skill — the same failure mode `test_ci_cancelled_is_not_a_verdict.py` guards for the
CLAUDE.md paragraph on CI conclusions. This is a proxy-citation guard, and it knows it: a green
result here means the skill still SAYS to run the fix-skeptic pass, never that a session did.

Run: uv run pytest ansible/tests/repo/test_fix_skeptic_pass_documented.py
"""

from _helpers import REPO

SKILL = REPO / ".claude" / "skills" / "homelab-review" / "SKILL.md"

REQUIRED_MARKERS = ("fix-skeptic", "skeptic", "SAFE", "LAUNDERS", "UNSAFE")


def _missing_markers(text: str) -> list[str]:
    """The fix-skeptic pass's required vocabulary that `text` does not carry."""
    return [marker for marker in REQUIRED_MARKERS if marker not in text]


def test_the_skill_file_exists():
    """Without this, the assertions below pass vacuously on a missing/empty file."""
    assert SKILL.is_file()
    assert len(SKILL.read_text(errors="replace")) > 1000


def test_the_fix_skeptic_pass_is_still_instructed():
    missing = _missing_markers(SKILL.read_text(errors="replace"))
    assert not missing, (
        f"SKILL.md is missing {missing} from the fix-skeptic pass. Recurring-failure class 1 "
        f"exists because findings got an adversarial pass and fixes got none — removing this "
        f"instruction reopens exactly that gap."
    )


def test_the_predicate_flags_a_skill_with_the_pass_stripped_and_clears_the_real_one():
    """Red-proof pair for `_missing_markers` itself."""
    stripped = (
        SKILL.read_text(errors="replace").replace("fix-skeptic", "").replace("SAFE", "")
    )
    assert _missing_markers(stripped)
    assert not _missing_markers(SKILL.read_text(errors="replace"))
