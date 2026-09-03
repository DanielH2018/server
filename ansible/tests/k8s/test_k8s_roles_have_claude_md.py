"""Every ansible/roles/k8s/ role directory carries a CLAUDE.md doc.

Written for issue: 33 of 63 k8s roles had no CLAUDE.md. A role's doc is the
first thing a session reads before touching that service (repo CLAUDE.md's
"Where to Look" table routes here), so a missing one means the session reads
the tasks/templates cold every time.

Red-proof pair: a fixture role with no CLAUDE.md is flagged by
`_check_role_doc`; a fixture role with an adequate one passes clean. Both
fixtures exercise the exact helper the real test uses, not a re-implementation
of its logic.
"""

from pathlib import Path

from _helpers import REPO

K8S_ROLES_DIR = REPO / "ansible" / "roles" / "k8s"

# manifests/ is the shared render -> apply -> queue role every other k8s role
# includes as a dependency (repo CLAUDE.md's "Where to Look" row points there
# for that contract). It has no standalone containers_list deploy tag of its
# own -- callers reach it via `include_role`, never `--tags manifests` -- and
# it is documented from the including roles' side, not as a deployed service.
EXCLUDED_DIRS = {"manifests"}

MIN_NON_BLANK_LINES = 8


def _role_dirs() -> list[Path]:
    return sorted(
        d
        for d in K8S_ROLES_DIR.iterdir()
        if d.is_dir() and d.name not in EXCLUDED_DIRS and not d.name.startswith(".")
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


def _check_role_doc(role_dir: Path) -> list[str]:
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
    if not _mentions_deploy_tag(role_dir.name, text):
        problems.append(
            f"{role_dir.name}: CLAUDE.md doesn't mention the role's deploy tag"
        )
    return problems


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
