"""An alert fires while daniel-pi's watchdog.service is stopped, and resolves once it is active.

Each test starts from the shipped rule that names the unit and follows it to what feeds it, so the
rule and its source cannot drift apart. The pytest shards install no promtool, so the fire and
resolve tests read the rule's promtool cases, and the validate-alert-rules hook runs those cases.
The source tests run the packaged producer against a stub `systemctl`. A `node_systemd_unit_state`
rule has no producer to run, so those tests read the node exporter's flags instead.

Run: uv run pytest ansible/tests/setup/test_pi_watchdog_alert.py
"""

import re
from pathlib import Path

import pytest
from _helpers import K8S_ROLES, load_yaml
from _host_packages import package as host_package
from _pi_health import REAL_CHECK_LIB, ROLE_CHECK_LIB
from lib.proc_testing import fake_bin, run, write_exec
from renderer.renderer_lib import host as host_mode

OBSERVABILITY = K8S_ROLES / "observability"
UNITS = "/usr/lib/systemd/system"
TEXTFILE_DIR = "/var/lib/node-exporter-textfile"
HEALTH_LOG_DIR = "/var/log/pi-health"
SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# homelab-ups-watchdog.service is the UPS secondary check, and `Watchdog` is Prometheus's own alert.
UNIT = re.compile(r"(?<![\w.-])watchdog\.service\b")
STATE_ARM_EXCLUDES = ("absent_over_time", "time()")
STOPPED = ["inactive", "failed"]

# Enough of `systemctl` for any way a script reads one unit's state. The real one would read this
# machine's units. Every other unit reads active.
SYSTEMCTL_STUB = """\
#!/usr/bin/env bash
args=" $* "
state=active
case "$args" in
  *" watchdog "*|*" watchdog.service "*) state="$STUB_WATCHDOG_STATE" ;;
esac
case "$args" in
  *" is-active "*)
    case "$args" in *" --quiet "*|*" -q "*) ;; *) echo "$state" ;; esac
    [ "$state" = active ]
    ;;
  *" is-failed "*)
    case "$args" in *" --quiet "*|*" -q "*) ;; *) echo "$state" ;; esac
    [ "$state" = failed ]
    ;;
  *" show "*)
    case "$args" in
      *" --value "*) echo "$state" ;;
      *) echo "ActiveState=$state" ;;
    esac
    ;;
  *" status "*)
    echo "Active: $state"
    [ "$state" = active ] || exit 3
    ;;
esac
"""


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_package("daniel-pi")


def _shipped_rules() -> list[tuple[str, dict]]:
    """Every (group, rule) that reaches Prometheus: a file the role's group list does not name never does."""
    groups = load_yaml(OBSERVABILITY / "defaults" / "main.yml")[
        "observability_rule_groups"
    ]
    return [
        (name, rule)
        for name in groups
        for group in load_yaml(OBSERVABILITY / "files" / "rules" / f"{name}.yaml")[
            "groups"
        ]
        for rule in group["rules"]
        if "alert" in rule
    ]


def _names_unit(labels_and_annotations: dict | None) -> bool:
    return bool(UNIT.search(str(labels_and_annotations or {})))


def _unit_rule() -> tuple[str, dict]:
    """The shipped rule that fires on the unit's state, not on its source going quiet."""
    found = [
        (name, rule)
        for name, rule in _shipped_rules()
        if (_names_unit(rule.get("labels")) or _names_unit(rule.get("annotations")))
        and not any(arm in rule["expr"] for arm in STATE_ARM_EXCLUDES)
    ]
    assert found, (
        "no rule in observability_rule_groups fires on the unit's state and names "
        "watchdog.service in its labels or annotations"
    )
    return found[0]


def _seconds(duration: str) -> int:
    return sum(
        int(n) * SECONDS[unit] for n, unit in re.findall(r"(\d+)([smhd])", duration)
    )


def _alert_cases(group: str, alert: str) -> list[dict]:
    """Each promtool case holding `alert`, with its input series and its (seconds, exp_alerts)."""
    path = OBSERVABILITY / "tests" / "rules" / f"{group}.test.yaml"
    assert path.is_file(), f"{alert} has no promtool cases: {path} is missing"
    cases = []
    for case in load_yaml(path)["tests"]:
        checks = [
            (_seconds(check["eval_time"]), check.get("exp_alerts") or [])
            for check in case.get("alert_rule_test", [])
            if check["alertname"] == alert
        ]
        if checks:
            cases.append({"series": case.get("input_series", []), "checks": checks})
    return cases


def _fires_naming_the_unit(exp_alerts: list[dict]) -> bool:
    return any(
        _names_unit(alert.get("exp_labels"))
        or _names_unit(alert.get("exp_annotations"))
        for alert in exp_alerts
    )


def _selected_metric(expr: str) -> str:
    metrics = re.findall(r"\b(\w+)\{", expr)
    assert metrics, f"the rule's expr selects no metric with matchers: {expr!r}"
    return metrics[0]


def _assert_node_exporter_collects_the_unit(package: host_mode.Package) -> None:
    unit = package.files[f"{UNITS}/node_exporter.service"][0].decode()
    assert "--collector.systemd" in unit, "node_exporter runs no systemd collector"
    include = re.search(r"--collector\.systemd\.unit-include=(\S+)", unit)
    assert include, (
        "the systemd collector has no unit-include, so it reports every unit"
    )
    assert re.fullmatch(include.group(1).strip("'\""), "watchdog.service")


