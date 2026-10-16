"""The revert runbook and the shed scenario say their operator-only steps in a safe order.

Two documents, read as text, because every step they describe is operator-only and no test
can run them:

- *The revert runbook* in docs/volume-snapshot-drills.md, the procedure that follows
  `promote`'s stateful hold (D5.2c in docs/target-architecture/deploy/d5-bulk-move.md);
- the shed scenario in docs/break-glass.md, *one node is down and the survivor runs short of
  memory*, whose suspends and `DEPLOY_HOLD` writes follow the same rules (D5.1b).

Each clause below is a mistake that leaves the cluster in a worse state than the failure it
answers: a suspend Flux takes back at its next reconcile, a clear that erases a failure hold,
a one-role revert copied into Branch 1, a classify read compared with the wrong value, and a
wait on a field that never names a commit. Each clause has a red fixture made by mutating the
real document.

Run: uv run pytest ansible/tests/longhorn/test_volume_revert_runbook_text.py
"""

import re

import pytest

from lib.repo_paths import REPO

RUNBOOK_DOC = REPO / "docs" / "volume-snapshot-drills.md"
BREAK_GLASS = REPO / "docs" / "break-glass.md"

RUNBOOK_HEADING = "## The revert runbook"
BRANCH_1_HEADING = "### Branch 1"
SHED_HEADING = "### Scenario: one node is down and the survivor runs short of memory"
OFFLINE_BULLET = "- **When GitHub cannot be reached**"

FIELD_MANAGER = "--field-manager=homelab-operator"
HOLD_READ = "gh variable get DEPLOY_HOLD"
HOLD_CLEAR = "gh variable set DEPLOY_HOLD --body none"
ORIGIN_KEY = "org.opencontainers.image.revision"

_FENCE = re.compile(r"^\s*```")
_FENCED_BLOCK = re.compile(
    r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.MULTILINE | re.DOTALL
)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_LIST_ITEM = re.compile(r"^\s*(?:-|\d+\.)\s")
_KUBECTL_PATCH = re.compile(r"\bkubectl\b.*\bpatch\b")
_RESUME_OR_CLEAR = re.compile(
    r"\bresum\w*|--body none|\"suspend\":false", re.IGNORECASE
)


def section(text: str, heading: str) -> str:
    """The text under the first heading line that starts with `heading`.

    The section ends at the next heading of the same level or higher. Lines inside a fenced
    block are never headings, so a `#` comment in a shell block does not end it.

    Raises:
        AssertionError: no heading starts with `heading`, so a renamed heading fails the test
            rather than leaving every clause to pass over an empty string.
    """
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
    assert start is not None, f"no heading starting {heading!r}"
    end_heading = re.compile(r"^#{1,%d} " % level)
    in_fence = False
    for i in range(start + 1, len(lines)):
        if _FENCE.match(lines[i]):
            in_fence = not in_fence
        elif not in_fence and end_heading.match(lines[i]):
            return "\n".join(lines[start + 1 : i])
    return "\n".join(lines[start + 1 :])


def commands(text: str) -> list[str]:
    """Every command the text shows, each on one line.

    A fenced block gives one command per logical line, with its `\\` continuations joined. An
    inline code span gives one command, with the line breaks Markdown wraps it across
    collapsed. A line-based grep would miss both shapes.
    """
    found = []
    for block in _FENCED_BLOCK.findall(text):
        for line in block.replace("\\\n", " ").splitlines():
            if line.strip():
                found.append(" ".join(line.split()))
    prose = _FENCED_BLOCK.sub("", text)
    found.extend(" ".join(span.split()) for span in _INLINE_CODE.findall(prose))
    return found


def list_items(text: str) -> list[str]:
    """Each list item's own text, without its nested items, plus each plain paragraph."""
    items: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip():
            items.append([])
        elif _LIST_ITEM.match(line) or not items:
            items.append([line])
        else:
            items[-1].append(line)
    return [" ".join(" ".join(item).split()) for item in items if item]


def patches(text: str) -> list[str]:
    return [cmd for cmd in commands(text) if _KUBECTL_PATCH.search(cmd)]


def field_manager_problems(text: str) -> list[str]:
    """A hand patch under kubectl's default manager is reverted at Flux's next reconcile."""
    return [cmd for cmd in patches(text) if FIELD_MANAGER not in cmd]


def unread_clear_problems(shed: str) -> list[str]:
    """A shed clear must follow a read of the hold, or it can erase a failure hold."""
    first_read = shed.find(HOLD_READ)
    return [
        f"clear at offset {m.start()} with no `{HOLD_READ}` before it"
        for m in re.finditer(re.escape(HOLD_CLEAR), shed)
        if first_read < 0 or first_read > m.start()
    ]


def offline_branch(shed: str) -> str:
    """The shed scenario's offline bullet, with its indented continuation lines."""
    lines = shed.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith(OFFLINE_BULLET)),
        None,
    )
    assert start is not None, f"no offline branch starting {OFFLINE_BULLET!r}"
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines) and (
        not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent
    ):
        end += 1
    return "\n".join(lines[start:end])


