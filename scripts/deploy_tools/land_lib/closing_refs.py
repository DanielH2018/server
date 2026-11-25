"""Close an issue a merged PR names in `closingIssuesReferences` when GitHub left it open.

GitHub can list an issue as a PR's closing reference and never close it, so `findings.py next`
offers work master already holds. The cause is unknown, so the landing checks the result
rather than the trigger.

`close_stranded` runs in step 1: a later step can crash or wait for hours while `findings.py next`
offers the issue, and a docs-only PR ends there. A failed read or close changes neither the verdict
token nor the exit code, which report the deploy. It goes on `Landing.warnings`, because the
`VERDICT:` line is all `cc-wait land` prints once a landing ends, through a lander unit too.

GitHub closes the issues a moment after the merge, so an open reference is read again after
`GRACE_S`. An issue reopened after the merge stays open, because someone chose that and a landing
can be re-run days later. An issue in another repo stays open, because `gh issue close <n>` would
close this repo's issue of the same number.

`refuse_stray` runs before the merge instead: it refuses a body whose closing keyword is
not its own `Closes #N` line, because GitHub closes the issue such a line names.
"""

import re

from lib.json_types import JsonObject, as_object, as_object_list
from deploy_tools.land_lib import gh_read
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import say

GRACE_S = 15

_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    nameWithOwner
    pullRequest(number: $pr) {
      mergedAt
      closingIssuesReferences(first: 50) {
        nodes {
          number state repository { nameWithOwner }
          timelineItems(itemTypes: [REOPENED_EVENT], last: 1) {
            nodes { ... on ReopenedEvent { createdAt } }
          }
        }
      }
    }
  }
}
"""

_FLATTEN = """.data.repository | {
  repo: .nameWithOwner,
  mergedAt: .pullRequest.mergedAt,
  issues: [.pullRequest.closingIssuesReferences.nodes[] | {
    number, state, repo: .repository.nameWithOwner, reopenedAt: .timelineItems.nodes[0].createdAt
  }]
}"""


def _read_refs(ln: Landing) -> JsonObject:
    """The PR's repo and merge time, and each closing reference's state, in `_FLATTEN`'s shape."""
    label = "gh api graphql closing references"
    fields = ("-F", "owner={owner}", "-F", "repo={repo}", "-F", f"pr={ln.opts.pr}")
    args = ("api", "graphql", "-f", f"query={_QUERY}", *fields, "--jq", _FLATTEN)
    return ln.read(lambda: as_object(ln.tools.gh_json(*args), label), label)


def _stranded(refs: JsonObject) -> list[JsonObject]:
    """The open references of `refs` the landing may close."""
    stranded = []
    for issue in as_object_list(refs.get("issues"), "closing references"):
        if issue.get("state") != "OPEN":
            continue
        number = issue.get("number")
        if issue.get("repo") != refs.get("repo"):
            say(
                f"#{number} in {issue.get('repo')} is still open; not closing another repo's issue"
            )
        elif str(issue.get("reopenedAt") or "") > str(refs.get("mergedAt") or ""):
            say(f"#{number} was reopened after the merge; leaving it open")
        else:
            stranded.append(issue)
    return stranded


def close_stranded(ln: Landing) -> None:
    """Close each issue GitHub left open after this merged PR; the caller holds a merged PR."""
    pr = ln.opts.pr
    try:
        if not _stranded(_read_refs(ln)):
            return
        ln.tools.sleep(GRACE_S)
        stranded = _stranded(_read_refs(ln))
    except (gh_read.GhReadFailed, ValueError) as exc:
        ln.warn(
            f"could not read PR #{pr}'s closing references ({exc}); "
            "check that each issue it closes is closed"
        )
        return
    for issue in stranded:
        number = issue.get("number")
        try:
            _close(ln, str(number))
        except gh_read.GhReadFailed as exc:
            ln.warn(
                f"could not close #{number}, which GitHub left open after PR #{pr} "
                f"merged ({exc}); run `uv run python scripts/dev/findings.py close {number} "
                f"--fixed --pr {pr}`"
            )
        else:
            say(f"closed #{number}, which GitHub left open after PR #{pr} merged")


