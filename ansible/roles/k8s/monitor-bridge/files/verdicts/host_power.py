"""Power and throttling verdicts — the Pi's undervoltage alarm, and kernel CPU thermal throttling.

Its own module rather than a section of `verdicts/host.py` for the reason
`verdicts/host_cgroups.py` records: that file sits at 598 lines against the 600-line cap in
`ansible/tests/repo/test_module_length_ratchet.py`. Same split idiom as `checks/host_edge.py`
and `checks/host_thermal.py`.

The first two verdicts are read by `check_host_temp`'s two newer arms (issue #1471). They share
that monitor rather than having their own, so the naming stays "host temperature" on the Kuma
side — see `checks/host_thermal.py` for why folding beat a new push token.
`thermal_monitor_verdict` is the composer that decides which of that monitor's four arms
reaches Kuma, split out here so the propagation has a test (issue #1547).

Decides; does not fetch. Takes its inputs as arguments and reads no module-level config — see
`bridge/parsing.py`'s header for the rule and why breaking it fails silently rather than loudly.
"""

from verdicts.host import _HWMON_MAX_LISTED, _hwmon_sensor_key, hwmon_display_name


def undervoltage_verdict(
    alarms: list[tuple[dict, float]],
    names: tuple[dict[tuple[str, str], str], dict[tuple[str, str, str], str]]
    | None = None,
) -> tuple[bool, str]:
    """Pure: (ok, msg) over node_hwmon_in_lcrit_alarm_volts.

    The Raspberry Pi firmware's own low-critical voltage alarm, a clean 0/1. A 1 means the 5V
    supply fell below its critical threshold, which corrupts SD cards and stalls USB devices —
    and unlike a temperature reading there is no threshold to choose, so this verdict has no
    tunable. daniel-pi is the only host that reports the sensor.

    An EMPTY list is not ok here, for the same reason hwmon_temp_verdict refuses one: the sensor
    exists on exactly one host, so an empty vector means that host stopped answering — and a Pi
    falling off the network is what sustained undervoltage causes. A "no alarm" verdict from zero
    readings would be green on precisely the condition this arm exists to catch. The caller
    decides whether an empty vector is a Pi-scrape outage another monitor owns; see
    `checks.host_thermal._undervoltage_arm`.
    """
    if not alarms:
        return False, "no undervoltage alarm sensor scraped (Pi node-exporter blind?)"
    asserted = sorted(
        hwmon_display_name(_hwmon_sensor_key(labels), names)
        for labels, value in alarms
        if value > 0.5
    )
    if not asserted:
        return True, "no undervoltage alarm (%d sensor(s))" % len(alarms)
    return False, (
        "undervoltage alarm asserted on %s — the 5V supply fell below its critical "
        "threshold, which corrupts SD cards and stalls USB devices"
        % ", ".join(asserted[:_HWMON_MAX_LISTED])
    )


def thermal_throttle_verdict(
    states: list[tuple[dict, float]], origins_min: int
) -> tuple[bool, str]:
    """Pure: (ok, msg) over node_cooling_device_cur_state{type="Processor"}.

    A non-zero cur_state means the kernel is actively throttling the CPU right now. That is a
    different fault from `check_cpu_throttle`, which reads CFS throttling — a cgroup quota being
    hit, not heat — and from `hwmon_temp_verdict`, which sees the temperature but not the
    kernel's response to it. A CPU can sit below its declared max and still be throttled, if the
    limit the firmware enforces is lower than the one the driver declares.

    Two ways to be not-ok, and the coverage arm is the one that stops this being inert:

    - an EMPTY vector means no host publishes a Processor cooling device, which on this estate
      means the collector went blind rather than that nothing is throttling;
    - fewer than `origins_min` distinct `origin` labels means a host that DOES publish these
      series stopped answering, and the remaining hosts would otherwise report "not throttling"
      for the whole estate. daniel-pi publishes none of these by design, so the floor is the two
      amd64 nodes rather than the three hosts the temperature arm counts.

    The throttling report leads and the coverage tally trails it, matching hwmon_temp_verdict:
    a host that IS reporting and IS throttled is the more actionable of the two.
    """
    if not states:
        return False, "no Processor cooling-device series scraped (collector blind?)"
    origins = {
        labels.get("origin", labels.get("instance", "?")) for labels, _v in states
    }
    throttled = sorted(
        {
            labels.get("origin", labels.get("instance", "?"))
            for labels, value in states
            if value > 0
        }
    )
    coverage = "%d cooling device(s) across %d host(s)" % (len(states), len(origins))
    if throttled:
        return False, "CPU thermally throttled on %s; %s" % (
            ", ".join(throttled[:_HWMON_MAX_LISTED]),
            coverage,
        )
    if len(origins) < origins_min:
        return False, (
            "only %d of %d expected host(s) report a Processor cooling device (%s) — "
            "the rest are unmonitored for throttling, not idle; %s"
            % (
                len(origins),
                origins_min,
                ", ".join(sorted(origins)),
                coverage,
            )
        )
    return True, "not throttling; %s" % coverage


