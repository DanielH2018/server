"""The required checks that have failed on a head GitHub refused to merge; no wait turns them green.

GitHub refuses a direct merge in two forms. One names each required check that has not passed,
`Required status check "X" is failing.`, which `merge.refusal_reason` shortens to
`required check X failing`. The other names none: `N of M required status checks are <state>.`.
The landing logs carry that form only as `are in progress.`, but its wording says nothing about
the checks it does not count, so for it `find` reads the checks themselves.

That read is two REST calls: the ruleset's required contexts for the base branch, then the
head's latest check runs. Every required context is pinned to the GitHub Actions app
(`github_expected_ruleset_contexts`), so a commit status can never satisfy one and the check-runs
listing is enough. A read that fails names nothing, so the wait goes on as it would for a
refusal it cannot classify.
"""

import re

from lib import host_modules  # noqa: F401
from lib.json_types import as_list

from ci_verdict import RunState, parse_check_runs
from deploy_tools.land_lib import gh_read
from deploy_tools.land_lib.landing import BRANCH, Landing

_NAMED = re.compile(r"(?:^|; )required check (.+?) failing(?=;|$)")
_AGGREGATE = re.compile(r"(?:^|; )\d+ of \d+ required status checks ")


def find(ln: Landing, head: str, refusal: str) -> list[str]:
    """The required checks that have failed at `head`, as `refusal` names them or GitHub reads them.

    Args:
        ln: the landing, whose `read` waits out a rate limit without the turn.
        head: the SHA the refused merge was pinned to.
        refusal: the refusal as one line, from `merge.refusal_reason`.

    Returns:
        The names, sorted for an aggregate refusal; [] when no required check has failed, when
        the refusal is about something else, or when a read for an aggregate refusal failed.
    """
    named = _NAMED.findall(refusal)
    if named or not _AGGREGATE.search(refusal):
        return named

    def required() -> set[str]:
        url = f"repos/{{owner}}/{{repo}}/rules/branches/{BRANCH}"
        jq = '[.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context]'
        return {
            str(c)
            for c in as_list(ln.tools.gh_json("api", url, "--jq", jq), "gh api rules")
        }

    def latest() -> list:
        url = f"repos/{{owner}}/{{repo}}/commits/{head}/check-runs?per_page=100"
        return as_list(
            ln.tools.gh_json("api", url, "--jq", ".check_runs"), "gh api check-runs"
        )

    try:
        contexts = ln.read(required, "gh api rules")
        runs = parse_check_runs(ln.read(latest, "gh api check-runs"))
    except gh_read.GhReadFailed, ValueError:
        return []
    return sorted(
        {r.name for r in runs if r.name in contexts and r.state is RunState.FAIL}
    )
