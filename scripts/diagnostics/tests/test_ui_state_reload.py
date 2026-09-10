"""Does `browser_close` make the next navigation re-read the storage-state file?

`docs/claude-tooling.md` → *Recovering a stale browser session* tells an agent that a browser
bouncing to the Authelia portal is recovered by re-minting the session and calling
`browser_close`, because the MCP server builds a browser context — and reads `storageState` —
once per context rather than once per navigation. That was measured against `@playwright/mcp`
0.0.79 on 2026-09-06 and nothing guarded it. The package is Renovate-managed, and a bump that
changed what `browser_close` disposes would leave the documented procedure silently wrong: no
test anywhere goes red, and the symptom is an agent following a doc that no longer works, which
looks exactly like the stale session the doc exists to explain (GitHub issue #1413).

**A private state file, never the shared jar.** The test swaps the file underneath a running
server, and the shared jar is what every other `-m ui` run and every live Claude session reads.
`UI_MCP_STATE_PATH` is what `ui_mcp.sh` takes for this; it skips the wrapper's mint and gives
the launch its own config file, so an ordinary launch alongside this one is unaffected.

**Marked `ui`, so CI never runs it** — same bargain as `test_ui_smoke.py`: a real Chromium, a
real LAN route and a SOPS read.

    uv run pytest -m ui -k state_reload

**What it does NOT assert.** Only that a context built after `browser_close` reflects the state
file as it stands. It says nothing about a navigation WITHOUT a close, deliberately: an upstream
that started re-reading the file on every navigation would be an improvement, and a test pinning
today's behaviour in that direction would report it as a break.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_ui_smoke import REPO_ROOT, WRAPPER, McpClient, page_title

pytestmark = pytest.mark.ui

# One_factor, and the same service `test_ui_smoke`'s canary uses: it is the cheapest page on
# the LAN edge that is unambiguously the app rather than Traefik.
SERVICE = "homepage"

EMPTY_STATE = {"cookies": [], "origins": []}


def ui_login(*args) -> subprocess.CompletedProcess:
    """`ui_login.py`, through `uv run` — it imports `core`, whose PEP 758 syntax Ubuntu's
    /usr/bin/python3 cannot parse."""
    return subprocess.run(
        [
            "uv",
            "run",
            "--directory",
            str(REPO_ROOT),
            "python",
            "scripts/diagnostics/ui_login.py",
            *args,
        ],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def domain() -> str:
    from diagnostics.probe_lib import core

    return core.sops_extract("domain")


@pytest.fixture
def good_state(tmp_path) -> Path:
    """A copy of a live one_factor session, minted if the shared jar is stale.

    The MCP server this test launches is pointed at a private state file, never at the shared
    jar — that is what `UI_MCP_STATE_PATH` buys, and it is why swapping the file underneath a
    running server is safe alongside a live session. The mint above is the one thing here that
    touches the shared jar: `ui_login()` with no arguments writes
    `~/.claude/playwright/authelia-state.json`. That write is a refresh, exactly what
    `ui_mcp.sh` does on every launch, never a corruption.

    Minting is a skip rather than a failure
    when it fails: with no live session there is no way to tell a `browser_close` regression
    from having nothing to log in with, and `test_ui_smoke`'s two_factor fixture records what
    reporting that as a failure costs — three failures where three skips were the truth.
    """
    if ui_login("--check").returncode != 0:
        minted = ui_login()
        if minted.returncode != 0:
            pytest.skip(
                f"no one_factor session and minting one failed: "
                f"{(minted.stderr or minted.stdout).strip()}"
            )
    source = Path(ui_login("--path").stdout.strip())
    if not source.is_file():
        pytest.skip(f"the minted state file is not at {source}")
    destination = tmp_path / "good-state.json"
    shutil.copyfile(source, destination)
    return destination


def test_browser_close_makes_the_next_navigation_re_read_the_state_file(
    tmp_path, monkeypatch, domain, good_state
):
    """The pair, in one session: an empty state lands on the portal, and a good state swapped
    in behind a `browser_close` lands on the service.

    The first half is the rejecting one. Without it, a run where the session happened to work
    from launch would score green whatever `browser_close` did — the state file has to be
    observed DECIDING the outcome before the reload means anything.
    """
    # The pid goes in the FILENAME, not in the directory: `ui_mcp.sh` derives its private
    # launch config as `$RUNTIME_DIR/playwright-mcp-private-$(basename "$STATE_PATH").json`,
    # which `tmp_path`'s uniqueness never reaches. Two concurrent `-m ui -k state_reload`
    # runs shared that config path and one server came up pointed at the other run's state
    # file (GitHub issue #1591).
    state = tmp_path / f"session-state-{os.getpid()}.json"
    # Valid JSON, not an empty file: a malformed state fails the context build, which arrives
    # as a server error rather than as the portal, and the first assertion never runs.
    state.write_text(json.dumps(EMPTY_STATE))
    monkeypatch.setenv("UI_MCP_STATE_PATH", str(state))

    url = f"https://{SERVICE}.local.{domain}/"

    client = McpClient([str(WRAPPER)])
    try:
        client.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ui-state-reload", "version": "1"},
            },
        )
        client.notify("notifications/initialized")

        report, is_error = client.navigate(url)
        assert not is_error, f"the empty-state navigation reported an error:\n{report}"
        assert "auth.local." in report, (
            f"a browser carrying no session should land on the Authelia portal, but "
            f"{SERVICE} answered:\n{report}"
        )

        shutil.copyfile(good_state, state)
        client.call("tools/call", {"name": "browser_close", "arguments": {}})

        report, is_error = client.navigate(url)
        assert not is_error, f"the post-close navigation reported an error:\n{report}"
        assert "auth.local." not in report, (
            "the context built after `browser_close` did not pick up the state file that "
            "was swapped in — the recovery procedure in docs/claude-tooling.md no longer "
            f"works. Report:\n{report}"
        )
        assert page_title(report) is not None, (
            f"expected {SERVICE}'s own page after the reload; got a page with no title, "
            f"which is what Traefik answers when nothing is routed:\n{report}"
        )
    finally:
        client.close()
