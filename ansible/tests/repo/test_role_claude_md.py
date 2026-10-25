"""Every role on every plane carries a CLAUDE.md that names what a session needs first.

A session reads a role's doc before touching the role, so a missing one means reading its tasks
cold. `PLANES` has one row per role tree, and every check below runs once per row (#3384).

A row carries what differs between planes and nothing else:

- `deploy_tag`: the doc must name the role beside a literal `--tags`. Only k8s, where the
  deploy tag IS the role's directory name. Setup roles are applied through `initial_setup.yml`
  with tags that do not follow that convention, and both `common` roles are include-only.
- `floor` and `members`: non-vacuity. A row whose tree yields no roles fails with "subject gone:
  delete this row" instead of passing over nothing, and each row names roles a broken census
  must still find.

Role-doc size has no check here. The `DECIDED` block above `_doc_problems` records why, and
`.claude/hooks/inject-nested-docs.py --report` prints each doc's margin under the hook's budget.

The cron-contract half of the setup plane is a different property and stays in
`ansible/tests/setup/test_setup_cron_roles_have_a_contract.py`.

Run: uv run pytest ansible/tests/repo/test_role_claude_md.py
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from _helpers import CONTAINER_ROLES, K8S_ROLES, REPO, SETUP_ROLES, run
from _role_census import role_dirs

MIN_NON_BLANK_LINES = 8


@dataclass(frozen=True)
class Plane:
    name: str
    roles_dir: Path
    floor: int
    members: frozenset[str]
    deploy_tag: bool = False


PLANES = [
    # `manifests` is a member rather than an exclusion: the shared render role every other k8s
    # role includes still needs a doc (#3246). It meets the deploy-tag arm the way the other
    # tagless shared roles do, by saying in its doc that there is nothing to type.
    Plane(
        "k8s",
        K8S_ROLES,
        floor=60,
        members=frozenset({"pihole", "traefik", "manifests"}),
        deploy_tag=True,
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
        floor=2,
        members=frozenset({"wg-easy", "common"}),
    ),
]
PLANE_IDS = [p.name for p in PLANES]


def _non_blank_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _mentions_deploy_tag(role_name: str, text: str) -> bool:
    """True if some line names both the role and a literal `--tags` flag.

    The bare word "tag" matches "outage" and "voltage". A shared role with no standalone tag
    passes by saying so ("not `--tags longhorn-api`"), which is the fact a reader needs.
    """
    name = role_name.lower()
    return any("--tags" in line and name in line for line in text.lower().splitlines())


# DECIDED: role-doc size has no CI gate, by the operator's ruling of 2026-10-12 on #2826
# (https://github.com/DanielH2018/server/issues/2826#issuecomment-6130815021), also recorded
# on #2584 (https://github.com/DanielH2018/server/issues/2584#issuecomment-6130815242). The
# ruling retired the per-role-doc character ratchet that #2826 chose and its recorded-count
# `over_ceiling` lists. A doc over the inject hook's budget is not an error: the hook delivers
# it as its head plus an outline of the sections it cut. To see each doc's margin, run
# `uv run --no-sync python .claude/hooks/inject-nested-docs.py --report`. The ruling accepts
# that a role doc grown into a head defers a head rule in the same command, and that a revert
# must trim every doc grown past the old ceiling. The long form is
# `docs/target-architecture/docs-guard/g4-drop-size-test.md`.
def _doc_problems(role_dir: Path, plane: Plane) -> list[str]:
    """Every problem with role_dir's CLAUDE.md under the plane's rules ([] if adequate)."""
    doc = role_dir / "CLAUDE.md"
    if not doc.is_file():
        return [f"{role_dir.name}: no CLAUDE.md"]
    text = doc.read_text()
    problems = []
    if len(_non_blank_lines(text)) < MIN_NON_BLANK_LINES:
        problems.append(
            f"{role_dir.name}: CLAUDE.md has fewer than {MIN_NON_BLANK_LINES} non-blank lines"
        )
    if plane.deploy_tag and not _mentions_deploy_tag(role_dir.name, text):
        problems.append(
            f"{role_dir.name}: CLAUDE.md doesn't mention the role's deploy tag"
        )
    return problems


# ── The live tree, one row at a time ──────────────────────────────────────────────────


@pytest.mark.parametrize("plane", PLANES, ids=PLANE_IDS)
def test_census_finds_its_named_members(plane):
    """Non-vacuity, per python-layout.md's "a named member it must find" rule."""
    found = {d.name for d in role_dirs(plane.roles_dir, skip_dotted=True)}
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
    problems = [
        p
        for d in role_dirs(plane.roles_dir, skip_dotted=True)
        for p in _doc_problems(d, plane)
    ]
    assert not problems, "\n".join(problems)


# ── Fixture pairs, run against every row ──────────────────────────────────────────────

_TAG_LINE = 'Deploy: `--tags "widget"`.\n'
_LONG_ENOUGH = "# widget\n\n" + "".join(f"- line {i}\n" for i in range(8)) + _TAG_LINE


def _fixture_role(tmp_path, doc: str | None) -> Path:
    role = tmp_path / "widget"
    role.mkdir()
    if doc is not None:
        (role / "CLAUDE.md").write_text(doc)
    return role


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


def test_fixture_tree_with_no_roles_is_subject_gone(tmp_path):
    """The red half of `test_census_finds_its_named_members`: an empty tree yields no roles."""
    assert role_dirs(tmp_path, skip_dotted=True) == []


# ── The planes against the tracked tree ───────────────────────────────────────────────


def _tracked_role_docs() -> list[str]:
    """Every tracked role CLAUDE.md, as `git ls-files` lists it."""
    listed = run(["git", "ls-files", "-z"], cwd=REPO, check=True).stdout
    docs = sorted(
        p
        for p in listed.split("\0")
        if p.startswith("ansible/roles/") and p.endswith("/CLAUDE.md")
    )
    assert "ansible/roles/k8s/pihole/CLAUDE.md" in docs, (
        "the role-doc census found no pihole doc — `git ls-files` returned something other "
        "than this repo's role tree, and the assertion below would pass vacuously"
    )
    return docs


def test_the_planes_cover_every_tracked_role_doc():
    """A role doc outside every row would escape the existence and tag checks above."""
    covered = {
        str((d / "CLAUDE.md").relative_to(REPO))
        for plane in PLANES
        for d in role_dirs(plane.roles_dir, skip_dotted=True)
    }
    tracked = set(_tracked_role_docs())
    assert tracked <= covered, f"role docs no PLANES row walks: {tracked - covered}"
