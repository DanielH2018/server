"""A node with a static wired link reports whether the gateway answers over that link.

The wired default route outranks the WiFi one while the link keeps carrier, so a NIC whose
transmit queue hangs blackholes the node with no change WiredLinkDown can read.
`wired-gateway-check.sh` in the k3s-server component pings the gateway bound to the link, and
three properties hold:

THE VERDICT IS A REPLY. A ping that gets a reply writes success 1; one that gets none, or that
cannot run, writes 0, and the unit still exits 0 because the verdict is the report.

THE RULES' LITERALS EQUAL THE SCRIPT'S DEADLINE_S, on one Unreachable and NotReporting pair per
host whose host_vars set k3s_node_static_link.

EVERY SUCH HOST ARMS THE TIMER, at a period that fits three runs inside the deadline, and its
unit pings that host's own link.

Run: uv run pytest hosts/tests/test_wired_gateway_check.py
"""

import os
import re

from _aide_check_run import LOGGER_STUB
from _helpers import K8S_ROLES, REPO
from _host_packages import HOSTS, package, shipped_text
from lib import yaml_fast
from lib.repo_paths import HOST_VARS, K3S_SERVER
from lib.proc_testing import fake_bin, run, write_exec

SCRIPT = K3S_SERVER / "files" / "wired-gateway-check.sh"
CHECK_LIB = REPO / "hosts/components/check-lib/files/homelab-check-lib.sh"
RULES = K8S_ROLES / "observability" / "files" / "rules" / "host-checks.yaml"
TIMER = "/usr/lib/systemd/system/homelab-wired-gateway.timer"
SERVICE = "/usr/lib/systemd/system/homelab-wired-gateway.service"
ALERTS = ("WiredGatewayUnreachable", "WiredGatewayCheckNotReporting")
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")


def _run(tmp_path, ping_rc: int) -> tuple[int, dict[str, str], str]:
    """Run the check with `ping` exiting `ping_rc`.

    Returns the exit code, every `homelab_*` series of the check by name, and the argv ping got.
    """
    script = write_exec(tmp_path / "check.sh", SCRIPT.read_text())
    textfile = tmp_path / "textfile"
    textfile.mkdir()
    argv = tmp_path / "ping-argv"
    stubs = fake_bin(
        tmp_path / "bin",
        logger=LOGGER_STUB,
        ping=f'echo "$*" > "{argv}"\nexit {ping_rc}\n',
        ip='echo "10.0.0.1 INCOMPLETE"\n',
    )
    result = run(
        [str(script)],
        env={
            **os.environ,
            "HOMELAB_LIB_DIR": str(CHECK_LIB.parent),
            "TEXTFILE_DIR": str(textfile),
            "LOGGER_OUT": str(tmp_path / "logged"),
            "WIRED_LINK": "eno1",
            "WIRED_GATEWAY": "10.0.0.1",
        },
        stub_bin=stubs,
    )
    prom = (textfile / "wired-gateway.prom").read_text()
    series = dict(
        re.findall(r'^(homelab_\w+)\{check="wired-gateway"\} (\d+)$', prom, re.M)
    )
    return result.returncode, series, argv.read_text()


def deadline() -> int:
    [found] = re.findall(r"^DEADLINE_S=(\d+)$", SCRIPT.read_text(), re.M)
    return int(found)


def test_a_reply_over_the_link_writes_success_1(tmp_path):
    code, series, argv = _run(tmp_path, ping_rc=0)
    assert (code, series["homelab_check_success"]) == (0, "1")
    assert series["homelab_check_deadline_seconds"] == str(deadline())
    assert "-I eno1 10.0.0.1" in argv


def test_no_reply_writes_success_0_and_exits_0(tmp_path):
    code, series, _ = _run(tmp_path, ping_rc=1)
    assert (code, series["homelab_check_success"]) == (0, "0")


def test_a_ping_that_cannot_run_writes_success_0(tmp_path):
    code, series, _ = _run(tmp_path, ping_rc=2)
    assert (code, series["homelab_check_success"]) == (0, "0")


def static_links() -> dict[str, str]:
    """host -> k3s_node_static_link, for every host whose host_vars set one."""
    links = {}
    for path in sorted(HOST_VARS.glob("*.yml")):
        link = (yaml_fast.safe_load(path.read_text()) or {}).get("k3s_node_static_link")
        if link:
            links[path.stem] = link
    return links


def rules(text: str) -> dict[tuple[str, str], str]:
    """(alert, origin) -> expr for every WiredGateway* rule."""
    [group] = yaml_fast.safe_load(text)["groups"]
    found = {}
    for rule in group["rules"]:
        if rule.get("alert") in ALERTS:
            [origin] = re.findall(r'origin="([^"]+)"', rule["expr"])[:1]
            found[(rule["alert"], origin)] = rule["expr"]
    return found


def literal_problems(
    found: dict[tuple[str, str], str], hosts: set[str], deadline_s: int
) -> list[str]:
    """Why the rule set or a NotReporting rule's literals differ from the script, or nothing."""
    expected = {(alert, host) for alert in ALERTS for host in hosts}
    problems = [f"no rule {key}" for key in sorted(expected - set(found))]
    problems += [f"an unexpected rule {key}" for key in sorted(set(found) - expected)]
    for (alert, origin), expr in sorted(found.items()):
        if alert != "WiredGatewayCheckNotReporting":
            continue
        for name, values in (
            ("threshold", STALE_ARM.findall(expr)),
            ("window", WINDOW.findall(expr)),
        ):
            if values != [str(deadline_s)]:
                problems.append(
                    f"{origin}: {name} reads {values}, DEADLINE_S is {deadline_s}"
                )
    return problems


def test_static_links_names_both_k3s_nodes():
    assert set(static_links()) == {"daniel-box", "daniel-server"}


def test_literal_problems_is_clean_on_the_shipped_rules():
    assert (
        literal_problems(rules(RULES.read_text()), set(static_links()), deadline())
        == []
    )


def test_literal_problems_is_flagged_on_a_threshold_that_drifted():
    text = RULES.read_text().replace(
        'check="wired-gateway",origin="daniel-box"} > 180',
        'check="wired-gateway",origin="daniel-box"} > 300',
    )
    assert literal_problems(rules(text), set(static_links()), deadline()) == [
        "daniel-box: threshold reads ['300'], DEADLINE_S is 180"
    ]


def test_literal_problems_is_flagged_on_a_static_link_host_with_no_rules():
    problems = literal_problems(
        rules(RULES.read_text()), set(static_links()) | {"daniel-pi"}, deadline()
    )
    assert problems == [
        "no rule ('WiredGatewayCheckNotReporting', 'daniel-pi')",
        "no rule ('WiredGatewayUnreachable', 'daniel-pi')",
    ]


def test_only_static_link_hosts_arm_the_timer():
    armed = {
        host
        for host in HOSTS
        if any(
            path.endswith("/homelab-wired-gateway.timer")
            for path in package(host).links
        )
    }
    assert armed == set(static_links())


def test_every_armed_timer_runs_three_times_per_deadline_on_its_own_link():
    for host, link in static_links().items():
        [minutes] = re.findall(
            r"^OnCalendar=\*:0/(\d+)$", shipped_text(host, TIMER), re.M
        )
        assert int(minutes) * 60 * 3 == deadline(), host
        assert f"WIRED_LINK={link} " in shipped_text(host, SERVICE), host