def offline_order_problems(branch: str) -> list[str]:
    """In the offline branch, the hold is set before any resume or clear.

    Read sentence by sentence. A sentence that runs `gh variable set` without `--body none`
    sets the hold. Any other sentence that resumes or clears, before the hold is set, is a
    problem, because a queued `promote` run can start as soon as GitHub answers.
    """
    problems = []
    hold_set = False
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(branch.split())):
        if "gh variable set" in sentence and "--body none" not in sentence:
            hold_set = True
        elif _RESUME_OR_CLEAR.search(sentence) and not hold_set:
            problems.append(sentence)
    if not hold_set:
        problems.append("the offline branch never runs `gh variable set`")
    return problems


def branch_1_revert_command_problems(branch_1: str) -> list[str]:
    """Branch 1 runs the post's per-role blocks, so it must hold no revert command of its own."""
    problems = [cmd for cmd in commands(branch_1) if "volume-revert.yml" in cmd]
    if "ansible-playbook" in branch_1:
        problems.append("Branch 1 names `ansible-playbook`")
    return problems


def classify_problems(runbook: str) -> list[str]:
    """A step that reads `lastAttemptedRevision` names the `prod@sha256:` value it compares with."""
    return [
        item
        for item in list_items(runbook)
        if "lastAttemptedRevision" in item and "prod@sha256:" not in item
    ]


def last_good_wait_reads(branch_1: str) -> list[str]:
    return [cmd for cmd in commands(branch_1) if "ocirepository homelab" in cmd]


def last_good_wait_problems(branch_1: str) -> list[str]:
    """The wait for `last-good` reads the origin key, because `.status.artifact.revision` is a digest."""
    return [
        cmd
        for cmd in last_good_wait_reads(branch_1)
        if ORIGIN_KEY not in cmd.replace("\\", "")
        or ".status.artifact.revision}" in cmd
    ]


@pytest.fixture(scope="module")
def runbook() -> str:
    return section(RUNBOOK_DOC.read_text(), RUNBOOK_HEADING)


@pytest.fixture(scope="module")
def shed() -> str:
    return section(BREAK_GLASS.read_text(), SHED_HEADING)


def mutate(text: str, old: str, new: str) -> str:
    """`text` with `old` replaced once. Fails when `old` is absent, so no fixture is a no-op."""
    assert old in text, f"fixture anchor {old!r} is gone"
    return text.replace(old, new, 1)


def test_every_hand_patch_carries_the_field_manager(runbook: str, shed: str) -> None:
    # Branch 1 suspends and resumes; the shed scenario suspends and resumes.
    assert len(patches(runbook)) >= 2
    assert len(patches(shed)) >= 2
    assert field_manager_problems(runbook) == []
    assert field_manager_problems(shed) == []


def test_a_patch_without_the_field_manager_fails(runbook: str, shed: str) -> None:
    for text in (runbook, shed):
        red = mutate(text, f"\n      {FIELD_MANAGER} -p", "\n      -p")
        assert field_manager_problems(red)


def test_the_shed_clear_follows_a_read_of_the_hold(shed: str) -> None:
    assert HOLD_CLEAR in shed
    assert unread_clear_problems(shed) == []


def test_a_shed_clear_with_the_read_removed_fails(shed: str) -> None:
    """The read removed leaves the clear unconditional."""
    red = mutate(shed, f"`{HOLD_READ} -R DanielH2018/server`", "nothing")
    assert unread_clear_problems(red)


def test_the_offline_branch_sets_the_hold_before_any_resume(shed: str) -> None:
    assert offline_order_problems(offline_branch(shed)) == []


def test_an_offline_branch_with_a_resume_moved_first_fails(shed: str) -> None:
    branch = offline_branch(shed)
    red = mutate(
        branch,
        "skip this step",
        "resume each suspended tag. Then skip this step",
    )
    assert offline_order_problems(red)


def test_branch_1_holds_no_revert_command(runbook: str) -> None:
    branch_1 = section(runbook, BRANCH_1_HEADING)
    assert "revert line of each role's block in the" in branch_1
    assert branch_1_revert_command_problems(branch_1) == []


def test_a_one_role_revert_command_in_branch_1_fails(runbook: str) -> None:
    branch_1 = section(runbook, BRANCH_1_HEADING)
    red = mutate(
        branch_1,
        "Run the revert line of each role's block in the",
        "Run `uv run ansible-playbook ansible/volume-revert.yml -e volume_revert_service=<s> "
        "-e volume_revert_sha=<digest8>`, then the revert line of each role's block in the",
    )
    assert branch_1_revert_command_problems(red)


def test_the_classify_step_names_its_comparison_value(runbook: str) -> None:
    assert any("lastAttemptedRevision" in item for item in list_items(runbook))
    assert classify_problems(runbook) == []


def test_a_classify_step_without_the_comparison_value_fails(runbook: str) -> None:
    red = mutate(runbook, "`prod@sha256:<digest of sha-<R>>`", "R's SHA")
    assert classify_problems(red)


def test_the_last_good_wait_reads_the_origin_key(runbook: str) -> None:
    branch_1 = section(runbook, BRANCH_1_HEADING)
    assert last_good_wait_reads(branch_1)
    assert last_good_wait_problems(branch_1) == []


def test_a_last_good_wait_on_the_artifact_revision_fails(runbook: str) -> None:
    branch_1 = section(runbook, BRANCH_1_HEADING)
    red = mutate(
        branch_1,
        r"{.status.artifact.metadata.org\.opencontainers\.image\.revision}",
        "{.status.artifact.revision}",
    )
    assert last_good_wait_problems(red)
