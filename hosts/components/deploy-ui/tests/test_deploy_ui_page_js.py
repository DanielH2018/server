"""The deploy page's own script, run under node against a stubbed DOM.

The page's script runs whole, with every top-level poll and listener landing on a stub, so the
helpers under test are the ones the page loads. Skipped where node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "files" / "deploy_ui.html"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)

# Every element is one inert object and fetch never resolves, so no poll renders anything.
_HARNESS = """
const vm = require("vm");
const el = new Proxy({}, {get: (t, k) => k in t ? t[k] : () => {}, set: (t, k, v) => (t[k] = v, true)});
globalThis.document = {getElementById: () => el, addEventListener: () => {}};
globalThis.window = {addEventListener: () => {}};
globalThis.location = {hash: "", pathname: "/"};
globalThis.history = {replaceState: () => {}};
globalThis.localStorage = {getItem: () => null, setItem: () => {}};
globalThis.fetch = () => new Promise(() => {});
globalThis.setInterval = () => 0;
vm.runInThisContext(process.argv[1]);
process.stdout.write(JSON.stringify(vm.runInThisContext(process.argv[2])));
"""


def page_eval(expr: str):
    """The JSON value of `expr`, evaluated after the page's script has run."""
    found = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert found, f"{PAGE.name} has no <script> block"
    out = subprocess.run(
        ["node", "-e", _HARNESS, found.group(1), expr],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(out.stdout)


def test_elapsed_reads_as_minutes_and_seconds_is_clean():
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing queue
    assert page_eval("[dur(5), dur(3243), dur(7510)]") == [
        "0m 05s",
        "54m 03s",
        "125m 10s",
    ]


def test_process_names_the_script_not_its_interpreter_is_clean():
    # fact: hosts/components/deploy-ui/CLAUDE.md#In flight
    assert page_eval(
        "[procName('/w/.venv/bin/python scripts/deploy_tools/land.py --pr 1 --since a'),"
        " procName('uv run python scripts/deploy_tools/land.py --pr 1'),"
        " procName('/usr/bin/flock -w 5 /var/lock/server-git-tree.lock')]"
    ) == ["land.py", "land.py", "flock"]


def _row(pr: str, queue: str, position=None, alive=True) -> dict:
    return {
        "pr": pr,
        "host": "daniel-server",
        "state": "queued",
        "label": "Queued",
        "in_state_s": 65,
        "history": [{"state": "armed", "label": "Armed", "since": 0}],
        "progress": {"done": 2, "total": 3, "what": "checks finished"},
        "alive": alive,
        "queue": queue,
        "position": position,
        "title": "",
    }


def test_queue_renders_the_daemons_order_with_each_landings_place_is_clean():
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing queue
    inflight = {
        "runs": [],
        "landings": [
            _row("1", "turn"),
            _row("2", "waiting", 1),
            _row("3", "stepped-out"),
            _row("4", "", alive=False),
        ],
        "landings_unavailable": "",
        "turn": {
            "turn_held": True,
            "serial": {"held": True, "pr": "9"},
            "slots": {"used": 2, "of": 6},
        },
    }
    html = page_eval(f"queue({json.dumps(inflight)})")
    assert re.findall(r"/pull/(\d+)", html) == ["9", "1", "2", "3", "4"]
    for text in ("holds the turn", "waiting #1", "stepped out", "died", "Queued"):
        assert text in html
    assert "2/3 checks finished" in html and "slots 2/6 in use" in html
