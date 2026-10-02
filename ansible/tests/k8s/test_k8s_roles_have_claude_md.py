"""Every ansible/roles/k8s/ role directory carries a CLAUDE.md doc.

A role's doc is the first thing a session reads before touching that service
(repo CLAUDE.md's "Where to Look" table routes here), so a missing one means the
session reads the tasks/templates cold every time.

Red-proof pair: a fixture role with no CLAUDE.md is flagged by
`_check_role_doc`; a fixture role with an adequate one passes clean. Both
fixtures exercise the exact helper the real test uses, not a re-implementation
of its logic.

The size ceiling: a role doc is loaded whole on every touch of the role, so a
long one costs its full size each time. A doc over `MAX_CHARS` fails unless
`OVER_CEILING` names the role with the reason, and an entry there for a role that
has since shrunk fails too, so the list cannot rot.

The unit is characters, and `MAX_CHARS` is the inject hook's payload budget
(`_doc_size.MAX_CHARS`). A line-based ceiling passes docs the hook still
truncates to their head, because the hook counts characters.

The warning band: a ceiling that only fails OVER the ceiling tells
nobody anything until the ceiling is already breached, so the first author to
learn about it is the one whose bullet does not fit. A doc
between `WARN_CHARS` and `MAX_CHARS` is reported through a `RoleDocNearCeiling`
warning, which `pyproject.toml`'s `filterwarnings` shows rather than errors, so
the signal arrives with room left to write the bullet and the guard still fails
only over the ceiling.
"""

import warnings
from pathlib import Path

from _doc_size import (
    MAX_CHARS,
    RoleDocNearCeiling,
    char_count,
    recorded_count_problems,
)
from _helpers import REPO
from _role_census import role_dirs

K8S_ROLES_DIR = REPO / "ansible" / "roles" / "k8s"

# Nothing is excluded. manifests/ used to be, as the shared render -> apply -> queue role every
# other k8s role includes rather than a service with a deploy tag of its own -- which left its
# doc under no size bound at all while it grew to 4x the inject hook's inline budget (#3246).
# It satisfies `_mentions_deploy_tag` the way the other tagless shared roles do, by saying in
# its own doc that there is nothing to type, so the exemption bought nothing a doc line does not.
EXCLUDED_DIRS: set[str] = set()

MIN_NON_BLANK_LINES = 8
# The band is a fraction of the ceiling rather than a second hand-set number, so moving
# MAX_CHARS moves both. 0.9 leaves 750 characters — room for a bullet and the measurement
# under it, and narrow enough that a doc in the band is genuinely close rather than large.
WARN_FRACTION = 0.9
WARN_CHARS = int(MAX_CHARS * WARN_FRACTION)

# Roles whose CLAUDE.md is allowed over MAX_CHARS, each with the reason. An entry is a
# justification, not a waiver: it names the operating rule that cannot move to a docs/ page,
# and the section to move out next. An entry for a doc that has since shrunk fails, so a
# stale waiver cannot survive the trim that earned it, and a doc that GROWS past its recorded
# count fails too — the ratchet is what makes these entries a shrinking list rather
# than a set of permanent exemptions. Each remaining reason names its own next section to
# move.
OVER_CEILING: dict[str, str] = {}


def _role_dirs() -> list[Path]:
    return sorted(
        d
        for d in role_dirs(K8S_ROLES_DIR)
        if d.name not in EXCLUDED_DIRS and not d.name.startswith(".")
    )


def _non_blank_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _mentions_deploy_tag(role_name: str, text: str) -> bool:
    """True if some line names both the role and a literal `--tags` flag.

    Checking for the bare word "tag" over-matches: "outage", "voltage" and
    "wattage" all contain it, and this repo's docs are full of incident
    write-ups that use those words next to a role name. Requiring the actual
    `--tags` flag on the same line is what makes a "how do I deploy this"
    line, not an unrelated sentence, the thing that passes.

    For a normal service the deploy tag IS the role's directory name (the
    `containers_list` entry and the role directory agree by convention,
    verified against `ansible/inventory/host_vars/daniel-box.yml`), so a line
    like `--tags "freshrss"` in the freshrss doc satisfies this. A shared/gate
    role with no standalone tag satisfies it by saying so explicitly, e.g.
    "no standalone deploy tag ... not `--tags cronjob-gate`" -- that line
    still carries both the role name and `--tags`, which is the fact a reader
    needs: what to type, or that there is nothing to type.
    """
    name = role_name.lower()
    for line in text.lower().splitlines():
        if "--tags" in line and name in line:
            return True
    return False


