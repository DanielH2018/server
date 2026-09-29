"""Every ansible/roles/containers/ role has a CLAUDE.md, and none of them is over the ceiling.

Written for issue #2920. The character ceiling from #2826 reached two of the three role planes:
`ansible/tests/k8s/test_k8s_roles_have_claude_md.py` walks `ansible/roles/k8s/` and
`ansible/tests/setup/test_setup_roles_have_claude_md.py` walks `SETUP_ROLES`. Nothing walked
`CONTAINER_ROLES`, so `wg-easy/CLAUDE.md` sat at 8,974 characters — over the inject hook's
budget, which means a session read it truncated to its head — and no test said so.

The unit and the ceiling come from `_doc_size`, the module the other two guards share, so all
three planes agree on what the ceiling is and on what a justified `OVER_CEILING` entry must say.

`_check_role_doc` from the k8s guard is NOT reused, for the reason the setup guard gives: its
deploy-tag arm assumes the `containers_list` convention where the tag is the role's directory
name, and `common` is include-only with no tag of its own. Existence, a minimum length and the
size ceiling are what transfer.

There is no warning band here. The band reports a doc approaching the ceiling through a
`RoleDocNearCeiling` warning, and it is the k8s plane's 60-plus roles that make an approaching
doc easy to miss; this plane is five roles on one host, and the setup guard carries no band
either.

`OVER_CEILING` starts EMPTY: `wg-easy`'s retired peer-pull history moved out to
`ansible/roles/k8s/pi-peer-backup/CLAUDE.md`, which owns that mechanism, in the commit that
added this guard. A plane whose docs all fit needs no exemptions, and minting one on day one
would be the loosening half of the ratchet.

Red-proof pairs use fixture roles under `tmp_path`, and non-vacuity pins both a census floor
and `wg-easy` by name — the role the issue was filed on, and the one a broken glob would stop
measuring.
"""

from pathlib import Path

from _doc_size import MAX_CHARS, char_count, recorded_count_problems
from _helpers import CONTAINER_ROLES
from _role_census import role_dirs

MIN_NON_BLANK_LINES = 8

# Roles whose CLAUDE.md is allowed over MAX_CHARS, each with the reason. An entry is a
# justification, not a waiver: it names the operating rule that cannot move to a docs/ page or
# to the role that owns the mechanism, and the section to move out next. A doc that grows past
# its recorded count fails (#2679), and an entry for a doc that has since shrunk fails too, so
# the list shrinks rather than settling. Same shape as the k8s and setup planes' lists.
OVER_CEILING: dict[str, str] = {}


def _role_dirs(roles_dir: Path = CONTAINER_ROLES) -> list[Path]:
    """Every role under `roles_dir`, a dotted directory and a retired role's shell skipped.

    A shell is a directory holding only a gitignored `__pycache__/` after the deployer's
    fast-forward removed the role's tracked files; reading it as a role made this guard raise
    `FileNotFoundError` on its missing `CLAUDE.md` (#2964).
    """
    return [d for d in role_dirs(roles_dir) if not d.name.startswith(".")]


def _non_blank_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _doc_problems(
    role_dir: Path, over_ceiling: dict[str, str] = OVER_CEILING
) -> list[str]:
    """Return problems with role_dir's CLAUDE.md ([] if it is adequate)."""
    doc = role_dir / "CLAUDE.md"
    if not doc.is_file():
        return [f"{role_dir.name}: no CLAUDE.md"]
    text = doc.read_text()
    if len(_non_blank_lines(text)) < MIN_NON_BLANK_LINES:
        return [
            f"{role_dir.name}: CLAUDE.md has fewer than {MIN_NON_BLANK_LINES} non-blank lines"
        ]
    chars = char_count(text)
    if chars > MAX_CHARS and role_dir.name not in over_ceiling:
        return [
            f"{role_dir.name}: CLAUDE.md is {chars} chars, over the {MAX_CHARS}-char ceiling "
            f"(the inject hook's payload budget) — move history and measurements to a docs/ "
            f"page or to the role that owns the mechanism, or add the role to OVER_CEILING "
            f"with the reason (issues #2126, #2826, #2920)"
        ]
    if role_dir.name in over_ceiling:
        return recorded_count_problems(
            role_dir.name, chars, over_ceiling[role_dir.name]
        )
    return []


def test_every_container_role_has_an_adequate_claude_md():
    problems = []
    for role_dir in _role_dirs():
        problems.extend(_doc_problems(role_dir))
    assert not problems, "\n".join(problems)


