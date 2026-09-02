"""Loki: the selector roster, the log-error arm, and the ingestion watchdog.

A LogQL selector naming a label promtail does not ship matches no stream and reports "no events"
forever. HA_BAN_SELECTOR shipped that way with app="home-assistant". A fail-open arm cannot tell
"nothing to report" from "wrong question", so the selector labels are checked against the
observed stream vocabulary rather than against the check's own verdict.

The ingestion watchdog counts lines for an always-active stream over a window and goes down at
zero — the freshness analogue of the SMART and restore-drill checks.
"""

import re
from pathlib import Path

import pytest

import bridge_config
import bridge_io
import checks_logs

_REPO = Path(__file__).resolve().parents[5]

# ── HA ip_ban arm (2026-08-23: a banned infra IP 403'd the probes into a crash loop) ────────
# HA's ban middleware keys on the peer address, so a burst of bad /api/ calls can ban the node's
# pod-network gateway. The probes now exec curl to 127.0.0.1 and are immune; this arm is what
# keeps the ban itself from being silent.
# Two limits, so this guard is not over-trusted:
#   1. It reads each constant's IN-CODE DEFAULT. `env-secret.yaml.j2` overrides LOKI_STREAM at
#      deploy time, and this role's CLAUDE.md already flags in-code-default != deployed-value as a
#      live trap for exactly that constant. Both happen to select on `job`, so it passes either
#      way — but a deployed override is NOT what is being checked here.
#   2. The vocabulary below came from one k8s pod stream. LOKI_STREAM selects file-tail streams,
#      which may legitimately carry labels this set does not list. Widen the set against a live
#      stream if a genuine selector ever fails — do not delete the guard.
# Promtail's k8s stream vocabulary, read off a live Loki stream on 2026-08-23. `app` is NOT in
# it — HA_BAN_SELECTOR shipped with app="home-assistant", matched no stream, and reported "no
# ip_ban events" forever. A fail-open arm cannot tell "nothing to report" from "wrong question",
# so the selector label has to be checked by something other than the check's own verdict.
# Transcribed from a live stream on 2026-08-23. Kept as a FLOOR rather than the whole answer:
# `filename`, `stream` and `service_name` are added by promtail/Loki itself and appear in no
# config, so deriving alone would under-count and reject a valid selector.
_LOKI_STREAM_LABELS_OBSERVED = frozenset(
    {
        "container",
        "filename",
        "job",
        "machine",
        "namespace",
        "pod",
        "service_name",
        "stream",
    }
)


def _promtail_relabel_targets():
    """`target_label:` values from the rendered promtail config — the labels it actually sets.

    Derived rather than transcribed because the transcription cannot follow a rename: renaming a
    relabel target leaves the frozenset above listing a label nothing emits, so a selector using
    the NEW name is rejected while one using the dead name passes — the guard reporting the
    opposite of the truth. Internal `__foo__` labels are dropped; they never reach a stream.
    """
    cfg = (
        _REPO / "ansible/roles/k8s/loki-homelab/templates/configmap.yaml.j2"
    ).read_text()
    found = set(re.findall(r"target_label:\s*(\S+)", cfg))
    return {label for label in found if not label.startswith("__")}


LOKI_STREAM_LABELS = _LOKI_STREAM_LABELS_OBSERVED | _promtail_relabel_targets()


def test_the_promtail_config_is_actually_readable():
    """A path typo would make _promtail_relabel_targets() return an empty set, silently reducing
    the vocabulary to the transcribed floor and re-opening the gap this closes."""
    assert _promtail_relabel_targets(), (
        "no target_label values parsed from the promtail ConfigMap — the path or the config "
        "shape changed, and the derived half of LOKI_STREAM_LABELS is now inert"
    )


def _selector_labels(selector):
    """Label names in a LogQL stream selector — the `foo` of `{foo="bar",baz=~"qux"}`."""
    head = selector.split("}", 1)[0]
    return set(re.findall(r"(\w+)\s*(?:=~|!~|!=|=)", head))


