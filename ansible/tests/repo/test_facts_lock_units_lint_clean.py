"""A section that is in docs/facts.lock stays lint-clean: no file:line, every atom resolving.

The ratchet for sections NOT yet in the lock is `fact_status.py lint --changed-since`, run by
the prek hook where origin/master exists. CI cannot rely on that ref, so this guard holds the
converted set and the hook holds the frontier.
"""

from _helpers import REPO
from lib.facts.lint import lint_sections
from lib.facts.lock import LOCK_REL, read_lock


def test_locked_sections_have_no_lint_errors():
    units = set(read_lock(REPO / LOCK_REL))
    assert units, "the lock is empty; the guard would pass vacuously"
    errors = [f for f in lint_sections(REPO, units) if not f.warn]
    assert not errors, "\n".join(f"{f.unit}: {f.rule} — {f.detail}" for f in errors)
