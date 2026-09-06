#!/usr/bin/env python3
"""Paired red-proofs for the two retries in `test_ui_smoke`'s MCP client.

Both exist to absorb a transient rather than report it, which is exactly the shape that can
silently stop absorbing anything — or start absorbing a real failure. So each gets one input
it must retry past and one it must still fail on.

These carry no `ui` marker and start no browser, so CI runs them; `test_ui_smoke` itself is
deselected by `addopts`. Importing `McpClient` from that module is safe for the same reason
its own docstring gives: everything it imports at module scope is stdlib, pytest, or the
stdlib-only `grafana_panel_report`.
"""

import json
import subprocess

import pytest
import test_ui_smoke as smoke
from test_ui_smoke import McpClient


def client_returning(*replies) -> McpClient:
    """An McpClient whose `call` yields each reply in turn, with no subprocess behind it."""
    client = object.__new__(McpClient)
    pending = list(replies)

    def fake_call(method, params=None):
        return pending.pop(0)

    client.call = fake_call
    return client


def echo_only() -> dict:
    """What the server returns when it answers with the code it ran and no result."""
    return {
        "content": [{"type": "text", "text": "### Ran Playwright code\n```js\n1\n```"}]
    }


def with_result(value) -> dict:
    """A reply carrying `value` as the page would return it.

    Encoded twice on purpose: every evaluate in this suite ends `return JSON.stringify(x)`,
    so the server's result block holds a JSON string whose contents are themselves JSON, and
    the client decodes both layers.
    """
    return {
        "content": [
            {
                "type": "text",
                "text": f"### Result\n{json.dumps(json.dumps(value))}\n### Ran code",
            }
        ]
    }


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(smoke, "_EVALUATE_RETRY_INTERVAL", 0)
    monkeypatch.setattr(smoke, "_TITLE_SETTLE_INTERVAL", 0)


def test_evaluate_retries_past_a_resultless_reply():
    client = client_returning(echo_only(), with_result("ok"))
    assert client.evaluate("() => 1") == "ok"


def test_evaluate_still_fails_when_every_attempt_is_resultless():
    """The rejecting half. A retry that never gives up would hang a real breakage forever."""
    client = client_returning(*[echo_only()] * smoke._EVALUATE_ATTEMPTS)
    with pytest.raises(AssertionError, match="no result"):
        client.evaluate("() => 1")


def test_evaluate_unwraps_a_json_encoded_string():
    """The page returns `JSON.stringify(...)`, so the result arrives double-encoded."""
    client = client_returning(with_result({"a": 1}))
    assert client.evaluate("() => 1") == {"a": 1}


def test_settled_title_waits_out_a_title_the_app_sets_mid_hydration():
    client = client_returning(
        with_result("Homepage"), with_result("My Awesome Homepage")
    )
    assert client.settled_title("My Awesome Homepage") == "My Awesome Homepage"


def test_settled_title_returns_the_wrong_title_rather_than_hiding_it():
    """The rejecting half: a wrong title must be returned rather than hidden.

    A title that never becomes the expected one must reach the assertion, or a genuinely renamed
    page passes forever.
    """
    client = client_returning(
        *[with_result("Something Else")] * smoke._TITLE_SETTLE_ATTEMPTS
    )
    assert client.settled_title("My Awesome Homepage") == "Something Else"


def test_settled_title_stops_reading_once_it_matches():
    """One reply only: a second read would raise IndexError off the empty queue."""
    client = client_returning(with_result("My Awesome Homepage"))
    assert client.settled_title("My Awesome Homepage") == "My Awesome Homepage"


def test_close_closes_every_pipe_and_reaps_the_process():
    """`close` must leave nothing for the garbage collector to warn about.

    `filterwarnings = ["error"]` promotes the ResourceWarning an unclosed `stdout`/`stderr`
    raises at GC into an ERROR, and it lands on whichever test happens to run last — reading
    as that test having failed. Closing only `stdin`, as this did until 2026-09-06, errored
    the final dashboard of every `-m ui -k grafana` run.
    """
    client = object.__new__(McpClient)
    client.proc = subprocess.Popen(
        ["cat"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    client.close()
    assert client.proc.stdin.closed, "stdin was left open"
    assert client.proc.stdout.closed, "stdout was left open"
    assert client.proc.stderr.closed, "stderr was left open"
    assert client.proc.poll() is not None, "the wrapper process was never reaped"


def _report(service: str, domain: str, path: str, title: str) -> str:
    """A navigate report shaped the way `assert_serves_ui` reads one."""
    return (
        f"- Page URL: https://{service}.local.{domain}{path}\n- Page Title: {title}\n"
    )


def test_assert_serves_ui_passes_on_the_expected_title():
    """The accepting half: a page carrying its configured title raises nothing."""
    smoke.assert_serves_ui(
        _report("homepage", "example.com", "/", "My Awesome Homepage"),
        False,
        "example.com",
        "homepage",
        "My Awesome Homepage",
        "/",
        smoke.MINT_HINT,
        observed_title="My Awesome Homepage",
    )


def test_assert_serves_ui_names_the_config_less_default_on_a_wrong_title():
    """The rejecting half, and the reason the message says what it says.

    `Homepage` is gethomepage's own fallback for a page rendered with no settings. Issue #1399
    read exactly that failure as a stale expectation and proposed relaxing the check to match
    it. The message has to send the next reader at the app's config, not at this line.
    """
    with pytest.raises(AssertionError) as excinfo:
        smoke.assert_serves_ui(
            _report("homepage", "example.com", "/", "Homepage"),
            False,
            "example.com",
            "homepage",
            "My Awesome Homepage",
            "/",
            smoke.MINT_HINT,
            observed_title="Homepage",
        )
    message = str(excinfo.value)
    assert "config-less default" in message
    assert "do NOT relax this expectation" in message
