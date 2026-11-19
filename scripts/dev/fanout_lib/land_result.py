"""How the fan-out reads the result of a landing `land.sh` ran: finished, or parked.

`review_land` reads the landing its own pipeline ran, and `status` reads one the server
lander ran, so the rule lives outside both. Every decision here comes from the fields of the
`LAND-RESULT:` line (`docs/landing.md`, *The `LAND-RESULT:` line*), never from a sentence.
"""

import re
from collections.abc import Mapping

# The verdicts after which the PR is merged and nothing more is owed. `deferred` is one: a host's
# updater applies its package, and cc-wait ends it with exit 4, not 0.
FINISHED_VERDICTS = frozenset({"settled", "nothing-to-deploy", "deferred"})
VERDICT_LINE = re.compile(r"(?m)^VERDICT: (\S+).*$")
_VERDICT_DETAIL = re.compile(r"^VERDICT: \S+ \((.*)\)$")
# land.sh's exit code for a stop that a re-run completes, as `LAND-RESULT:` prints it.
_GAVE_UP = "75"
# `promote_lib.wait.HOLD_OWN` and `HOLD_OTHER`, and `land_lib.outcome.Cause.PROMOTE_HELD`,
# restated because a batch's unit runs without the project's dependencies, which those import.
HOLD_OWN = "own"
HOLD_OTHER = "other"
PROMOTE_HELD = "promote-held"
# What a batch's final line opens with when its landing waits on a hold or a rule that is
# not its own. `status.PARKED_LINE` matches it.
PARKED = "parked:"


def park_line(result: Mapping[str, str], verdict_line: str = "") -> str:
    """The `parked:` line for a landing that waits on something not its own, or "".

    Read from the `LAND-RESULT:` fields alone, so rewording a landing message changes nothing
    here. Two landings park:

    - one a rule refused before its merge (`land_lib/slots.py`, exit 75), which merged nothing;
    - one that merged and ended `deploy-failed` behind a `DEPLOY_HOLD` whose failed names are
      not this PR's services. A hold that names no failure, or a PR with no service, parks
      too, marked `unattributed` for the root coordinator.

    A hold on this PR's own services does not park: its implementer is resumed.

    Args:
        result: the `LAND-RESULT:` fields, as `land_lib.detach.result_in` returns them.
        verdict_line: the landing's `VERDICT:` line, quoted as the reason of a refusal.
    """
    pr = result.get("pr", "")
    if result.get("verdict") == "refused" and result.get("exit") == _GAVE_UP:
        detail = _VERDICT_DETAIL.match(verdict_line)
        why = detail.group(1) if detail else "a landing rule"
        return (
            f"{PARKED} refused before merge ({why}); re-land with land.sh --pr {pr} "
            f"--arm-merge --await-merge --detach && cc-wait land {pr}"
        )
    owner = result.get("hold_owner")
    if result.get("cause") != PROMOTE_HELD or owner == HOLD_OWN:
        return ""
    text = (
        f"{PARKED} merged, unverified behind DEPLOY_HOLD {result.get('hold_sha', '-')}; "
        "re-land after cc-wait landing-clear"
    )
    if owner != HOLD_OTHER:
        text += "; unattributed, so the root coordinator triages the hold"
    return text
