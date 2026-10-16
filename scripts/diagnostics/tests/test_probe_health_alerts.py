"""`probe.py health`'s alert plane (M6 B-1): the fixtures the plan lists, each a red/green pair.

The fake Prometheus evaluates the label matchers of each selector against a list of series,
so a case that passes because of `shadow!="true"` or `alertname!="Watchdog"` passes because
the selector excluded the series, not because the fake never returned it.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_alerts.py
"""

import re

import pytest

from deploy_tools.deploy_lib import detach_notify
from diagnostics.probe_lib import health, health_alerts

TAG = "littlelink"
APPLIED = 1_000_000.0
PROM_START = 900_000.0
_MATCHER = re.compile(r'(\w+)(!?=)"([^"]*)"')


class FakeProm:
    """A Prometheus holding `series`, with a clock that advances only when the gate sleeps.

    Each series is `(name, labels, value, sample_ts)`. `value` and `sample_ts` may be callables
    of the clock, so a probe can read 0 now and 1 after the gate waits.
    """

    def __init__(self, series=(), flux_first_report=None):
        self.series: list = list(series)
        self.flux_first_report = flux_first_report
        self.now = APPLIED + 30

    def _resolve(self, v):
        return v(self.now) if callable(v) else v

    def query(self, promql):
        if promql.startswith("min(min_over_time"):
            fr = self.flux_first_report
            return [] if fr is None else [{"metric": {}, "value": [self.now, str(fr)]}]
        stamp = promql.startswith("timestamp(")
        name, _, body = promql.removeprefix("timestamp(").partition("{")
        rows = []
        for sname, labels, value, ts in self.series:
            if sname != name or not all(
                (labels.get(k, "") == v) == (op == "=")
                for k, op, v in _MATCHER.findall(body)
            ):
                continue
            out = self._resolve(ts) if stamp else self._resolve(value)
            rows.append(
                {"metric": {"__name__": name, **labels}, "value": [self.now, str(out)]}
            )
        return rows

    def sleep(self, seconds):
        self.now += seconds

    def tools(self):
        return health_alerts.PromTools(
            query=self.query, sleep=self.sleep, clock=lambda: self.now
        )


def alert(active, service=TAG, **labels):
    """The ALERTS and ALERTS_FOR_STATE pair for one firing alert activated at `active`."""
    labels = {
        "alertname": "SomethingBroke",
        "severity": "critical",
        "service": service,
        **labels,
    }
    return [
        ("ALERTS", {**labels, "alertstate": "firing"}, 1, active),
        ("ALERTS_FOR_STATE", labels, active, active),
    ]


START = [("process_start_time_seconds", {"job": "prometheus"}, PROM_START, PROM_START)]


def probe(value, ts):
    labels = {
        "service": TAG,
        "instance": "https://www.local.example",
        "job": "blackbox-ingress",
    }
    return [("probe_success", labels, value, ts)]


def run(fake, reference=APPLIED, probe_reference=APPLIED, has_route=True):
    return health_alerts.gate(TAG, reference, probe_reference, has_route, fake.tools())


# --- Alerts ------------------------------------------------------------------------------


def test_a_critical_alert_active_after_the_apply_that_still_fires_is_flagged():
    code, lines = run(FakeProm(alert(APPLIED + 5) + START))
    assert code == 1
    assert any(
        "SomethingBroke{severity=critical} firing since" in line for line in lines
    )


def test_a_critical_alert_that_resolves_by_the_re_read_is_clean():
    fake = FakeProm(alert(APPLIED + 5) + START)
    tools = fake.tools()

    def sleep_and_resolve(seconds):
        fake.sleep(seconds)
        fake.series = START

    tools = health_alerts.PromTools(
        query=tools.query, sleep=sleep_and_resolve, clock=tools.clock
    )
    assert health_alerts.gate(TAG, APPLIED, APPLIED, False, tools)[0] == 0


def test_a_warning_alert_labelled_verify_active_after_the_apply_is_flagged():
    code, _ = run(
        FakeProm(alert(APPLIED + 5, severity="warning", verify="true") + START)
    )
    assert code == 1


def test_a_warning_alert_without_verify_is_clean_and_noted():
    code, lines = run(FakeProm(alert(APPLIED + 5, severity="warning") + START))
    assert code == 0
    assert any("outside the gated set" in line for line in lines)


def test_a_critical_alert_active_before_the_apply_is_clean():
    code, lines = run(FakeProm(alert(APPLIED - 5) + START))
    assert code == 0
    assert any("before the reference" in line for line in lines)


@pytest.mark.parametrize(
    "series",
    [
        alert(APPLIED + 5, alertname="Watchdog", severity="critical"),
        alert(APPLIED + 5, shadow="true"),
        alert(APPLIED + 5, service="observability", drill="true"),
    ],
    ids=["watchdog", "shadow", "drill-on-another-tag"],
)
def test_an_alert_outside_the_selector_is_clean(series):
    assert run(FakeProm(series + START))[0] == 0


def test_with_no_reference_time_every_gated_alert_is_flagged():
    assert run(FakeProm(alert(1.0) + START), reference=None)[0] == 1


