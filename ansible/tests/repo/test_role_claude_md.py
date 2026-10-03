"""Every role on every plane carries a CLAUDE.md the inject hook delivers whole.

A session reads a role's doc before touching the role, so a missing one means reading its tasks
cold. `PLANES` has one row per role tree, and every check below runs once per row (#3384).

A row carries what differs between planes and nothing else:

- `deploy_tag`: the doc must name the role beside a literal `--tags`. Only k8s, where the
  deploy tag IS the role's directory name. Setup roles are applied through `initial_setup.yml`
  with tags that do not follow that convention, and both `common` roles are include-only.
- `warn_band`: a doc between `WARN_CHARS` and `MAX_CHARS` is reported through a
  `RoleDocNearCeiling` warning. Only k8s, whose 60-plus roles make an approaching doc easy to
  miss. `pyproject.toml`'s `filterwarnings` shows the warning rather than raising it.
- `over_ceiling`: roles whose doc may exceed `MAX_CHARS`, each reason opening with the count it
  was written against. Growing past that count fails, and so does an entry whose doc has since
  shrunk, so the lists only shrink. All three are empty.
- `floor` and `members`: non-vacuity. A row whose tree yields no roles fails with "subject gone:
  delete this row" instead of passing over nothing, and each row names roles a broken census
  must still find.

The ceiling is in characters: what `.claude/hooks/inject-nested-docs.py` leaves for the doc
after its preamble and header (`_doc_size`). The hook tests at the bottom ask the hook's own
`_fits_inline` about a doc of exactly `MAX_CHARS`, then about every real role doc (#3245).

The cron-contract half of the setup plane is a different property and stays in
`ansible/tests/setup/test_setup_cron_roles_have_a_contract.py`.

Run: uv run pytest ansible/tests/repo/test_role_claude_md.py
"""

import warnings
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from _doc_size import (
    HEADER_ALLOWANCE,
    INJECT_HOOK,
    MAX_CHARS,
    RoleDocNearCeiling,
    char_count,
    effective_ceiling,
    hook_inline_budget,
    hook_inline_max_chars,
    load_inject_hook,
    recorded_count_problems,
)
from _helpers import CONTAINER_ROLES, K8S_ROLES, REPO, SETUP_ROLES, run
from _role_census import role_dirs

MIN_NON_BLANK_LINES = 8
# A fraction of the ceiling, so moving MAX_CHARS moves both. 0.9 leaves about 700 characters:
# room for a bullet and its measurement, narrow enough that a doc in the band is genuinely close.
WARN_FRACTION = 0.9
WARN_CHARS = int(MAX_CHARS * WARN_FRACTION)


@dataclass(frozen=True)
class Plane:
    name: str
    roles_dir: Path
    floor: int
    members: frozenset[str]
    deploy_tag: bool = False
    warn_band: bool = False
    over_ceiling: dict[str, str] = field(default_factory=dict)


PLANES = [
    # `manifests` is a member rather than an exclusion. It used to be excluded as the shared
    # render role every other k8s role includes, which left its doc under no size bound while
    # it grew to 4x the hook's budget (#3246). It meets the deploy-tag arm the way the other
    # tagless shared roles do, by saying in its doc that there is nothing to type.
    Plane(
        "k8s",
        K8S_ROLES,
        floor=60,
        members=frozenset({"pihole", "traefik", "manifests"}),
        deploy_tag=True,
        warn_band=True,
    ),
    Plane(
        "setup",
        SETUP_ROLES,
        floor=12,
        members=frozenset({"gitops_deploy", "k3s", "common", "nut_host"}),
    ),
    # `wg-easy` is the role a broken glob would stop measuring; `common` is the include-only
    # shared deploy path, the member a deploy-tag arm would wrongly fail.
    Plane(
        "containers",
        CONTAINER_ROLES,
        floor=5,
        members=frozenset({"wg-easy", "common", "alloy", "autoheal", "docker-proxy"}),
    ),
]
PLANE_IDS = [p.name for p in PLANES]
BY_NAME = {p.name: p for p in PLANES}


