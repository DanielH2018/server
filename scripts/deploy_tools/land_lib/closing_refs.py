"""Close an issue a merged PR names in `closingIssuesReferences` when GitHub left it open.

PR #7295 merged with GitHub listing #7291 as a closing reference, and #7291's timeline has no
`closed` event. The open issue stayed on the backlog, and two orchestrator waves dispatched
work master already held (#7397). The cause is unknown, so the landing checks the result rather
than the trigger.

`close_stranded` runs in step 1, straight after the merge commit resolves. A later step can crash,
or wait for hours while `findings.py next` still offers the issue, and a docs-only PR ends inside
step 1. It is best-effort: a failed read or close changes neither the verdict token nor the exit
code, which report the deploy. The failure goes on `Landing.warnings`, which `pipeline.run` appends
to the `VERDICT:` line's detail. That line is all `cc-wait land` prints once a landing ends, and
all it takes back from a lander unit, whose `== ` lines the next phase header displaces.

GitHub closes the issues a moment after the merge, so an open reference is read again after
`GRACE_S` before the landing closes it. An issue reopened after the merge stays open, because
someone chose that and a landing can be re-run days later. An issue in another repo also stays
open, because `gh issue close <n>` would close this repo's issue of the same number.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
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
          number
          state
          repository { nameWithOwner }
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
    number,
    state,
    repo: .repository.nameWithOwner,
    reopenedAt: .timelineItems.nodes[0].createdAt
  }]
}"""


def _read_refs(ln: Landing) -> JsonObject:
    """The PR's repo and merge time, and each closing reference's state, in `_FLATTEN`'s shape."""

    def call() -> JsonObject:
        got = ln.tools.gh_json(
            "api",
            "graphql",
            "-f",
            f"query={_QUERY}",
            "-F",
            "owner={owner}",
            "-F",
            "repo={repo}",
            "-F",
            f"pr={ln.opts.pr}",
            "--jq",
            _FLATTEN,
        )
        return as_object(got, "gh api graphql closing references")

    return gh_read.read(call, ln.tools.sleep, "gh api graphql closing references")


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
    """Close issue `number`, retried through a transient failure.

    A retry is safe: `gh issue close` on a closed issue exits 0 and posts no comment.
    """
    comment = (
        f"Fixed by PR #{ln.opts.pr}. GitHub left this issue open after the merge, "
        "so the landing closed it."
    )
    gh_read.read(
        lambda: ln.tools.gh(
            "issue", "close", number, "--reason", "completed", "--comment", comment
        ),
        ln.tools.sleep,
        f"gh issue close {number}",
    )
