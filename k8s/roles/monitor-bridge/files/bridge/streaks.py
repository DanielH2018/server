"""The per-process hysteresis state every check carries across cycles, and the step that advances it.

`State` holds every consecutive-down counter and every throttled-probe cache a check body
mutates. `cli.main()` builds one `Sources` per process, and that `Sources` builds one `State`
as `src.state`, so the state lives exactly as long as the process: a bridge restart resets it,
as it always has. A check body reads it as `src.state.<field>` beside its config and its I/O,
and a test gets a zeroed `State` with every `FakeSources` it builds, so nothing has to clear or
patch a module global between tests (#3866).
"""

from dataclasses import dataclass, field
from typing import TypedDict


class ProbeCache(TypedDict):
    """A throttled probe's last verdict. `ts` None means never probed.

    An explicit sentinel rather than 0.0: "0 seconds since the epoch" is indistinguishable from
    a real timestamp by the cache arithmetic, and only the sheer size of a real time.time()
    keeps that from reading as a fresh cache entry on the first cycle.
    """

    ts: float | None
    ok: bool
    msg: str


class StampedCache(TypedDict):
    """A throttled probe's last verdict, seeded at `ts` 0.0 so the first cycle probes."""

    ts: float
    ok: bool
    msg: str


class B2ProbeCache(TypedDict):
    """The B2 gate's last verdict and how long to hold it.

    `ttl` is chosen per outcome by `checks.b2.b2_reachable`: a billed answer from B2 holds
    B2_PROBE_INTERVAL_S, a transport failure holds B2_TRANSPORT_RETRY_S. It seeds at 0, meaning
    "nothing is cached yet, probe now": the first cycle probes regardless, because `ts` is 0 and
    every real clock is further from it than any interval.
    """

    ts: float
    ok: bool
    msg: str
    ttl: float


class RegistrationCache(TypedDict):
    """The RDAP probe's last outcome and the registration expiry it last read.

    `ts` None means never probed. `expiry` is the Unix time of the registration's expiration
    event, kept through a failed probe, because a registry's answer going missing does not move
    the date it last gave.
    """

    ts: float | None
    ok: bool
    expiry: float | None


def _never_probed() -> ProbeCache:
    return {"ts": None, "ok": True, "msg": ""}


@dataclass
class State:
    """Every counter and cache a check body carries from one cycle to the next.

    Attributes:
      down_streaks: Consecutive-down count per check name, advanced through `down_streak()` and
        reset to 0 by each check on an `ok` result.
      n8n_streaks: Per-workflow consecutive-failure streaks (`checks.service.check_n8n`).
      b2_probe: The B2 gate's cache (`checks.b2.b2_reachable`).
      b2_storage: The B2 storage-usage cache (`checks.b2.b2_storage_usage`).
      r2_probe: The R2 usage cache (`checks.r2.r2_usage`).
      email_probe: The SMTP backstop cache (`checks.notify.email_backstop`).
      cloudflare_ips_probe: The Cloudflare allowlist drift cache (`checks.cloudflare_ips`).
      healthchecks_probe: The Healthchecks.io console drift cache (`checks.healthchecks`).
      domain_registration_probe: The RDAP registration-expiry cache
        (`checks.domain_registration`).
    """

    down_streaks: dict[str, int] = field(default_factory=dict)
    n8n_streaks: dict = field(default_factory=dict)
    b2_probe: B2ProbeCache = field(
        default_factory=lambda: {
            "ts": 0.0,
            "ok": True,
            "msg": "not yet probed",
            "ttl": 0.0,
        }
    )
    b2_storage: StampedCache = field(
        default_factory=lambda: {"ts": 0.0, "ok": False, "msg": "not yet probed"}
    )
    r2_probe: ProbeCache = field(default_factory=_never_probed)
    email_probe: StampedCache = field(
        default_factory=lambda: {"ts": 0.0, "ok": True, "msg": "not yet probed"}
    )
    cloudflare_ips_probe: ProbeCache = field(default_factory=_never_probed)
    healthchecks_probe: ProbeCache = field(default_factory=_never_probed)
    domain_registration_probe: RegistrationCache = field(
        default_factory=lambda: {"ts": None, "ok": False, "expiry": None}
    )


def down_streak(
    count: int,
    threshold: float,
    msg: str,
    grace_note: str,
    held_label: str = "down streak",
) -> tuple[int, bool, str]:
    """Pure consecutive-down hysteresis step shared by every per-check grace mechanism.

    Used by check_ha_heartbeat's and check_discord's per-check grace. Call on a DOWN result — the caller resets its own counter to 0 on
    `ok`. Increments `count` and returns (new_count, hold_ok, out_msg): while under
    `threshold` it holds `up` with a "<held_label> n/N (<grace_note>): msg" note; the
    `threshold`'th straight down pages with "msg (n cycles)".
    """
    count += 1
    if count < threshold:
        return (
            count,
            True,
            "%s %d/%d (%s): %s" % (held_label, count, threshold, grace_note, msg),
        )
    return count, False, "%s (%d cycles)" % (msg, count)