def _logql_selector_names():
    """Every module constant that holds a LogQL stream selector, found by shape.

    Derived rather than listed. The hardcoded four were the selectors that existed when this
    was written, so LOKI_PI_STREAM -- added for the Pi's own promtail -- would have joined
    them unchecked (2026-08-25 review M-11). A selector this cannot see is a selector that can
    name a label promtail does not emit and go permanently green, which is the exact failure
    the test exists for.

    Matched on the LEADING `{...}` only, deliberately: a selector may carry line filters
    after the closing brace (`{...} |~ "Banned IP"`), and requiring the string to END in `}`
    silently dropped HA_BAN_SELECTOR -- narrowing the roster while looking like it widened it.
    """
    return sorted(
        name
        for name in dir(bridge_config)
        if name.isupper()
        and isinstance(getattr(bridge_config, name), str)
        and getattr(bridge_config, name).startswith("{")
        and "}" in getattr(bridge_config, name)
    )


def _deployed_selector_values():
    """LogQL selector values that actually deploy, read from `templates/env-secret.yaml.j2`.

    `_logql_selector_names()` only sees check.py's IN-CODE DEFAULTS. `LOKI_STREAM` and
    `LOG_ERROR_SELECTOR` are both overridden at deploy time in this template (the comment on
    `test_loki_selectors_use_real_stream_labels` says so: "a deployed override is NOT what is
    being checked here"). Neither in-code default is what runs against live Loki, so this reads
    the rendered `KEY: "value"` pairs straight out of the template.

    Only a QUOTED literal is a candidate: a LogQL selector is always written as a quoted string
    here, and quoting is also what tells a selector apart from a Jinja substitution. A bare
    shape test ("starts with `{`, contains `}`") is not enough on its own -- stripped of quotes,
    `KUMA_PUSH_GITOPS_ALIVE: "{{ monitor_bridge_gitops_alive_push_token }}"` has the same shape
    and this file has two dozen lines like it. Drop anything containing `{{` before applying the
    shape test, so a future Jinja `default(...)` filter or dict literal can't get misclassified
    as a selector.
    """
    tmpl = (
        _REPO / "ansible/roles/k8s/monitor-bridge/templates/env-secret.yaml.j2"
    ).read_text()
    found = {}
    for line in tmpl.splitlines():
        match = re.match(r"\s*([A-Z][A-Z0-9_]*):\s*(['\"])(.*)\2\s*$", line)
        if not match:
            continue
        key, _quote, value = match.groups()
        if "{{" in value:
            continue  # a Jinja substitution, not a literal
        if value.startswith("{") and "}" in value:
            found[key] = value
    return found


def test_the_selector_roster_covers_the_known_selectors():
    """A shape-derived roster that matches nothing passes every assertion vacuously, and one
    that matches less than the hardcoded list it replaced is a silent narrowing."""
    names = set(_logql_selector_names())
    known = {
        "LOKI_STREAM",
        "LOKI_DOCKER_STREAM",
        "LOKI_PI_STREAM",
        "HA_BAN_SELECTOR",
        "LOG_ERROR_SELECTOR",
    }
    assert known <= names, (
        "the derived roster no longer covers known selectors, so they are unchecked: %s"
        % sorted(known - names)
    )


def test_loki_selectors_use_real_stream_labels():
    for name in _logql_selector_names():
        selector = getattr(bridge_config, name)
        unknown = _selector_labels(selector) - LOKI_STREAM_LABELS
        assert not unknown, (
            "%s selects on %s, which promtail does not emit — the query matches no stream and "
            "the check goes permanently green: %s" % (name, sorted(unknown), selector)
        )


def test_the_deployed_selector_roster_covers_known_overrides():
    """A path typo or a template rewrite that drops the quoting would make
    `_deployed_selector_values()` return an empty dict, silently reducing this guard to the
    in-code-default arm above -- the exact gap it exists to close."""
    known = {"LOKI_STREAM", "LOG_ERROR_SELECTOR"}
    deployed = set(_deployed_selector_values())
    assert known <= deployed, (
        "the deployed-selector roster no longer covers the known overrides, so they are "
        "unchecked: %s" % sorted(known - deployed)
    )