def ups_on_battery_verdict(on_battery: float | None) -> tuple[bool, str] | None:
    """Pure: (ok, msg) over NUT's `ups.status{flag="OB"}`, or None when there is nothing to say.

    Mains power is gone and the UPS is carrying the load. Distinct from the charge and runtime
    arms, which read the battery's RUNWAY: those can sit at 100% and 20 minutes through an
    outage that is about to become a shutdown, and read green the whole way down until the
    runway collapses. This arm is the outage itself.

    None covers both quiet cases — the arm is unconfigured, or mains power is fine — for the
    same reason `_undervoltage_arm` returns None on a clean cycle: a clean arm must not append a
    note to the up message, or an ordinary cycle stops reading like one. An absent series is
    also None here rather than not-ok, because the flag is one-hot over `flag` and its absence
    means the whole exporter went quiet — which the all-arms-absent branch in `check_ups` and
    the nut pod's liveness probe already own between them.

    No grace of its own is applied here; the caller rides UPS_CONSECUTIVE, so a brownout shorter
    than the grace window never pages.
    """
    if on_battery is None or on_battery <= 0.5:
        return None
    return False, "UPS on battery (NUT ups.status OB) — mains power is gone"


def thermal_monitor_verdict(
    undervoltage: tuple[bool, str] | None,
    temperature: tuple[bool, str] | None,
    throttle: tuple[bool, str] | None,
    coverage: tuple[bool, str] | None,
    temperature_msg: str,
) -> tuple[bool, str]:
    """Pure: compose `check_host_temp`'s four arms into the one verdict its monitor reports.

    Each arm arrives already decided, as `(ok, msg)` or None for "nothing to say". The check
    fetches and evaluates; this decides the ORDER the arms speak in and which of them reaches
    the monitor. Splitting it out is what gives propagation a direct test (issue #1547): the
    arms had accept/reject pairs of their own, but nothing could see a missing `return` at a
    call site, because the structural test reads `co_names` and a deleted return leaves the
    name in place.

    `temperature` follows the same convention as the other arms and is None exactly when the
    hwmon verdict was clean. It is NOT None when that verdict was red, whether the thermal-spike
    grace is still holding (ok True) or has expired (ok False) — either way the temperature arm
    is speaking and it short-circuits everything after it, which is why an `ok` arm can still
    end the composition here. `temperature_msg` is the clean verdict's message; it leads the
    up-path message and is unread on every other path.

    The order, and what each step suppresses:

      1. an asserted undervoltage alarm returns alone. The firmware has already decided, and the
         damage it does is not undone by cooling down.
      2. a red temperature verdict returns alone. The caller has not fetched the throttle arm on
         this path, so `throttle` is None here by construction rather than by choice.
      3. a red throttle arm returns alone.
      4. a coverage shortfall returns last of the not-ok arms: a host that IS reporting and IS
         too hot outranks a complaint about the absent one.
      5. otherwise up, with the clean temperature message leading and only the arms still
         HOLDING inside their own grace appending a note. A clean arm passes None and adds
         nothing, so an ordinary cycle reads exactly as it did before any of these arms existed.
    """
    if undervoltage is not None and not undervoltage[0]:
        return undervoltage
    if temperature is not None:
        return temperature
    if throttle is not None and not throttle[0]:
        return throttle
    if coverage is not None:
        return coverage
    notes = [temperature_msg] + [
        arm[1] for arm in (undervoltage, throttle) if arm is not None
    ]
    return True, "; ".join(notes)
