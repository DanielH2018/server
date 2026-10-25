"""crowdsec's off-cluster evidence channel agrees with itself across its three files.

Three files in three roles carry one contract, and each side passes its own tests alone:

- the probe script (`hosts/components/crowdsec-probe/crowdsec-edge-probe.sh`) writes
  `<time> http=<code>` to a status file;
- the Pi's Alloy (`hosts/components/pi-compose/services/alloy/config.alloy.j2`) tails that path, extracts
  `status` with a regex and counts it under a metric name;
- the verify Job (`k8s/crowdsec/files/crowdsec_verify.py`) queries `COUNTER` from Prometheus.

A rename on any one side leaves every other test green, and the flipped verify Job then fails
on every revision with "no probe answer". So this reads the rendered Alloy block, runs the real
probe against a stub `curl`, and holds the three together. Each check has a red case built by
editing one side.

Run: uv run pytest ansible/tests/services/test_crowdsec_edge_probe_contract.py
"""

import os
import re
import subprocess

import pytest

from _helpers import REPO
from lib.proc_testing import write_exec
from renderer.renderer_lib import host as host_mode

# crowdsec's verify module ships from its role's files/, which `pythonpath` in pyproject.toml
# puts on sys.path (autofix-bridge's ConfigMap carries crowdsec's jobs_metrics.py).
import crowdsec_verify

PROBE = REPO / "hosts" / "components" / "crowdsec-probe" / "crowdsec-edge-probe.sh"
# Alloy's `loki.process` prefixes every `stage.metrics` name with this (its `metrics_prefix`
# default), which is what Prometheus scrapes off `alloy-pi`.
ALLOY_METRICS_PREFIX = "loki_process_custom_"


def _block(config: str, kind: str, label: str) -> str:
    """The body of `<kind> "<label>" { ... }`, braces balanced."""
    start = config.index(f'{kind} "{label}" {{')
    depth = 0
    for i in range(start, len(config)):
        depth += {"{": 1, "}": -1}.get(config[i], 0)
        if depth == 0 and config[i] == "}":
            return config[start : i + 1]
    raise AssertionError(f"unbalanced {kind} {label}")


def contract(config: str) -> dict[str, str]:
    """The path, the status regex and the full metric name the Alloy config declares."""
    match = _block(config, "local.file_match", "crowdsec_edge_probe")
    process = _block(config, "loki.process", "crowdsec_edge_probe")
    (path,) = re.findall(r'"__path__" = "([^"]+)"', match)
    (regex,) = re.findall(
        r'expression = "((?:[^"\\]|\\.)*)"\n', process.split("stage.drop")[0]
    )
    (name,) = re.findall(r'name\s+= "([^"]+)"', process)
    return {
        "path": path,
        "regex": regex.encode().decode("unicode_escape"),
        "metric": ALLOY_METRICS_PREFIX + name,
    }


def _probe_line(tmp_path, script_text: str, code: str) -> tuple[str, str]:
    """Runs the probe with a stub `curl` answering `code`; its default path and its line."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_exec(bin_dir / "curl", f"#!/bin/sh\nprintf '%s' '{code}'\n")
    script = tmp_path / "probe.sh"
    write_exec(script, script_text)
    out = tmp_path / "probe.status"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CROWDSEC_EDGE_PROBE_FILE": str(out),
    }
    subprocess.run(
        [str(script), "jellyfin.local.example", "10.0.0.240"],
        env=env,
        check=True,
        timeout=30,
    )
    (default,) = re.findall(r"CROWDSEC_EDGE_PROBE_FILE:-([^}]+)\}", script_text)
    return default, out.read_text().splitlines()[-1]


@pytest.fixture(scope="module")
def alloy() -> str:
    """The config the Pi installs, from the rendered package (H5.8), not the template source."""
    package = host_mode.render_host("daniel-pi")
    return package.files["/usr/lib/homelab/compose/alloy/config.alloy"][0].decode()


def test_alloy_counts_the_metric_the_verify_job_queries(alloy):
    assert contract(alloy)["metric"] == crowdsec_verify.COUNTER


def test_a_renamed_alloy_counter_is_flagged(alloy):
    renamed = alloy.replace('"crowdsec_edge_probe_total"', '"crowdsec_probe_total"')
    assert contract(renamed)["metric"] != crowdsec_verify.COUNTER


def test_alloy_tails_the_file_the_probe_writes_and_parses_its_line(alloy, tmp_path):
    default, line = _probe_line(tmp_path, PROBE.read_text(), "403")
    found = contract(alloy)
    assert found["path"] == default
    match = re.search(found["regex"], line)
    assert match and match["status"] == "403", (found["regex"], line)


def test_a_probe_line_alloy_cannot_parse_is_flagged(alloy, tmp_path):
    edited = PROBE.read_text().replace("http=%s", "code=%s")
    _, line = _probe_line(tmp_path, edited, "403")
    assert re.search(contract(alloy)["regex"], line) is None


def test_a_probe_writing_another_path_is_flagged(alloy, tmp_path):
    edited = PROBE.read_text().replace(
        "crowdsec-edge-probe.status}", "crowdsec-edge-probe.log}"
    )
    default, _ = _probe_line(tmp_path, edited, "302")
    assert default != contract(alloy)["path"]
