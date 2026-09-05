"""The consecutive-down counter shared by every check that holds `up` through a transient.

`_down_streaks` is mutated by checks in four domains (host, service, notify, storage) and
cleared between tests by conftest.py's autouse fixture, so it lives in its own module rather
than in whichever domain moved out of check.py first. Callers reach it as
`bridge.streaks.down_streak(...)` / `bridge.streaks._down_streaks[...]`; the tests patch and
clear it here. `_grace_streaks` and `apply_startup_grace` moved here from check.py on
2026-09-04, which is where this header always said they were.
"""

# Per-check consecutive-down count (check_ups/check_ha_heartbeat/check_discord/
# check_longhorn_volumes), keyed by check name, mutated via down_streak(). Reset to 0 on an
# `ok` result by each check itself, and cleared between tests by conftest.py's autouse
# fixture. Distinct from _grace_streaks below, which is apply_startup_grace's per-name
# state for the reach-out checks' post-reboot startup grace, a different mechanism keyed
# by a disjoint set of names.
_down_streaks: dict[str, int] = {}


def down_streak(
    count: int,
    threshold: float,
    msg: str,
    grace_note: str,
    held_label: str = "down streak",
) -> tuple[int, bool, str]:
    """Pure consecutive-down hysteresis step shared by every per-check grace mechanism.

    Used by check_ha_heartbeat's, check_ups's and check_discord's per-check grace, plus
    apply_startup_grace. Call on a DOWN result — the caller resets its own counter to 0 on
    `ok`. Increments `count` and returns (new_count, hold_ok, out_msg): while under
    `threshold` it holds `up` with a "<held_label> n/N (<grace_note>): msg" note; the
    `threshold`'th straight down pages with "msg (n cycles)". (check_cpu_throttle keeps its
    own down branch — its page message embeds the throttle thresholds, so it can't use the
    generic format.)
    """
    count += 1
    if count < threshold:
        return (
            count,
            True,
            "%s %d/%d (%s): %s" % (held_label, count, threshold, grace_note, msg),
        )
    return count, False, "%s (%d cycles)" % (msg, count)


# apply_startup_grace's per-name state for the reach-out checks' post-reboot startup grace
# (STARTUP_GRACE in check.py). Keyed by a set of names disjoint from _down_streaks', and a
# different mechanism: this one holds `up` through the first GRACE_CYCLES-1 cycles after the
# bridge itself starts, rather than through a transient at any time.
_grace_streaks: dict[str, int] = {}


def apply_startup_grace(
    name: str, ok: bool, msg: str, threshold: float, streaks: dict[str, int]
) -> tuple[bool, str]:
    """Pure: hold a reach-out check `up` through the first `threshold`-1 consecutive down cycles.

    `streaks` is a name->consecutive-down-count dict, mutated in place. An `ok` result resets the
    count; a down result advances the shared `down_streak` hysteresis, so a held cycle reads with the
    same "down streak n/N" / "(n cycles)" wording as the HA/UPS/Discord per-check grace.
    """
    if ok:
        streaks[name] = 0
        return ok, msg
    streaks[name], ok, msg = down_streak(
        streaks.get(name, 0), threshold, msg, "startup/redeploy grace"
    )
    return ok, msg
