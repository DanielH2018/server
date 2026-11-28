"""The cap on apt's retry delay that two container cases put on, against real apt.

The test image's `Acquire::Retries "10"` makes a refused source cost an `apt-get update` 181 s.
`Container.cap_apt_retry_delay` caps the wait between retries; these hold that the cap keeps
every retry and ends the fetch in seconds, and that the stopped-server case puts it on first.

Run: uv run pytest scripts/validate/tests/host_cases/test_apt_retry_delay_cap.py
"""

import ast
import re
import socket
import time

from test_validate_host_package_cases import _apt_update, _publish
from validate.validate_lib import host_container
from validate.validate_lib import host_fixtures as fx


def test_the_retry_delay_cap_ends_a_refused_fetch_in_seconds_with_every_retry(tmp_path):
    dockerfile = (fx.REPO / "hosts/lib/tests/container/Dockerfile").read_text()
    (image_retries,) = re.findall(r"echo '(Acquire::Retries \"(\d+)\";)'", dockerfile)
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    try:
        start = time.monotonic()
        done = _apt_update(
            _publish(tmp_path),
            tmp_path,
            uri=f"http://127.0.0.1:{closed.getsockname()[1]}",
            conf=f"{image_retries[0]}\n{host_container.RETRY_DELAY_CAP}\n",
        )
        seconds = time.monotonic() - start
    finally:
        closed.close()

    assert "Connection refused" in done.stdout + done.stderr
    assert {
        "retries": done.stdout.count("Ign:1 "),
        "under 30 s": seconds < 30,
    } == {"retries": int(image_retries[1]), "under 30 s": True}


def test_the_stopped_server_case_caps_apts_retry_delay_before_it_stops_the_server():
    source = (fx.REPO / "scripts/validate/host_package_cases.py").read_text()
    (case,) = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "stopped_server"
    ]
    calls = [
        node.func.attr
        for node in sorted(
            (n for n in ast.walk(case) if isinstance(n, ast.Call)),
            key=lambda n: (n.lineno, n.col_offset),
        )
        if isinstance(node.func, ast.Attribute)
    ]

    assert "cap_apt_retry_delay" in calls
    assert calls.index("cap_apt_retry_delay") < calls.index("stop_serving")
