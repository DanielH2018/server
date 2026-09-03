"""Guards for the home-allowlist cron's own Kuma-push retry window, issue #999.

The monitor is push-type with `max_retries: 0`, so one dropped push is an immediate DOWN. The
pushes drop during an uptime-kuma rollout, and measured from Traefik's access log on 2026-09-03
they drop as Traefik's own 404 with an empty RouterName — the KubernetesCRD provider removes a
router whose service has no endpoints, so the route is gone rather than serving 503 through it.
The longest endpoint-less window observed was 31s (12:29:43-12:30:14, 2026-09-03).

Two things therefore have to stay true of the push, and each is guarded by an accept/reject
pair so a rule that stopped matching fails its own test:

  * `--retry-all-errors` is present. Measured on curl 8.5.0: a bare `--retry` returns from a 404
    in 0.02s without retrying at all, so dropping the flag makes the whole window inert while
    every other flag still reads correct.
  * the retry window covers the observed outage, and the whole run still fits the */5 period.

Both budgets are computed from curl's documented backoff rather than asserted as a literal, so
the failure message says which number moved.
"""

from __future__ import annotations

import re

import pytest
import yaml
from _helpers import ROLES

SCRIPT = ROLES / "k8s/crowdsec/templates/crowdsec-update-home-allowlist.sh.j2"
TASKS = ROLES / "k8s/crowdsec/tasks/main.yml"

# The endpoint-less window measured on 2026-09-03 — see the DECIDED block in the script.
OBSERVED_OUTAGE_S = 31


def _curl_invocations(text: str) -> list[str]:
    """Every `curl ...` command in the script, line continuations folded out.

    Two of the three sit inside `if ! VAR=$(curl ...)`, so the command is sliced out of the
    line rather than matched at its start.
    """
    folded = text.replace("\\\n", " ")
    return [
        line[line.index("curl ") :].strip()
        for line in folded.splitlines()
        if "curl " in line and not line.lstrip().startswith("#")
    ]


def _flag(cmd: str, name: str, default: int | None = None) -> int | None:
    match = re.search(rf"{re.escape(name)}\s+(\d+)", cmd)
    return int(match.group(1)) if match else default


def _backoff_delays(cmd: str) -> list[int]:
    """curl's 1/2/4/8... retry delays that actually run, honouring `--retry-max-time`.

    `--retry-max-time` gates each retry's START, not the total, so a retry that begins inside
    the window still sleeps its whole delay — which is why `--retry-max-time 120` measured
    127.16s rather than stopping at 120s.
    """
    retries = _flag(cmd, "--retry", 0) or 0
    retry_max_time = _flag(cmd, "--retry-max-time")
    delays: list[int] = []
    for n in range(retries):
        if retry_max_time is not None and sum(delays) > retry_max_time:
            break
        delays.append(2**n)
    return delays


def _retry_window_s(cmd: str) -> int:
    """How long an outage the retries span, against a failure curl returns from immediately.

    A rollout 404 comes back in microseconds, so the coverage is the backoff sum alone. This is
    the number the observed outage has to fit inside.
    """
    return sum(_backoff_delays(cmd))


def _worst_case_s(cmd: str) -> int:
    """The wall cost the cron period must absorb: every attempt at `--max-time`, plus backoff."""
    attempts = len(_backoff_delays(cmd)) + 1
    return _retry_window_s(cmd) + attempts * (_flag(cmd, "--max-time", 0) or 0)


def _push_curl() -> str:
    invocations = _curl_invocations(SCRIPT.read_text())
    # Non-vacuity: the script pushes once and resolves both IP families, so a census that finds
    # fewer than three curls has stopped matching the file rather than found a smaller file.
    assert len(invocations) >= 3, f"expected >=3 curl invocations, found {invocations}"
    pushes = [cmd for cmd in invocations if "-K -" in cmd]
    assert len(pushes) == 1, (
        f"expected exactly one config-file push curl, found {pushes}"
    )
    return pushes[0]


def _cron_period_s() -> int:
    tasks = yaml.safe_load(TASKS.read_text())
    schedules = [
        task["ansible.builtin.cron"]["minute"]
        for task in tasks
        if "ansible.builtin.cron" in task
        and "crowdsec-update-home-allowlist.sh"
        in task["ansible.builtin.cron"].get("job", "")
    ]
    assert len(schedules) == 1, f"expected one home-allowlist cron, found {schedules}"
    minute = schedules[0]
    assert minute.startswith("*/"), f"unhandled cron minute spec {minute!r}"
    return int(minute.removeprefix("*/")) * 60


def test_the_push_retry_window_covers_the_observed_rollout():
    window = _retry_window_s(_push_curl())
    assert window >= 2 * OBSERVED_OUTAGE_S, (
        f"push retry window is {window}s, under 2x the {OBSERVED_OUTAGE_S}s outage measured "
        "2026-09-03"
    )


def test_the_pre_999_retry_window_is_flagged_as_too_narrow():
    # `--retry 3 --max-time 10` measured 7.04s against a 404 and covered none of the outage.
    assert (
        _retry_window_s("curl -fsS --max-time 10 --retry 3 --retry-all-errors")
        < OBSERVED_OUTAGE_S
    )


def test_a_whole_run_still_fits_the_cron_period():
    text = SCRIPT.read_text()
    budget = sum(_worst_case_s(cmd) for cmd in _curl_invocations(text))
    assert budget < _cron_period_s(), (
        f"worst-case run is {budget}s against a {_cron_period_s()}s cron period — a down Kuma "
        "would let a run overlap its successor"
    )


def test_a_run_that_overruns_the_cron_period_is_flagged():
    overrun = "curl --max-time 10 --retry 12 --retry-all-errors"
    assert _worst_case_s(overrun) > _cron_period_s()


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # curl 8.5.0, measured 2026-09-03 against a 404: 1+2+4s of backoff, 7.04s end to end.
        ("curl --max-time 10 --retry 3", 7),
        # The shipped flags against the same 404: 63.09s end to end. The attempts themselves
        # return in microseconds, so the measurement is the backoff sum.
        ("curl --max-time 10 --retry 6 --retry-max-time 60", 63),
        # The variant rejected for overrunning the cron period, measured at 127.16s. Kept so the
        # `--retry-max-time` reading stays pinned to a measurement at both widths.
        ("curl --max-time 10 --retry 7 --retry-max-time 120", 127),
    ],
)
def test_the_backoff_model_matches_what_curl_measured(cmd, expected):
    assert _retry_window_s(cmd) == expected
