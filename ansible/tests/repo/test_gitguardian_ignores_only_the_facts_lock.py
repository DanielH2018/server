"""`.gitguardian.yaml` exempts the facts lock from GitGuardian's scan, and nothing else.

GitGuardian's high-entropy detector reads the lock's content digests as secrets (#6366), so the
config ignores `docs/facts.lock/`. Every other path in the repo stays scanned: an ignored path
outside the lock is a place a real credential could land without an incident.

Run: uv run pytest ansible/tests/repo/test_gitguardian_ignores_only_the_facts_lock.py
"""

from lib import yaml_fast
from _helpers import REPO

LOCK_DIR = "docs/facts.lock/"


def outside_the_lock(patterns: list[str]) -> list[str]:
    """The ignore patterns that could match a path outside `docs/facts.lock/`."""
    return [p for p in patterns if not p.startswith(LOCK_DIR) or ".." in p]


def test_a_pattern_inside_the_lock_is_clean():
    assert outside_the_lock(["docs/facts.lock/**", "docs/facts.lock/*.json"]) == []


def test_a_pattern_outside_the_lock_is_flagged():
    for pattern in (
        "**/*.json",
        "docs/",
        "docs/facts.lock/../vars/**",
        "**/facts.lock/**",
    ):
        assert outside_the_lock([pattern]) == [pattern]


def test_gitguardian_yaml_ignores_the_lock_and_nothing_else():
    config = yaml_fast.safe_load((REPO / ".gitguardian.yaml").read_text())
    assert config["version"] == 2
    patterns = config["secret"]["ignored_paths"]
    assert "docs/facts.lock/**" in patterns, "subject gone: the facts.lock exclusion"
    assert outside_the_lock(patterns) == []