def _producer(package: host_mode.Package, check: str) -> str:
    """The packaged executable that writes `check`'s textfile by asking systemctl."""
    found = [
        dst
        for dst, (data, mode) in package.files.items()
        if mode & 0o111 and b"systemctl" in data and check.encode() in data
    ]
    assert found, f"daniel-pi's package ships no script that writes check={check!r}"
    return found[0]


def _reported_success(
    package: host_mode.Package, check: str, state: str, tmp_path: Path
) -> float:
    """The `homelab_check_success` the producer writes for `check` while the unit is in `state`."""
    textfiles = tmp_path / "textfile"
    logs = tmp_path / "log"
    textfiles.mkdir()
    logs.mkdir()
    text = package.files[_producer(package, check)][0].decode()
    for real, repointed in (
        (REAL_CHECK_LIB, str(ROLE_CHECK_LIB)),
        (TEXTFILE_DIR, str(textfiles)),
        (HEALTH_LOG_DIR, str(logs)),
    ):
        text = text.replace(real, repointed)
    script = write_exec(tmp_path / "producer", text)
    result = run(
        ["bash", str(script)],
        stub_bin=fake_bin(
            tmp_path / "bin", systemctl=SYSTEMCTL_STUB, logger="exit 0\n"
        ),
        env={
            "PATH": "/usr/bin:/bin",
            "STUB_WATCHDOG_STATE": state,
            "TEXTFILE_DIR": str(textfiles),
        },
    )
    values = re.findall(
        rf'^homelab_check_success\{{[^}}]*check="{re.escape(check)}"[^}}]*\}} (\S+)$',
        "\n".join(p.read_text() for p in textfiles.glob("*.prom")),
        re.M,
    )
    assert len(values) == 1, (
        f"the producer wrote {len(values)} verdicts for {check}; stderr: {result.stderr}"
    )
    return float(values[0])


def _assert_source_reports(
    package: host_mode.Package, state: str, expected: float, tmp_path: Path
) -> None:
    _, rule = _unit_rule()
    metric = _selected_metric(rule["expr"])
    if metric == "node_systemd_unit_state":
        _assert_node_exporter_collects_the_unit(package)
        return
    assert metric.startswith("homelab_check_"), (
        f"the rule reads {metric}, which neither a textfile producer nor the systemd collector writes"
    )
    check = re.search(r'check="([^"]+)"', rule["expr"])
    assert check, f"the rule selects no check label: {rule['expr']!r}"
    assert _reported_success(package, check.group(1), state, tmp_path) == expected


def test_an_alert_on_the_units_state_ships_and_names_the_unit():
    group, rule = _unit_rule()
    assert group and rule["alert"]


def test_a_promtool_case_fires_the_alert_naming_the_unit_while_it_is_stopped():
    group, rule = _unit_rule()
    cases = _alert_cases(group, rule["alert"])
    assert any(
        _fires_naming_the_unit(exp_alerts)
        for case in cases
        for _, exp_alerts in case["checks"]
    ), f"no promtool case expects {rule['alert']} to fire naming watchdog.service"


def test_a_promtool_case_resolves_the_alert_once_the_unit_is_active():
    group, rule = _unit_rule()
    metric = _selected_metric(rule["expr"])
    resolving = [
        case
        for case in _alert_cases(group, rule["alert"])
        if any(
            later > fired and not quiet
            for fired, firing in case["checks"]
            if _fires_naming_the_unit(firing)
            for later, quiet in case["checks"]
        )
        # The last input is the active reading: a series gone stale would also quiet the alert.
        and any(
            series["series"].startswith(metric)
            and series["values"].split()[-1].startswith("1")
            for series in case["series"]
        )
    ]
    assert resolving, (
        f"no promtool case takes {metric} back to 1 and expects {rule['alert']} quiet after it fired"
    )


@pytest.mark.parametrize("state", STOPPED)
def test_the_rules_source_reports_a_stopped_unit(package, state, tmp_path):
    _assert_source_reports(package, state, 0, tmp_path)


def test_the_rules_source_reports_an_active_unit(package, tmp_path):
    _assert_source_reports(package, "active", 1, tmp_path)


def test_an_enabled_timer_keeps_the_source_fresh(package):
    _, rule = _unit_rule()
    metric = _selected_metric(rule["expr"])
    if metric == "node_systemd_unit_state":
        _assert_node_exporter_collects_the_unit(package)
        return
    check = re.search(r'check="([^"]+)"', rule["expr"])
    assert check, f"the rule selects no check label: {rule['expr']!r}"
    producer = _producer(package, check.group(1))
    # An enabled timer's service can ship in the updater package, which this render leaves out.
    services = {
        timer: package.files.get(f"{UNITS}/{timer.removesuffix('.timer')}.service")
        for timer in package.enable_links()
        if timer.endswith(".timer")
    }
    armed = [
        timer
        for timer, service in services.items()
        if service and f"ExecStart={producer}" in service[0].decode()
    ]
    assert armed, f"no enabled timer runs {producer}"