@pytest.mark.parametrize("offset", [0, 7], ids=["equal", "one-evaluation-later"])
def test_an_alert_activated_at_prometheus_start_is_flagged_as_age_unknown(offset):
    fake = FakeProm(alert(PROM_START + offset) + START)
    code, lines = run(fake, reference=PROM_START - 100)
    assert code == 1
    assert any(
        "since Prometheus started" in line and "age is unknown" in line
        for line in lines
    )


def test_an_alert_activated_long_after_prometheus_start_is_not_called_age_unknown():
    _, lines = run(FakeProm(alert(APPLIED + 5) + START))
    assert not any("age is unknown" in line for line in lines)


# --- The flux reference ------------------------------------------------------------------

# peanut's release record froze at its last Ansible apply, a month before APPLIED.
_MONTH_OLD = "2026-09-16T00:00:00Z"


def test_a_flux_tag_alert_before_its_kustomization_revision_is_clean():
    fake = FakeProm(flux_first_report=APPLIED)
    reference = health_alerts.alert_reference("peanut", "flux", _MONTH_OLD, fake.query)
    assert reference == APPLIED
    fake.series = alert(APPLIED - 60, service="peanut") + START
    gate = health_alerts.gate("peanut", reference, APPLIED, False, fake.tools())
    assert gate[0] == 0


def test_a_flux_tag_alert_after_its_kustomization_revision_is_flagged():
    fake = FakeProm(flux_first_report=APPLIED)
    reference = health_alerts.alert_reference("peanut", "flux", _MONTH_OLD, fake.query)
    fake.series = alert(APPLIED + 60, service="peanut") + START
    gate = health_alerts.gate("peanut", reference, APPLIED, False, fake.tools())
    assert gate[0] == 1


def test_an_ansible_tag_reference_is_its_applied_at():
    reference = health_alerts.alert_reference(TAG, None, _MONTH_OLD, FakeProm().query)
    assert reference == health_alerts.epoch(_MONTH_OLD)


# --- Probes ------------------------------------------------------------------------------


def test_a_probe_reading_0_after_the_rollout_is_flagged():
    code, lines = run(FakeProm(probe(0, lambda now: now - 1)))
    assert code == 1
    assert any("reads probe_success 0" in line for line in lines)


def test_a_probe_with_no_sample_newer_than_the_rollout_after_the_wait_is_flagged():
    code, lines = run(FakeProm(probe(1, APPLIED - 1)))
    assert code == 1
    assert any("reported no sample after" in line for line in lines)


def test_a_mid_rollout_0_followed_by_a_1_is_clean():
    first_scrape = APPLIED + 50
    fake = FakeProm(
        probe(
            lambda now: 1 if now >= first_scrape else 0,
            lambda now: first_scrape if now >= first_scrape else APPLIED - 10,
        )
    )
    assert run(fake)[0] == 0


def test_a_tag_with_no_probe_series_is_clean():
    code, lines = run(FakeProm())
    assert code == 0
    assert any("no probe_success series" in line for line in lines)


def test_a_tag_with_no_route_and_an_old_probe_series_is_clean():
    code, lines = run(FakeProm(probe(0, APPLIED - 1)), has_route=False)
    assert code == 0
    assert any("renders no ingress route" in line for line in lines)


def test_pods_ready_at_is_the_newest_ready_transition():
    def pod(status, when):
        cond = {"type": "Ready", "status": status, "lastTransitionTime": when}
        return {"status": {"conditions": [cond]}}

    doc = {
        "items": [
            pod("True", "2026-10-16T10:00:00Z"),
            pod("False", "2026-10-16T11:00:00Z"),
        ]
    }
    assert health_alerts.pods_ready_at([doc, None]) == health_alerts.epoch(
        "2026-10-16T10:00:00Z"
    )
    assert health_alerts.pods_ready_at([None]) is None


def test_an_entry_with_a_hostname_has_an_ingress_route():
    assert health_alerts.has_ingress_route({"name": TAG, "hostname": "www"})


def test_an_entry_without_a_hostname_or_with_kuma_off_has_no_ingress_route():
    assert not health_alerts.has_ingress_route({"name": "netpol-baseline"})
    assert not health_alerts.has_ingress_route(
        {"name": TAG, "hostname": "www", "kuma": False}
    )
    assert not health_alerts.has_ingress_route(None)


# --- Folded into run_health --------------------------------------------------------------


def test_a_failing_alert_plane_on_a_no_workload_role_is_flagged_not_skipped(
    capsys,
):
    """netpol-baseline's pass line carries a skip marker; a failure must lead with its own."""
    # Active after any applied_at this host can hold, so it gates whatever the record says.
    fake = FakeProm(alert(4e9, service="netpol-baseline") + START)
    code = health.run_health("netpol-baseline", served="prod", prom=fake.tools())
    first = capsys.readouterr().out.splitlines()[0]
    assert code == 1
    assert "alert plane FAILED" in first
    assert not [m for m in detach_notify.NOT_APPLICABLE_MARKERS if m in first]


def test_a_quiet_alert_plane_on_a_no_workload_role_keeps_its_skip_line(
    capsys,
):
    code = health.run_health(
        "netpol-baseline", served="prod", prom=FakeProm(START).tools()
    )
    first = capsys.readouterr().out.splitlines()[0]
    assert code == 0
    assert any(m in first for m in detach_notify.NOT_APPLICABLE_MARKERS)