def _role_dirs(plane: Plane, roles_dir: Path | None = None) -> list[Path]:
    """Every role in the plane's tree, a dotted directory and a retired role's shell skipped."""
    return [
        d for d in role_dirs(roles_dir or plane.roles_dir) if not d.name.startswith(".")
    ]


def _non_blank_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _mentions_deploy_tag(role_name: str, text: str) -> bool:
    """True if some line names both the role and a literal `--tags` flag.

    The bare word "tag" matches "outage" and "voltage". A shared role with no standalone tag
    passes by saying so ("not `--tags cronjob-gate`"), which is the fact a reader needs.
    """
    name = role_name.lower()
    return any("--tags" in line and name in line for line in text.lower().splitlines())


def _doc_problems(
    role_dir: Path, plane: Plane, over_ceiling: dict[str, str] | None = None
) -> list[str]:
    """Every problem with role_dir's CLAUDE.md under the plane's rules ([] if adequate)."""
    over_ceiling = plane.over_ceiling if over_ceiling is None else over_ceiling
    doc = role_dir / "CLAUDE.md"
    if not doc.is_file():
        return [f"{role_dir.name}: no CLAUDE.md"]
    text = doc.read_text()
    problems = []
    if len(_non_blank_lines(text)) < MIN_NON_BLANK_LINES:
        problems.append(
            f"{role_dir.name}: CLAUDE.md has fewer than {MIN_NON_BLANK_LINES} non-blank lines"
        )
    chars = char_count(text)
    if role_dir.name in over_ceiling:
        problems.extend(
            recorded_count_problems(role_dir.name, chars, over_ceiling[role_dir.name])
        )
    elif chars > MAX_CHARS:
        problems.append(
            f"{role_dir.name}: CLAUDE.md is {chars} chars, over the {MAX_CHARS}-char ceiling "
            f"(the inject hook's payload budget) — move history and measurements to a docs/ "
            f"page or to the role that owns the mechanism, or add the role to its plane's "
            f"over_ceiling with the reason (issues #2126, #2826, #2920)"
        )
    if plane.deploy_tag and not _mentions_deploy_tag(role_dir.name, text):
        problems.append(
            f"{role_dir.name}: CLAUDE.md doesn't mention the role's deploy tag"
        )
    return problems


def _band_notice(
    role_dir: Path, plane: Plane, over_ceiling: dict[str, str] | None = None
) -> str | None:
    """A notice if role_dir's CLAUDE.md sits in the plane's warning band, else None.

    A doc past MAX_CHARS is left alone: an unjustified one already FAILS `_doc_problems`, and a
    justified one has a reason that says what to do. Warning about either reports a doc the
    author cannot act on differently.
    """
    over_ceiling = plane.over_ceiling if over_ceiling is None else over_ceiling
    doc = role_dir / "CLAUDE.md"
    if not plane.warn_band or not doc.is_file() or role_dir.name in over_ceiling:
        return None
    chars = char_count(doc.read_text())
    if not WARN_CHARS <= chars <= MAX_CHARS:
        return None
    return (
        f"{role_dir.name}: CLAUDE.md is {chars} chars, {MAX_CHARS - chars} short of the "
        f"{MAX_CHARS}-char ceiling — move history and measurements to a docs/ page now, while "
        f"there is still room for the bullet you came to write (issue #2557)"
    )


# ── The live tree, one row at a time ──────────────────────────────────────────────────


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_census_finds_its_named_members(plane):
    """Non-vacuity, per python-layout.md's "a named member it must find" rule."""
    found = {d.name for d in _role_dirs(plane)}
    assert found, (
        f"subject gone: {plane.roles_dir} holds no roles — delete the {plane.name!r} row"
    )
    assert len(found) >= plane.floor, (
        f"only found {len(found)} {plane.name} roles under {plane.roles_dir} "
        f"(want >= {plane.floor})"
    )
    assert plane.members <= found, (
        f"missing from the {plane.name} census: {plane.members - found}"
    )


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_every_role_has_an_adequate_claude_md(plane):
    problems = [p for d in _role_dirs(plane) for p in _doc_problems(d, plane)]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_over_ceiling_entries_are_still_over_the_ceiling(plane):
    """A reason for a doc that has since shrunk is dead text and would wave a regrowth through;
    an entry for a role that no longer exists is the same rot.
    """
    stale = [
        name
        for name in plane.over_ceiling
        if not (plane.roles_dir / name / "CLAUDE.md").is_file()
        or char_count((plane.roles_dir / name / "CLAUDE.md").read_text()) <= MAX_CHARS
    ]
    assert not stale, (
        f"{plane.name} over_ceiling names docs no longer over {MAX_CHARS} chars: {stale}"
    )