def test_deployed_loki_selectors_use_real_stream_labels():
    """The in-code-default arm above cannot see a deploy-time override. This is the other
    half: `LOKI_STREAM` and `LOG_ERROR_SELECTOR` both ship a different selector than their
    check.py default (env-secret.yaml.j2), and neither ever ran through `_selector_labels`
    until now.

    Right now this is a regression guard, not an active finding: both deployed selectors
    select on `job`, which is a real promtail label, so this passes today. It exists for the
    NEXT edit to either constant -- the precedent is HA_BAN_SELECTOR (see this role's
    CLAUDE.md), which shipped an `app=` label that matched no stream and read "no ip_ban
    events" permanently green.
    """
    for name, selector in _deployed_selector_values().items():
        unknown = _selector_labels(selector) - LOKI_STREAM_LABELS
        assert not unknown, (
            "%s deploys as %s, which selects on %s -- promtail does not emit that label, so "
            "the query matches no stream and the check goes permanently green"
            % (name, selector, sorted(unknown))
        )


def test_log_error_inert_when_the_selector_matches_nothing():
    """Zero total volume must report INERT, never OK.

    The arm fails open, so a wrong selector produces no matches and reads exactly like a
    healthy estate. This is the HA_BAN_SELECTOR trap generalised: a fail-open check goes green
    on a typo. The total-volume count is the only thing separating "nothing is wrong" from
    "I asked the wrong question", and this test is what keeps it load-bearing.
    """
    ok, msg = checks_logs.log_error_verdict([], 0, 20, "1h")
    assert ok
    assert "INERT" in msg, (
        "a selector matching no lines must SAY so, not read as healthy"
    )


def test_log_error_quiet_estate_is_ok():
    ok, msg = checks_logs.log_error_verdict([], 5000, 20, "1h")
    assert ok
    assert "no log-error bursts" in msg


def test_log_error_names_the_offending_container():
    ok, msg = checks_logs.log_error_verdict(
        [({"container": "grafana"}, 91.0), ({"container": "sonarr"}, 3.0)],
        5000,
        20,
        "1h",
    )
    assert not ok
    assert "grafana (91)" in msg
    assert "sonarr" not in msg, "a container under the threshold is not an offender"


def test_log_error_orders_offenders_worst_first():
    _, msg = checks_logs.log_error_verdict(
        [({"container": "quiet"}, 21.0), ({"container": "loud"}, 900.0)], 5000, 20, "1h"
    )
    assert msg.index("loud") < msg.index("quiet")


def test_log_error_ignore_list_is_case_insensitive():
    ok, _ = checks_logs.log_error_verdict(
        [({"container": "Chatty"}, 900.0)], 5000, 20, "1h", ignore={"chatty"}
    )
    assert ok


def test_log_error_burst_wins_the_message_over_healthy_workloads(monkeypatch):
    """A Ready-but-failing workload pages even though every Kubernetes arm reads healthy.

    That combination IS the finding: readiness asks whether the port is open.
    """
    monkeypatch.setattr(bridge_config, "LOG_ERROR_SELECTOR", '{job=~"k8s|pi"}')
    monkeypatch.setattr(bridge_config, "LOG_ERROR_IGNORE", "")
    monkeypatch.setattr(
        bridge_io,
        "log_error_counts",
        lambda *a, **k: ([({"container": "grafana"}, 91.0)], 5000),
    )

    ok, msg = checks_logs.with_log_errors(True, "42 k8s workloads healthy")

    assert not ok
    assert msg.startswith("fatal log lines"), "the actionable arm leads"
    assert "42 k8s workloads healthy" in msg, (
        "the workload arm's text is kept, not dropped"
    )


