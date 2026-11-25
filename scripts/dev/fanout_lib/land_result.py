"""How the fan-out reads the result of a landing `land.sh` ran: finished, or parked.

`review_land` reads the landing its own pipeline ran, and `status` reads one the server
lander ran, so the rule lives outside both. Every decision here comes from the fields of the
`LAND-RESULT:` line (`docs/landing.md`, *The `LAND-RESULT:` line*), never from a sentence.
"""

import re
from collections.abc import Mapping

# `deferred` is finished: a host's updater applies its package, and cc-wait ends it with exit 4, not
# 0.
FINISHED_VERDICTS = frozenset({"settled", "nothing-to-deploy", "deferred"})
VERDICT_LINE = re.compile(r"(?m)^VERDICT: (\S+).*$")
_VERDICT_DETAIL = re.compile(r"^VERDICT: \S+ \((.*)\)$")
# land.sh's exit code for a stop that a re-run completes, as `LAND-RESULT:` prints it.
_GAVE_UP = "75"
# Restated from `promote_lib.wait` and `land_lib.outcome` because a batch's unit runs without the
# project's dependencies those import.
HOLD_OWN = "own"
HOLD_OTHER = "other"
PROMOTE_HELD = "promote-held"
LANDING_LIVE = "landing-live"
PARKED = "parked:"


def reland_command(pr: str) -> str:
    """The command that lands PR number `pr` again and waits for its verdict."""
    return f"land.sh --pr {pr} --arm-merge --await-merge --detach && cc-wait land {pr}"


def last_verdict(text: str) -> tuple[str, str]:
    """The token and the whole of a landing log's last `VERDICT:` line; "" twice for none."""
    matches = list(VERDICT_LINE.finditer(text))
    return (matches[-1].group(1), matches[-1].group(0)) if matches else ("", "")


def another_has_it(result: Mapping[str, str]) -> bool:
    """Whether `land.sh` started no landing because another landing of the PR is live."""
    return result.get("cause") == LANDING_LIVE


def park_line(result: Mapping[str, str], verdict_line: str = "") -> str:
    """The `parked:` line for a landing that waits on something not its own, or "".

    Read from the `LAND-RESULT:` fields alone, so rewording a landing message changes nothing
    here. Three landings park:

    - one that never started, because another landing of the PR is live
      (`land_lib/pr_lock.py`, exit 75). That landing's verdict is the PR's, and the line
      quotes the command that waits on it;
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
    detail = _VERDICT_DETAIL.match(verdict_line)
    if another_has_it(result):
        why = detail.group(1) if detail else f"wait with cc-wait land {pr}"
        return (
            f"{PARKED} another landing has it, so none started; do not re-land ({why})"
        )
    if result.get("verdict") == "refused" and result.get("exit") == _GAVE_UP:
        why = detail.group(1) if detail else "a landing rule"
        return (
            f"{PARKED} refused before merge ({why}); re-land with {reland_command(pr)}"
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