def _close(ln: Landing, number: str) -> None:
    """Close issue `number`, retried: `gh issue close` on a closed issue exits 0 and posts nothing."""
    comment = (
        f"Fixed by PR #{ln.opts.pr}. GitHub left this issue open after the merge, "
        "so the landing closed it."
    )
    ln.read(
        lambda: ln.tools.gh(
            "issue", "close", number, "--reason", "completed", "--comment", comment
        ),
        f"gh issue close {number}",
    )


# A closing keyword anywhere in the body, with or without a colon, closes the issue: "not fixed: #N"
# closes #N.
_KEYWORD = r"close[sd]?|fix(?:e[sd])?|resolve[sd]?"
_CLOSING_REF = re.compile(rf"\b({_KEYWORD})\b:?\s+#(\d+)", re.IGNORECASE)
# One deliberate closing reference with trailing punctuation, so a second keyword on the same line
# is read against what is left.
_CANONICAL_REF = re.compile(rf"\b({_KEYWORD})\b:?\s+#\d+[.,;:)\]]*\s*", re.IGNORECASE)
# Markdown that carries no meaning before a reference; GitHub creates no reference inside a code
# span.
_DECORATION = re.compile(r"^[\s>*_#`~-]+|[\s>*_#`~-]+$")
_SENTENCE_END = re.compile(r"[.!?]$")


def _reference_is_deliberate(prefix: str) -> bool:
    """Whether a closing keyword preceded by `prefix` on its line is the intended kind.

    Deliberate means the keyword opens a clause of its own: it starts the line, follows
    another closing reference, or follows a finished sentence. `Filed and not fixed: #N`
    fails all three.
    """
    rest = _DECORATION.sub("", _CANONICAL_REF.sub("", prefix))
    return not rest or _SENTENCE_END.search(rest) is not None


def stray_closing_refs(body: str) -> list[str]:
    """The lines of `body` whose closing keyword is not a deliberate closing reference.

    GitHub reads the `fixed: #N` in a body that says "Filed and not fixed: #N" as a closing
    keyword, and closes the unfixed follow-up on merge, which drops it from
    `findings.py list` until someone reopens it by hand.

    Set membership cannot tell the two apart: the intentional form and the accidental one are
    the same construct, so a closing reference the PR does not intend looks exactly like one it
    does. POSITION is the discriminator — a keyword that opens a clause is the convention every
    PR body here writes, and one buried mid-sentence is the accident. A body naming an unfixed
    follow-up writes it without a keyword in front of the number: `Filed for later: #N`.

    The clause test is what the corpus forces. A keyword-opens-the-LINE rule flags the repo's
    ordinary `Closes #A. Closes #B.` on one line, and a guard that refuses a fifth of all
    landings is one the operator turns off. Flagging only a keyword that opens no clause
    avoids that.

    Known limits, both deliberate. A keyword inside a fenced code block is still flagged:
    over-flagging costs one `gh pr edit` and under-flagging costs a silently closed finding.
    And only the bare `#N` form is matched, not `owner/repo#N` or a full issue URL, which
    GitHub also closes on — no agent or template here writes either.

    A pure function of one string so the branch is testable without gh.

    Args:
        body: the PR body, as `gh pr view --json body` returns it.

    Returns:
        The stripped offending lines, in order, without duplicates.
    """
    found: list[str] = []
    for line in body.splitlines():
        for match in _CLOSING_REF.finditer(line):
            if _reference_is_deliberate(line[: match.start()]):
                continue
            if line.strip() not in found:
                found.append(line.strip())
    return found


def refuse_stray(ln: Landing, body: str) -> None:
    """Die when the PR body carries a closing reference that is not its own `Closes #N` line.

    Runs beside `_require_author`, before any merge call, because the damage happens AT the
    merge and is not undone by one: reopening the issue is a hand step the operator only takes
    once they notice. The session that hits this is usually not the session that wrote the
    body — the fan-out agent opens the PR on daniel-server and daniel-box lands it — so the
    message says what to rewrite rather than assuming the reader chose the wording.
    """
    stray = stray_closing_refs(body)
    if not stray:
        return
    lines = "\n  ".join(stray)
    ln.die(
        f"PR #{ln.opts.pr}'s body carries a closing keyword that is not a `Closes #N` line, "
        f"so merging it closes an issue this PR may not have fixed (issue #2513):\n"
        f"  {lines}\n"
        "Rewrite the reference without a closing keyword in front of the number — "
        '"Filed for later: #N" — or move a deliberate close onto its own `Closes #N` line, '
        "then `gh pr edit` the body and re-run this.",
        1,
    )
