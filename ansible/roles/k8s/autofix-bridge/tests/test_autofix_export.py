"""autofix-bridge exports each cycle's verdict beside the Kuma push (M6 batch B7).

`autofix.py` records the verdict to crowdsec's `jobs_metrics` exposition BEFORE it pushes,
because `kuma_push` can retry a rollout's 404 window for about 90s and a scrape in that gap must
not read the previous cycle's verdict. A cycle that raises records 0. These tests pass recorders
through the seams `start_exporter`, `cycle_verdict` and `report_cycle` expose, so no port is
bound and no first-party module is patched.

Run: uv run pytest ansible/roles/k8s/autofix-bridge/tests/test_autofix_export.py
"""

import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "autofix_export_under_test",
    pathlib.Path(__file__).resolve().parent.parent / "files" / "autofix.py",
)
assert _SPEC and _SPEC.loader, "spec_from_file_location found no loader"
autofix = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(autofix)


class _Recorder:
    """Stands in for the exposition and for the Kuma push, sharing one event list."""

    def __init__(self, events, deadlines=None):
        self.events = events
        self.deadlines = deadlines
        self.port = None

    def serve(self, port):
        self.port = port

    def record(self, tag, ok, now):
        self.events.append(("record", tag, ok))

    def push(self, ok, msg):
        self.events.append(("push", ok))


def test_a_cycle_is_recorded_before_it_is_pushed():
    events = []
    recorder = _Recorder(events)
    autofix.report_cycle(recorder, True, "queue clean", push_fn=recorder.push)
    autofix.report_cycle(recorder, False, "sonarr down", push_fn=recorder.push)
    assert events == [
        ("record", "arr-autoblock", True),
        ("push", True),
        ("record", "arr-autoblock", False),
        ("push", False),
    ]


def test_a_cycle_without_an_exporter_still_pushes():
    events = []
    autofix.report_cycle(None, True, "queue clean", push_fn=_Recorder(events).push)
    assert events == [("push", True)]


def test_a_cycle_that_raises_is_a_down_naming_the_error():
    def boom(streaks):
        raise RuntimeError("sonarr unreachable")

    assert autofix.cycle_verdict({}, run=boom) == (
        False,
        "autofix-bridge error: sonarr unreachable",
    )
    assert autofix.cycle_verdict({}, run=lambda streaks: (True, "ok")) == (True, "ok")


def test_the_exporter_covers_the_check_with_a_three_cycle_deadline():
    events = []
    made = []

    def factory(deadlines):
        made.append(_Recorder(events, deadlines))
        return made[0]

    exporter = autofix.start_exporter(9183, 300, factory=factory)
    assert exporter is made[0]
    assert exporter.port == 9183
    assert exporter.deadlines == {"arr-autoblock": 900}


def test_port_zero_starts_no_exporter():
    def factory(deadlines):
        raise AssertionError("an exporter was built for port 0")

    assert autofix.start_exporter(0, 300, factory=factory) is None


def test_the_real_exposition_carries_the_series_the_rules_select():
    exposition = autofix.jobs_metrics.Exposition({"arr-autoblock": 900}, started=100.0)
    exposition.record("arr-autoblock", False, 400.0)
    body = exposition.body().decode()
    assert 'homelab_check_success{check="arr-autoblock"} 0.0' in body
    assert (
        'homelab_check_last_run_timestamp_seconds{check="arr-autoblock"} 400.0' in body
    )
    assert 'homelab_check_deadline_seconds{check="arr-autoblock"} 900.0' in body