def _check_role_doc(
    role_dir: Path, over_ceiling: dict[str, str] = OVER_CEILING
) -> list[str]:
    """Return problems with role_dir's CLAUDE.md ([] if it's adequate)."""
    doc = role_dir / "CLAUDE.md"
    if not doc.exists():
        return [f"{role_dir.name}: no CLAUDE.md"]
    text = doc.read_text()
    problems = []
    if len(_non_blank_lines(text)) < MIN_NON_BLANK_LINES:
        problems.append(
            f"{role_dir.name}: CLAUDE.md has fewer than {MIN_NON_BLANK_LINES} non-blank lines"
        )
    chars = char_count(text)
    if chars > MAX_CHARS and role_dir.name not in over_ceiling:
        problems.append(
            f"{role_dir.name}: CLAUDE.md is {chars} chars, over the {MAX_CHARS}-char ceiling "
            f"(the inject hook's payload budget) — move history and measurements to a docs/ "
            f"page, or add the role to OVER_CEILING with the reason (issues #2126, #2826)"
        )
    if role_dir.name in over_ceiling:
        problems.extend(
            recorded_count_problems(role_dir.name, chars, over_ceiling[role_dir.name])
        )
    if not _mentions_deploy_tag(role_dir.name, text):
        problems.append(
            f"{role_dir.name}: CLAUDE.md doesn't mention the role's deploy tag"
        )
    return problems


def _band_notice(
    role_dir: Path, over_ceiling: dict[str, str] = OVER_CEILING
) -> str | None:
    """Return a notice if role_dir's CLAUDE.md sits in the warning band, else None.

    A doc already past MAX_CHARS is left alone: an unjustified one is a FAILURE of
    `_check_role_doc`, and a justified one is an OVER_CEILING entry whose reason already says
    what to do. Warning about either would report a doc the author cannot act on differently.
    """
    doc = role_dir / "CLAUDE.md"
    if not doc.exists() or role_dir.name in over_ceiling:
        return None
    chars = char_count(doc.read_text())
    if not WARN_CHARS <= chars <= MAX_CHARS:
        return None
    return (
        f"{role_dir.name}: CLAUDE.md is {chars} chars, {MAX_CHARS - chars} short of the "
        f"{MAX_CHARS}-char ceiling — move history and measurements to a docs/ page now, while "
        f"there is still room for the bullet you came to write (issue #2557)"
    )


def test_every_k8s_role_has_an_adequate_claude_md():
    problems = []
    for role_dir in _role_dirs():
        problems.extend(_check_role_doc(role_dir))
    assert not problems, "\n".join(problems)


def test_census_sees_at_least_60_roles():
    # Non-vacuity: if the glob above breaks (roles renamed, moved a level
    # down), `_role_dirs()` can silently return an empty or tiny list and the
    # test above passes for checking nothing. Pin a concrete floor instead.
    n = len(_role_dirs())
    assert n >= 60, (
        f"only found {n} k8s role directories under {K8S_ROLES_DIR} (want >= 60)"
    )


def test_fixture_role_with_no_doc_is_flagged(tmp_path):
    role = tmp_path / "widget"
    role.mkdir()
    assert _check_role_doc(role) == ["widget: no CLAUDE.md"]


def test_fixture_role_with_adequate_doc_passes(tmp_path):
    role = tmp_path / "widget"
    role.mkdir()
    (role / "CLAUDE.md").write_text(
        "# widget — a fixture role\n\n"
        "This fixture exists only for test_k8s_roles_have_claude_md.py.\n\n"
        "- line a\n- line b\n- line c\n- line d\n- line e\n\n"
        'Deploy: `--tags "widget"`.\n'
    )
    assert _check_role_doc(role) == []


def test_fixture_role_with_no_tag_mention_is_flagged(tmp_path):
    role = tmp_path / "widget"
    role.mkdir()
    (role / "CLAUDE.md").write_text(
        "# widget — a fixture role\n\n"
        "This fixture exists only for test_k8s_roles_have_claude_md.py, and\n"
        "documents widget at length without ever saying how it is deployed.\n\n"
        "- line a\n- line b\n- line c\n- line d\n- line e\n- line f\n"
    )
    assert _check_role_doc(role) == [
        "widget: CLAUDE.md doesn't mention the role's deploy tag"
    ]


def test_fixture_role_too_short_is_flagged_even_with_a_tag_mention(tmp_path):
    role = tmp_path / "widget"
    role.mkdir()
    (role / "CLAUDE.md").write_text('# widget\n\nDeploy: `--tags "widget"`.\n')
    assert _check_role_doc(role) == [
        "widget: CLAUDE.md has fewer than 8 non-blank lines"
    ]


def _sized_doc(chars: int) -> str:
    """An otherwise-adequate widget doc of exactly `chars` characters."""
    head = '# widget — a fixture role\n\nDeploy: `--tags "widget"`.\n'
    padded = head + "".join(f"- line {i}\n" for i in range(chars))
    return padded[:chars]


def test_fixture_role_over_the_ceiling_is_flagged(tmp_path):
    role = tmp_path / "widget"
    role.mkdir()
    (role / "CLAUDE.md").write_text(_sized_doc(MAX_CHARS + 1))
    problems = _check_role_doc(role, over_ceiling={})
    assert len(problems) == 1 and problems[0].startswith(
        f"widget: CLAUDE.md is {MAX_CHARS + 1} chars, over the {MAX_CHARS}-char ceiling"
    ), problems