def test_role_docs_near_the_ceiling_are_reported_without_failing():
    """Always green: it reports the band, it never judges it."""
    for plane in PLANES:
        for role_dir in _role_dirs(plane):
            notice = _band_notice(role_dir, plane)
            if notice:
                warnings.warn(notice, RoleDocNearCeiling, stacklevel=2)


def test_a_band_notice_is_shown_rather_than_raised():
    """Raise one under the session's OWN filters: `filterwarnings = ["error"]` would raise here.

    This fails the day `pyproject.toml` loses its `always::_doc_size.RoleDocNearCeiling` entry,
    rather than the later day a real doc first reaches WARN_CHARS.
    """
    warnings.warn(
        "band reporting self-check, not a real role doc",
        RoleDocNearCeiling,
        stacklevel=2,
    )


# ── Fixture pairs, run against every row ──────────────────────────────────────────────

_TAG_LINE = 'Deploy: `--tags "widget"`.\n'
_LONG_ENOUGH = "# widget\n\n" + "".join(f"- line {i}\n" for i in range(8)) + _TAG_LINE


def _fixture_role(tmp_path, doc: str | None) -> Path:
    role = tmp_path / "widget"
    role.mkdir()
    if doc is not None:
        (role / "CLAUDE.md").write_text(doc)
    return role


def _sized_doc(chars: int) -> str:
    """An otherwise-adequate widget doc of exactly `chars` characters."""
    padded = "# widget\n\n" + _TAG_LINE + "".join(f"- line {i}\n" for i in range(chars))
    return padded[:chars]


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_fixture_role_with_no_doc_is_flagged(tmp_path, plane):
    assert _doc_problems(_fixture_role(tmp_path, None), plane) == [
        "widget: no CLAUDE.md"
    ]


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_fixture_role_with_an_adequate_doc_passes(tmp_path, plane):
    assert _doc_problems(_fixture_role(tmp_path, _LONG_ENOUGH), plane) == []


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_fixture_role_with_a_short_doc_is_flagged(tmp_path, plane):
    role = _fixture_role(tmp_path, "# widget\n\n" + _TAG_LINE)
    assert _doc_problems(role, plane) == [
        "widget: CLAUDE.md has fewer than 8 non-blank lines"
    ]


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_fixture_role_with_no_tag_mention_is_flagged_only_where_the_row_asks(
    tmp_path, plane
):
    role = _fixture_role(tmp_path, _LONG_ENOUGH.replace(_TAG_LINE, ""))
    expected = (
        ["widget: CLAUDE.md doesn't mention the role's deploy tag"]
        if plane.deploy_tag
        else []
    )
    assert _doc_problems(role, plane) == expected


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_fixture_role_over_the_ceiling_is_flagged(tmp_path, plane):
    role = _fixture_role(tmp_path, _sized_doc(MAX_CHARS + 1))
    problems = _doc_problems(role, plane, over_ceiling={})
    assert len(problems) == 1 and problems[0].startswith(
        f"widget: CLAUDE.md is {MAX_CHARS + 1} chars, over the {MAX_CHARS}-char ceiling"
    ), problems


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_fixture_role_at_the_ceiling_passes(tmp_path, plane):
    role = _fixture_role(tmp_path, _sized_doc(MAX_CHARS))
    assert _doc_problems(role, plane, over_ceiling={}) == []


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_fixture_role_over_the_ceiling_passes_when_justified(tmp_path, plane):
    role = _fixture_role(tmp_path, _sized_doc(MAX_CHARS + 1))
    reason = f"{MAX_CHARS + 1} chars on 2026-09-26, a reason"
    assert _doc_problems(role, plane, over_ceiling={"widget": reason}) == []


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_fixture_role_grown_past_its_recorded_count_is_flagged(tmp_path, plane):
    role = _fixture_role(tmp_path, _sized_doc(MAX_CHARS + 2))
    reason = f"{MAX_CHARS + 1} chars on 2026-09-26, a reason"
    problems = _doc_problems(role, plane, over_ceiling={"widget": reason})
    assert (
        len(problems) == 1
        and f"past the {MAX_CHARS + 1} its OVER_CEILING reason records" in problems[0]
    )


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_fixture_reason_without_a_recorded_count_is_flagged(tmp_path, plane):
    role = _fixture_role(tmp_path, _sized_doc(MAX_CHARS + 1))
    assert _doc_problems(role, plane, over_ceiling={"widget": "a reason"}) == [
        "widget: OVER_CEILING reason must open with '<N> chars on <date>'"
    ]


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_fixture_role_at_the_warning_band_is_reported_only_where_the_row_asks(
    tmp_path, plane
):
    role = _fixture_role(tmp_path, _sized_doc(WARN_CHARS))
    notice = _band_notice(role, plane, over_ceiling={})
    if plane.warn_band:
        assert notice is not None and notice.startswith(
            f"widget: CLAUDE.md is {WARN_CHARS} chars, {MAX_CHARS - WARN_CHARS} short of the"
        ), notice
    else:
        assert notice is None
    # The band reports; it does not fail.
    assert _doc_problems(role, plane, over_ceiling={}) == []