def test_log_error_arm_fails_open_on_a_loki_outage(monkeypatch):
    """A Loki outage must not blind the three Kubernetes arms, which do not depend on it.

    This is why the check is NOT in LOKI_DEPENDENT: membership there suppresses the whole
    check, and Loki Reachable already owns that root cause.
    """
    monkeypatch.setattr(bridge_config, "LOG_ERROR_SELECTOR", '{job=~"k8s|pi"}')

    def boom(*a, **k):
        raise RuntimeError("loki query status=error")

    monkeypatch.setattr(bridge_io, "log_error_counts", boom)

    ok, msg = checks_logs.with_log_errors(False, "2 workloads unavailable")

    assert not ok, "the workload verdict survives the arm being unavailable"
    assert "2 workloads unavailable" in msg
    assert "log-error arm unavailable" in msg, "the arm must say it could not evaluate"


def _loki_scalar(val):
    """A Loki instant-query response for `sum(count_over_time(...))`. None -> empty result."""
    if val is None:
        return {"status": "success", "data": {"resultType": "vector", "result": []}}
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1700000000, str(val)]}],
        },
    }


def test_loki_ingestion_with_lines_is_ok():
    ok, msg = checks_logs.loki_ingestion_fresh(1234, "10m")
    assert ok
    assert "1234" in msg


def test_loki_ingestion_zero_lines_is_down():
    ok, msg = checks_logs.loki_ingestion_fresh(0, "10m")
    assert not ok
    assert "silent" in msg


def test_loki_ingestion_no_series_is_down():
    # an empty query result (no matching stream at all) is also a silent pipeline
    ok, msg = checks_logs.loki_ingestion_fresh(None, "10m")
    assert not ok


def test_loki_count_parses_value(monkeypatch):
    monkeypatch.setattr(bridge_io, "_get_json", lambda *a, **k: _loki_scalar(42))
    assert bridge_io.loki_count('{job="syslog"}', "10m") == 42.0


def test_loki_count_empty_result_is_none(monkeypatch):
    monkeypatch.setattr(bridge_io, "_get_json", lambda *a, **k: _loki_scalar(None))
    assert bridge_io.loki_count('{job="syslog"}', "10m") is None


def test_loki_count_non_success_raises(monkeypatch):
    monkeypatch.setattr(bridge_io, "_get_json", lambda *a, **k: {"status": "error"})
    with pytest.raises(RuntimeError):
        bridge_io.loki_count('{job="syslog"}', "10m")


def test_check_loki_ingestion_fresh_is_up(monkeypatch):
    monkeypatch.setattr(bridge_io, "loki_count", lambda *a, **k: 500)
    ok, _ = checks_logs.check_loki_ingestion()
    assert ok


def test_check_loki_ingestion_silent_is_down(monkeypatch):
    monkeypatch.setattr(bridge_io, "loki_count", lambda *a, **k: 0)
    ok, msg = checks_logs.check_loki_ingestion()
    assert not ok


def test_check_loki_ingestion_docker_stream_silent_is_down(monkeypatch):
    # docker_sd-specific failure: the file-tail streams keep flowing, but the highest-volume
    # container-log stream ({container=~".+"}) went silent. The file-tail arm alone stays
    # non-zero and would hide it — the docker-specific arm must page.
    def fake_count(selector, window):
        return 0 if "container" in selector else 500

    monkeypatch.setattr(bridge_io, "loki_count", fake_count)
    ok, msg = checks_logs.check_loki_ingestion()
    assert not ok
    assert "container" in msg


def test_check_loki_ingestion_filetail_silent_is_down(monkeypatch):
    # file-tail-only failure (the 2026-07-07 blind spot): the docker stream keeps flowing,
    # but authlog/syslog/traefik went silent. Arm 1's selector must EXCLUDE the docker stream
    # (which carries a `container` label) so a healthy container stream can't mask a dead
    # file-tail pipeline — the file-tail arm must page.
    def fake_count(selector, window):
        return 500 if "container" in selector else 0

    monkeypatch.setattr(bridge_io, "loki_count", fake_count)
    ok, msg = checks_logs.check_loki_ingestion()
    assert not ok
    assert "file-tail" in msg