def test_fixture_role_at_the_ceiling_passes(tmp_path):
    role = tmp_path / "widget"
    role.mkdir()
    (role / "CLAUDE.md").write_text(_sized_doc(MAX_CHARS))
    assert _check_role_doc(role, over_ceiling={}) == []


def test_fixture_role_over_the_ceiling_passes_when_justified(tmp_path):
    role = tmp_path / "widget"
    role.mkdir()
    (role / "CLAUDE.md").write_text(_sized_doc(MAX_CHARS + 1))
    reason = f"{MAX_CHARS + 1} chars on 2026-09-26, a reason"
    assert _check_role_doc(role, over_ceiling={"widget": reason}) == []


def test_fixture_role_grown_past_its_recorded_count_is_flagged(tmp_path):
    role = tmp_path / "widget"
    role.mkdir()
    (role / "CLAUDE.md").write_text(_sized_doc(MAX_CHARS + 2))
    reason = f"{MAX_CHARS + 1} chars on 2026-09-26, a reason"
    problems = _check_role_doc(role, over_ceiling={"widget": reason})
    assert (
        len(problems) == 1
        and f"past the {MAX_CHARS + 1} its OVER_CEILING reason records" in problems[0]
    )


def test_over_ceiling_entries_are_still_over_the_ceiling():
    """A justification for a doc that has since shrunk is dead text, and would wave a regrowth
    through; an entry for a role that no longer exists is the same rot.
    """
    stale = [
        name
        for name in OVER_CEILING
        if not (K8S_ROLES_DIR / name / "CLAUDE.md").is_file()
        or char_count((K8S_ROLES_DIR / name / "CLAUDE.md").read_text()) <= MAX_CHARS
    ]
    assert not stale, (
        f"OVER_CEILING names docs no longer over {MAX_CHARS} chars: {stale}"
    )


def test_role_docs_near_the_ceiling_are_reported_without_failing():
    """Always green: it reports the band, it never judges it.

    Non-vacuity comes from `test_census_sees_at_least_60_roles`, which pins the role list this
    walks; the fixture pair below pins the band arithmetic itself.
    """
    for role_dir in _role_dirs():
        notice = _band_notice(role_dir)
        if notice:
            warnings.warn(notice, RoleDocNearCeiling, stacklevel=2)


def test_fixture_role_at_the_warning_band_is_reported(tmp_path):
    role = tmp_path / "widget"
    role.mkdir()
    (role / "CLAUDE.md").write_text(_sized_doc(WARN_CHARS))
    notice = _band_notice(role, over_ceiling={})
    assert notice is not None and notice.startswith(
        f"widget: CLAUDE.md is {WARN_CHARS} chars, {MAX_CHARS - WARN_CHARS} short of the"
    ), notice
    # The band reports; it does not fail.
    assert _check_role_doc(role, over_ceiling={}) == []


def test_fixture_role_below_the_warning_band_is_not_reported(tmp_path):
    role = tmp_path / "widget"
    role.mkdir()
    (role / "CLAUDE.md").write_text(_sized_doc(WARN_CHARS - 1))
    assert _band_notice(role, over_ceiling={}) is None


def test_fixture_role_over_the_ceiling_fails_instead_of_being_reported(tmp_path):
    role = tmp_path / "widget"
    role.mkdir()
    (role / "CLAUDE.md").write_text(_sized_doc(MAX_CHARS + 1))
    assert _band_notice(role, over_ceiling={}) is None
    assert len(_check_role_doc(role, over_ceiling={})) == 1


def test_fixture_role_justified_over_the_ceiling_is_not_reported(tmp_path):
    role = tmp_path / "widget"
    role.mkdir()
    (role / "CLAUDE.md").write_text(_sized_doc(WARN_CHARS + 1))
    assert _band_notice(role, over_ceiling={"widget": "a reason"}) is None


def test_a_band_notice_is_shown_rather_than_raised():
    """Raise one under the session's OWN filters: `filterwarnings = ["error"]` would raise here.

    The `always::_doc_size.RoleDocNearCeiling` entry in `pyproject.toml` is what keeps the band a
    report instead of a failure, and dropping it turns the census above red the day a doc reaches
    WARN_CHARS — years after the edit that dropped it. This test fails the same day the entry
    goes, because pytest applies the config filters around every test item.

    A broken entry needs no test: pytest raises `PytestConfigWarning: Failed to import filter
    module` when it cannot resolve the category, and the blanket `error` turns that into a failure
    of every test in the run. Only a MISSING entry is silent, and that is what this covers.
    """
    warnings.warn(
        "band reporting self-check, not a real role doc",
        RoleDocNearCeiling,
        stacklevel=2,
    )