def test_fixture_band_edges_are_not_reported(tmp_path):
    """Below the band, past the ceiling (a failure instead) and justified: no notice."""
    k8s = BY_NAME["k8s"]
    for chars, over_ceiling in [
        (WARN_CHARS - 1, {}),
        (MAX_CHARS + 1, {}),
        (WARN_CHARS + 1, {"widget": "a reason"}),
    ]:
        role = tmp_path / str(chars) / "widget"
        role.mkdir(parents=True)
        (role / "CLAUDE.md").write_text(_sized_doc(chars))
        assert _band_notice(role, k8s, over_ceiling=over_ceiling) is None, chars


def test_fixture_tree_with_no_roles_is_subject_gone(tmp_path):
    """The red half of `test_census_finds_its_named_members`: an empty tree yields no roles."""
    assert _role_dirs(BY_NAME["k8s"], roles_dir=tmp_path) == []


# ── The ceiling against the hook that delivers the doc ────────────────────────────────


@pytest.fixture(scope="module")
def hook():
    return load_inject_hook()


def _tracked() -> list[str]:
    listed = run(["git", "ls-files", "-z"], cwd=REPO, check=True).stdout
    return [p for p in listed.split("\0") if p]


def _role_docs_with_longest_trigger() -> list[tuple[str, str]]:
    """Every tracked role CLAUDE.md paired with the longest tracked path that selects it.

    The trigger is whichever path in the Bash command made the hook load the doc, so the
    longest path under the role is this tree's worst case for that doc's header.
    """
    tracked = _tracked()
    docs = sorted(
        p
        for p in tracked
        if p.startswith("ansible/roles/") and p.endswith("/CLAUDE.md")
    )
    assert "ansible/roles/k8s/pihole/CLAUDE.md" in docs, (
        "the role-doc census found no pihole doc — `git ls-files` returned something other "
        "than this repo's role tree, and every assertion below would pass vacuously"
    )
    pairs = []
    for doc in docs:
        role = doc.rsplit("/", 1)[0]
        under = [p for p in tracked if p.startswith(role + "/")]
        pairs.append((doc, max(under, key=len)))
    return pairs


