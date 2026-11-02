"""M6's `kuma` census row must not fail a file that only cites the plan by its filename (#6903).

Exit criterion 3's census row holds every mention of `kuma` under `ansible/`, `hosts/`,
`scripts/` and `.claude/` to a frozen allowlist. A comment that cites
`m6-retire-kuma.md` contains `kuma`, and stays true after Kuma is gone. The plan must say how the
row treats that citation, and the treatment must cover every file that cites the plan.

Run: uv run pytest ansible/tests/repo/test_m6_census_plan_citation.py
"""

import re

from _markdown_section import section, steps
from lib.proc_testing import run
from lib.repo_paths import REPO

DOC = REPO / "docs" / "target-architecture" / "monitoring" / "m6-retire-kuma.md"
PLAN_NAME = "m6-retire-kuma"
PLAN_DIR_PREFIX = "docs/target-architecture/monitoring/m6-"
CENSUS_TREES = ("ansible", "hosts", "scripts", ".claude")
CENSUS_EXCLUDES = (":!*/tests/*", ":!*/test_*")

# A citation of the plan or of a file beside it: the full path form, then the bare filename.
_CITATION = re.compile(
    re.escape(PLAN_DIR_PREFIX) + r"[\w./-]*|" + re.escape(PLAN_NAME), re.I
)
_C6_ITEM = re.compile(r"^\d+\. \*\*C-6\b.*?(?=^\d+\. |\Z)", re.M | re.S)


def _exit_criterion_3() -> str:
    return steps(section(DOC.read_text(), "## Goal and exit criterion"))[3]


def _c6_item() -> str:
    match = _C6_ITEM.search(section(DOC.read_text(), "## Design"))
    assert match, "no `C-6` numbered item under ## Design"
    return match.group(0)


def _census_rule_text() -> str:
    return _exit_criterion_3() + "\n" + _c6_item()


def _citing_files() -> list[str]:
    argv = ["git", "grep", "-l", "-i", PLAN_NAME, "--", *CENSUS_TREES, *CENSUS_EXCLUDES]
    return run(argv, cwd=REPO).stdout.split()


def _citation_only(rel: str) -> bool:
    text = (REPO / rel).read_text(errors="replace")
    return "kuma" not in _CITATION.sub("", text).lower()


def test_the_census_rule_names_the_plan_filename_it_strips():
    rule = _census_rule_text()
    assert PLAN_NAME in rule, (
        f"neither exit criterion 3 nor C-6 says how the census row treats `{PLAN_NAME}`"
    )


def test_the_census_rule_names_the_plan_directory_path_it_strips():
    rule = _census_rule_text()
    assert PLAN_DIR_PREFIX in rule, (
        f"neither exit criterion 3 nor C-6 says how the census row treats a "
        f"`{PLAN_DIR_PREFIX}` path"
    )


def _named_in(rel: str, rule: str) -> bool:
    # The plan lists an allowlisted file by full path or, beside a sibling, by basename.
    return rel in rule or rel.rsplit("/", 1)[1] in rule


def test_no_file_that_only_cites_the_plan_fails_the_census_row():
    files = _citing_files()
    # Named member: #6891 allowlisted it, so the census must still find it.
    assert "hosts/components/pi-heartbeats/templates/pi-sd-health.sh.j2" in files
    rule = _census_rule_text()
    uncovered = [
        rel
        for rel in files
        if _citation_only(rel) and PLAN_NAME not in rule and not _named_in(rel, rule)
    ]
    assert not uncovered, (
        "these files mention `kuma` only by citing the M6 plan, yet the census row as the "
        f"plan states it would fail on them: {uncovered}"
    )


def _strip_bullet() -> str:
    """Exit criterion 3's bullet that says what the census row strips before it matches."""
    bullets = re.split(r"^    - ", _exit_criterion_3(), flags=re.M)
    found = [b for b in bullets if re.search(r"\bstrips\b", b) and PLAN_NAME in b]
    assert len(found) == 1, (
        f"want one exit-criterion-3 bullet stating the strip, got {found}"
    )
    return found[0]


def _stated_strip(bullet: str) -> re.Pattern[str]:
    # The bullet backticks other paths too; only an `m6-` token names a citation.
    # One ending in `/` or `-` is a path prefix, any other is literal.
    tokens = [t for t in re.findall(r"`([^`]+)`", bullet) if "m6-" in t]
    alts = [re.escape(t) + (r"[\w./-]*" if t[-1] in "/-" else "") for t in tokens]
    return re.compile("|".join(alts), re.I)


def test_the_stated_strip_rule_keeps_a_bare_kuma_and_covers_every_citation():
    bullet = _strip_bullet()
    flat = " ".join(bullet.split())
    assert not re.search(r"\b(not|never|no longer) strip", flat), flat
    assert "bare `kuma` beside a citation" in flat and "fails" in flat, (
        "the strip bullet must name the red fixture that keeps a bare `kuma` failing"
    )
    strip = _stated_strip(bullet)
    assert "kuma" not in strip.sub("", "# see m6-retire-kuma.md, C-6").lower()
    assert (
        "kuma"
        not in strip.sub(
            "", "# docs/target-architecture/monitoring/m6-retire-kuma.md#design"
        ).lower()
    )
    assert "kuma" in strip.sub("", "kuma_url = x  # m6-retire-kuma.md").lower()
    # The plan's own tokens must clear every citation form the tree holds, not just the test's.
    stated = {
        rel
        for rel in _citing_files()
        if "kuma" not in strip.sub("", (REPO / rel).read_text(errors="replace")).lower()
    }
    expected = {rel for rel in _citing_files() if _citation_only(rel)}
    assert "hosts/components/pi-heartbeats/templates/pi-sd-health.sh.j2" in stated
    assert stated == expected, f"the stated strip leaves `kuma` in {expected - stated}"
