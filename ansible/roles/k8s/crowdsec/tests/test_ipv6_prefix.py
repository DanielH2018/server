"""The home-allowlist cron's IPv6 masking, run by path the way the cron runs it
(`files/ipv6_prefix.py`).

Run: uv run pytest ansible/roles/k8s/crowdsec/tests/test_ipv6_prefix.py
"""

import subprocess
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "files" / "ipv6_prefix.py"


def _run(arg):
    return subprocess.run(
        [sys.executable, str(MODULE), arg],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_an_address_prints_its_64_and_junk_exits_non_zero():
    ok = _run("2001:db8:1:2:aaaa:bbbb:cccc:dddd")
    assert (ok.returncode, ok.stdout) == (0, "2001:db8:1:2::/64\n")

    bad = _run("not-an-address")
    assert bad.returncode == 1
    assert bad.stdout == ""
    assert "'not-an-address' is not an IPv6 address" in bad.stderr