def test_the_planes_cover_every_tracked_role_doc():
    """A role doc outside every row would escape the size and existence checks above."""
    covered = {
        str((d / "CLAUDE.md").relative_to(REPO))
        for plane in PLANES
        for d in _role_dirs(plane)
    }
    tracked = {doc for doc, _ in _role_docs_with_longest_trigger()}
    assert tracked <= covered, f"role docs no PLANES row walks: {tracked - covered}"


def _filler(chars: int) -> str:
    """`chars` characters over few lines, so only the char half of `_fits_inline` decides."""
    body = "# Role\n\nfiller words that stand in for prose "
    return body + "x" * (chars - len(body))


def test_a_doc_at_the_ceiling_is_inlined_whole(hook):
    """The ceiling's whole promise: a doc at it arrives whole, header and all.

    No trailing newline, and a header padded to exactly `HEADER_ALLOWANCE`: the worst shape
    `MAX_CHARS` has to cover, since `_fits_inline` rstrips the doc before measuring it.
    """
    text = _filler(MAX_CHARS)
    assert len(text) == MAX_CHARS and not text.endswith("\n")
    assert hook._fits_inline(text, "=" * HEADER_ALLOWANCE)


def test_a_doc_one_char_over_the_ceiling_is_not_inlined(hook):
    """The red case, and what makes the ceiling tight rather than merely safe."""
    assert not hook._fits_inline(_filler(MAX_CHARS + 1), "=" * HEADER_ALLOWANCE)


def test_the_header_allowance_covers_every_role_docs_longest_trigger(hook):
    """`HEADER_ALLOWANCE` is a reserve, so something has to fail when the tree outgrows it."""
    too_long = {
        doc: len(hook._header(doc, trigger))
        for doc, trigger in _role_docs_with_longest_trigger()
        if len(hook._header(doc, trigger)) > HEADER_ALLOWANCE
    }
    assert not too_long, (
        f"_doc_size.HEADER_ALLOWANCE reserves {HEADER_ALLOWANCE} chars and these docs' "
        f"longest trigger needs more: {too_long} — raise the allowance (every role doc's "
        f"ceiling drops by the same amount) or shorten the path"
    )


def test_every_role_doc_is_inlined_whole(hook):
    """The ceiling is only worth having if the hook agrees doc by doc.

    `MAX_CHARS` reserves the worst-case header, so this passes by a margin for a doc whose own
    paths are short. It fails for a doc over the ceiling whatever a row's `over_ceiling` says:
    those lists waive the census, not the hook.
    """
    truncated = [
        doc
        for doc, trigger in _role_docs_with_longest_trigger()
        if not hook._fits_inline((REPO / doc).read_text(), hook._header(doc, trigger))
    ]
    assert not truncated, (
        f"the inject hook injects these role docs as a head, not whole: {truncated} — move "
        f"history and measurements to the role's docs/ page"
    )


def test_the_live_hooks_budget_is_the_numbers_this_repo_was_measured_against():
    """Non-vacuity, and a second failure the derivation cannot give on its own.

    The derivation passes when the hook's budget moves, which is the edit that changes what
    every session reads without anyone re-reading the measurement behind it. Spelling all three
    numbers out here means moving the budget fails until someone writes the new ones down.
    """
    assert hook_inline_max_chars() == 7500
    assert hook_inline_budget() == 7272
    assert MAX_CHARS == effective_ceiling() == 7070


def test_a_hook_without_the_literal_is_flagged(tmp_path):
    hook = tmp_path / "inject-nested-docs.py"
    hook.write_text("BUDGET = 7500\n")
    with pytest.raises(AssertionError, match="no longer assigns INLINE_MAX_CHARS"):
        hook_inline_max_chars(hook)


def test_a_hook_with_the_literal_is_read(tmp_path):
    hook = tmp_path / "inject-nested-docs.py"
    hook.write_text('"""Doc."""\n\nINLINE_MAX_CHARS = 1234\nINLINE_MAX_LINES = 190\n')
    assert hook_inline_max_chars(hook) == 1234


def test_the_hook_this_ceiling_reads_is_the_one_the_harness_runs():
    assert INJECT_HOOK.exists()