def test_census_finds_the_pi_roles_it_must_measure():
    """Non-vacuity, per CLAUDE.md's "a check that finds its own subject by pattern" rule.

    `wg-easy` is the role #2920 was filed on; `common` is the include-only shared deploy path,
    which is the member a guard copied from the k8s plane would have failed on its deploy tag.
    """
    found = {d.name for d in _role_dirs()}
    expected = {"wg-easy", "common", "alloy", "autoheal", "docker-proxy"}
    assert expected <= found, f"missing from the containers census: {expected - found}"


def test_census_sees_at_least_five_container_roles():
    n = len(_role_dirs())
    assert n >= 5, f"only found {n} role directories under {CONTAINER_ROLES}"


def test_over_ceiling_entries_are_still_over_the_ceiling():
    """A justification for a doc that has since shrunk is dead text, and would wave a regrowth
    through; an entry for a role that no longer exists is the same rot.
    """
    stale = [
        name
        for name in OVER_CEILING
        if not (CONTAINER_ROLES / name / "CLAUDE.md").is_file()
        or char_count((CONTAINER_ROLES / name / "CLAUDE.md").read_text()) <= MAX_CHARS
    ]
    assert not stale, (
        f"OVER_CEILING names docs no longer over {MAX_CHARS} chars: {stale}"
    )


# ── Fixture pairs ─────────────────────────────────────────────────────────────────────

_LONG_ENOUGH = "# widget\n\n" + "\n".join(f"- line {i}" for i in range(8)) + "\n"


def _fixture_role(tmp_path, name: str, doc: str | None) -> Path:
    role = tmp_path / name
    role.mkdir(parents=True)
    if doc is not None:
        (role / "CLAUDE.md").write_text(doc)
    return role


def _sized_doc(chars: int) -> str:
    """A widget doc of exactly `chars` characters."""
    padded = "# widget\n\n" + "".join(f"- line {i}\n" for i in range(chars))
    return padded[:chars]


def test_fixture_role_with_no_doc_is_flagged(tmp_path):
    role = _fixture_role(tmp_path, "widget", None)
    assert _doc_problems(role) == ["widget: no CLAUDE.md"]


def test_fixture_role_with_a_short_doc_is_flagged(tmp_path):
    role = _fixture_role(tmp_path, "widget", "# widget\n")
    assert _doc_problems(role) == ["widget: CLAUDE.md has fewer than 8 non-blank lines"]


def test_fixture_role_with_an_adequate_doc_passes(tmp_path):
    role = _fixture_role(tmp_path, "widget", _LONG_ENOUGH)
    assert _doc_problems(role) == []


def test_fixture_role_over_the_ceiling_is_flagged(tmp_path):
    role = _fixture_role(tmp_path, "widget", _sized_doc(MAX_CHARS + 1))
    problems = _doc_problems(role, over_ceiling={})
    assert len(problems) == 1 and problems[0].startswith(
        f"widget: CLAUDE.md is {MAX_CHARS + 1} chars, over the {MAX_CHARS}-char ceiling"
    ), problems


def test_fixture_role_at_the_ceiling_passes(tmp_path):
    role = _fixture_role(tmp_path, "widget", _sized_doc(MAX_CHARS))
    assert _doc_problems(role, over_ceiling={}) == []


def test_fixture_role_over_the_ceiling_passes_when_justified(tmp_path):
    role = _fixture_role(tmp_path, "widget", _sized_doc(MAX_CHARS + 1))
    reason = f"{MAX_CHARS + 1} chars on 2026-09-29, a reason"
    assert _doc_problems(role, over_ceiling={"widget": reason}) == []


def test_fixture_role_grown_past_its_recorded_count_is_flagged(tmp_path):
    role = _fixture_role(tmp_path, "widget", _sized_doc(MAX_CHARS + 2))
    reason = f"{MAX_CHARS + 1} chars on 2026-09-29, a reason"
    problems = _doc_problems(role, over_ceiling={"widget": reason})
    assert (
        len(problems) == 1
        and f"past the {MAX_CHARS + 1} its OVER_CEILING reason records" in problems[0]
    )


def test_fixture_reason_without_a_recorded_count_is_flagged(tmp_path):
    role = _fixture_role(tmp_path, "widget", _sized_doc(MAX_CHARS + 1))
    problems = _doc_problems(role, over_ceiling={"widget": "a reason"})
    assert problems == [
        "widget: OVER_CEILING reason must open with '<N> chars on <date>'"
    ]
