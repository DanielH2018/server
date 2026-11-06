"""Close an issue a merged PR names in `closingIssuesReferences` when GitHub left it open.

GitHub listed #7291 as a closing reference of PR #7295 and never closed it, so two orchestrator
waves dispatched work master already held (#7397). The cause is unknown, so the landing checks
the result rather than the trigger.

`close_stranded` runs in step 1: a later step can crash or wait for hours while `findings.py next`
offers the issue, and a docs-only PR ends there. A failed read or close changes neither the verdict
token nor the exit code, which report the deploy. It goes on `Landing.warnings`, because the
`VERDICT:` line is all `cc-wait land` prints once a landing ends, through a lander unit too.

GitHub closes the issues a moment after the merge, so an open reference is read again after
`GRACE_S`. An issue reopened after the merge stays open, because someone chose that and a landing
can be re-run days later. An issue in another repo stays open, because `gh issue close <n>` would
close this repo's issue of the same number.
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
    return gh_read.read(
        lambda: as_object(ln.tools.gh_json(*args), label), ln.tools.sleep, label
    )


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
    gh_read.read(
        lambda: ln.tools.gh(
            "issue", "close", number, "--reason", "completed", "--comment", comment
        ),
        ln.tools.sleep,
        f"gh issue close {number}",
    )
