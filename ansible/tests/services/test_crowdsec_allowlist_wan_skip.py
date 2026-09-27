"""The home-allowlist cron's own WAN probe, issue #2793.

This cron is self-contained by design — its `push` carries curl flags tuned to its `*/5` period
rather than the shared library's, and the block at the top of the script records why. So it carries
its own `wan_reachable` too, and the two copies are free to drift. These guards hold them equal in
the two ways drift would matter: the flags (a probe that retried would eat the run budget
`test_a_whole_run_still_fits_the_cron_period` asserts) and the endpoint list (a copy probing
something the bridge never agreed to reports a skip for an outage nothing else sees).

The v4 arm is the only one wired. A WAN outage fails the v4 fetch first and `fail_reachout` exits,
so the v6 arm below it is unreachable in that state — wiring it as well would be a second gate on
a path that cannot be taken, which reads as coverage.
"""

import re

from lib import yaml_fast
from _helpers import ALL_VARS, ROLES

SCRIPT = ROLES / "k8s/crowdsec/templates/crowdsec-update-home-allowlist.sh.j2"
LIB = ROLES / "setup/initial_setup/files/kuma-push-lib.sh"


def _probe_curl(path) -> str:
    """The `wan_reachable` body's curl line, as written in `path`."""
    lines = [
        line.strip()
        for line in path.read_text().splitlines()
        if "curl " in line
        and "--max-time 5" in line
        and not line.lstrip().startswith("#")
    ]
    assert lines, f"{path.name} has no WAN-probe curl at --max-time 5"
    # The library writes one looped invocation; the script renders one per URL, all identical
    # apart from the URL itself, so a single representative is what the comparison needs.
    return lines[0]


def _flags(curl_line: str) -> set[str]:
    return {tok for tok in curl_line.split() if tok.startswith("-")}


def test_the_probe_flags_match_the_shared_library():
    assert _flags(_probe_curl(SCRIPT)) == _flags(_probe_curl(LIB))


def test_a_probe_that_retried_would_be_flagged():
    # The reject half. `--retry` is what would blow the run budget, so its absence is the property
    # the equality above is protecting rather than an incidental match.
    assert "--retry" not in _probe_curl(SCRIPT)
    assert "--retry" not in _probe_curl(LIB)


def test_the_script_probes_exactly_the_declared_endpoints():
    declared = yaml_fast.safe_load(ALL_VARS.read_text())["wan_probe_urls"]
    text = SCRIPT.read_text()
    # Rendered as one curl per URL by a Jinja loop over the same var, which is what lets
    # test_a_whole_run_still_fits_the_cron_period count every probe rather than one iteration.
    assert re.search(r"\{%\s*for url in wan_probe_urls\s*%\}", text), (
        "the probe no longer unrolls the endpoint list, so the run-budget guard undercounts it"
    )
    assert len(declared) >= 2


def test_the_ipv4_arm_reports_a_skip_rather_than_a_down():
    text = SCRIPT.read_text()
    assert 'fail_reachout "failed to resolve public IPv4 from ipify"' in text
    assert 'fail "failed to resolve public IPv4 from ipify"' not in text


def test_the_skip_path_logs_status_up_beside_the_push():
    # `probe.py alerts` rebuilds this cron's DOWN episodes from `status=down` in syslog, so a skip
    # that pushed `up` while still logging `status=down` would read as an alert for a green tile.
    body = SCRIPT.read_text().split("fail_reachout() {", 1)[1].split("\n}", 1)[0]
    assert "status=up skipped: WAN unreachable" in body
    assert "status=down" not in body
