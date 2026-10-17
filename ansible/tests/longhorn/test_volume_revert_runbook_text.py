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
HOLD_SET = "gh variable set DEPLOY_HOLD"
HOLD_CLEAR = "gh variable set DEPLOY_HOLD --body none"
ORIGIN_KEY = "org.opencontainers.image.revision"

_FENCE = re.compile(r"^\s*```")
_FENCED_BLOCK = re.compile(
    r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.MULTILINE | re.DOTALL
)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_LIST_ITEM = re.compile(r"^\s*(?:-|\d+\.)\s")
_KUBECTL_PATCH = re.compile(r"\bkubectl\b.*\bpatch\b")
_NEGATION = re.compile(r"\b(?:not|never|don't|skip\w*)\b", re.IGNORECASE)
_ORDER_WORD = re.compile(r"\b(?:after|before|once|until|first|then)\b", re.IGNORECASE)
_RESUME_OR_CLEAR = re.compile(
    r"\bresum\w*|--body none|\"suspend\":false", re.IGNORECASE
)
WAN_RETURNS = "Once the WAN returns,"
# The one resume mention the offline branch may hold: an ordering constraint on the set.
_LATER_RESUME = "before any resume"


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
    """In the offline branch, `DEPLOY_HOLD` is set before any resume or clear.

    Text position is not execution order: "set it only after you resume", "before running
    the set, resume" and "set it. Before that, resume" all put the resume first. So the
    check is structural rather than positional, and it rejects any wording it cannot rank:

    - The first action after "Once the WAN returns," is `gh variable set DEPLOY_HOLD`. The
      text between them holds no sentence end, no resume or clear, no ordering word and no
      negation.
    - The branch mentions a resume or clear only as "before any resume". Resuming is step
      4's job, so any other mention, in any sentence, is a problem.

    Only `gh variable set DEPLOY_HOLD` sets the hold; a set of any other variable, or one
    with `--body none`, does not count. A queued `promote` run can start as soon as GitHub
    answers, which is why the set must come first.
    """
    problems = []
    text = " ".join(branch.split())
    anchor = text.find(WAN_RETURNS)
    if anchor < 0:
        return [f"the offline branch never says {WAN_RETURNS!r}"]
    after_anchor = text[anchor + len(WAN_RETURNS) :]
    first_set = next(
        (
            m
            for m in re.finditer(re.escape(HOLD_SET), after_anchor)
            if "--body none" not in after_anchor[m.end() : m.end() + 40]
        ),
        None,
    )
    if first_set is None:
        problems.append(
            f"the offline branch never runs `{HOLD_SET}` after the WAN returns"
        )
    else:
        lead = after_anchor[: first_set.start()]
        if (
            re.search(r"[.!?;]", lead)
            or _RESUME_OR_CLEAR.search(lead)
            or _ORDER_WORD.search(lead)
            or _NEGATION.search(lead)
        ):
            problems.append(f"`{HOLD_SET}` is not the first action: {lead!r}")
    for m in _RESUME_OR_CLEAR.finditer(text):
        if not text[: m.end()].endswith(_LATER_RESUME):
            problems.append(
                f"a resume or clear the set may not precede: {text[max(0, m.start() - 40) : m.end()]!r}"
            )
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


SET = "run this step's `gh variable set DEPLOY_HOLD` first, before any resume and before step 4"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # A resume in its own sentence ahead of the set.
        ("skip this step", "resume each suspended tag. Then skip this step"),
        # A resume ahead of the set inside the same sentence.
        (
            "run this step's `gh variable set DEPLOY_HOLD` first",
            "resume each suspended tag, then run this step's `gh variable set DEPLOY_HOLD`",
        ),
        # The set negated, with the resume after it in the text.
        (
            "run this step's `gh variable set DEPLOY_HOLD` first",
            "do not run this step's `gh variable set DEPLOY_HOLD`; resume each suspended tag "
            "first",
        ),
        # Ordering words that put the resume first while the set comes first in the text.
        (
            SET,
            "run this step's `gh variable set DEPLOY_HOLD` only after you resume each suspended "
            "tag, and before step 4",
        ),
        (
            SET,
            "before running this step's `gh variable set DEPLOY_HOLD`, resume each suspended tag, "
            "then step 4",
        ),
        (
            SET,
            "run this step's `gh variable set DEPLOY_HOLD` once you have resumed each suspended tag",
        ),
        # A "before <noun>" ahead of a resume does not make the resume a later step.
        (
            SET,
            "before step 4 resume each suspended tag, then run this step's "
            "`gh variable set DEPLOY_HOLD`",
        ),
        # "after that" refers back to the resume.
        (
            SET,
            "resume each suspended tag, then after that run `gh variable set DEPLOY_HOLD`",
        ),
        # Ordering words across a sentence boundary.
        (
            SET,
            "run this step's `gh variable set DEPLOY_HOLD`. Before that, resume each suspended "
            "tag",
        ),
        (
            SET,
            "run this step's `gh variable set DEPLOY_HOLD`. But first resume each suspended tag",
        ),
        (
            "before step 4,",
            "before step 4. Before running it, resume each suspended tag,",
        ),
        # A set of another variable is not the hold.
        (
            "run this step's `gh variable set DEPLOY_HOLD` first",
            "run `gh variable set SOMETHING_ELSE --body x` first, then resume each suspended tag",
        ),
    ],
)
def test_an_offline_branch_with_a_resume_moved_first_fails(
    shed: str, old: str, new: str
) -> None:
    red = mutate(" ".join(offline_branch(shed).split()), old, new)
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
