"""Behavioural tests for kuma_push — the Python twin of kuma-push-lib.sh.

The retry rule is the point: Traefik answers 404 for the whole of an uptime-kuma rollout
(#1010), so a push that gives up on the first 404 is lost.
"""

import io
import urllib.error
import urllib.parse
from email.message import Message

import kuma_push


class _Body:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"ok":true}'


def _http_error(code, ctype="text/plain", body=b""):
    headers = Message()
    headers["Content-Type"] = ctype
    return urllib.error.HTTPError(
        "https://k/api/push/T", code, "x", headers, io.BytesIO(body)
    )


def _opener(*outcomes):
    """An opener that raises or answers each outcome in turn, recording the URLs it saw."""
    seen = []
    queue = list(outcomes)

    def opener(url, timeout=None):
        seen.append(url)
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Body()

    return opener, seen


def test_kuma_push_retries_a_rollout_404_and_delivers():
    opener, seen = _opener(_http_error(404), "ok")
    sleeps, logs = [], []
    assert kuma_push.kuma_push(
        "up",
        "fine",
        "kuma.example",
        "T",
        log=logs.append,
        opener=opener,
        sleep=sleeps.append,
    )
    assert len(seen) == 2
    assert sleeps == [kuma_push.KUMA_PUSH_RETRY_DELAY_S]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
    assert seen[0].startswith("https://kuma.example/api/push/T?")
    assert query["status"] == ["up"] and query["msg"] == ["fine"]


def test_kuma_push_to_a_base_url_keeps_its_scheme_and_port():
    """A pod pushes to the in-cluster Service over plain http, not through Traefik (#3745)."""
    opener, seen = _opener("ok")
    assert kuma_push.kuma_push(
        "up", "fine", "http://uptime-kuma.homelab:3001/", "T", opener=opener
    )
    assert seen[0].startswith("http://uptime-kuma.homelab:3001/api/push/T?")


def test_kuma_push_stops_on_a_rejected_token_without_retrying():
    opener, seen = _opener(_http_error(401, "application/json"))
    sleeps, logs = [], []
    ok = kuma_push.kuma_push(
        "down", "bad", "k", "T", log=logs.append, opener=opener, sleep=sleeps.append
    )
    assert ok is False
    assert len(seen) == 1 and sleeps == []
    assert "http=401 by=kuma" in logs[-1]


def test_kuma_push_logs_kumas_own_reason_with_the_token_redacted():
    # Kuma answers 404 for any exception, so `by=kuma` alone read a DB error as a token
    # mismatch (#4481). The reason gets its own line and the final line keeps its shape.
    body = b'{"ok":false,"msg":"select where push_token = \'SECRET\' - SQLITE_BUSY"}'
    opener, _seen = _opener(
        *(_http_error(404, "application/json", body) for _ in range(3))
    )
    logs = []
    kuma_push.kuma_push(
        "up", "m", "k", "SECRET", log=logs.append, opener=opener, sleep=lambda _s: None
    )
    assert logs[-2:] == [
        "push rejected by kuma (http=404): select where push_token = '<token>' - SQLITE_BUSY",
        "push failed (http=404 by=kuma) (status=up: m)",
    ]
    assert not any("SECRET" in line for line in logs)


def test_kuma_push_logs_no_reason_for_a_non_kuma_body():
    # The pair: Traefik's text/plain page is not Kuma answering, whatever its body says.
    body = b'{"msg":"not kuma"}'
    opener, _seen = _opener(*(_http_error(404, "text/plain", body) for _ in range(3)))
    logs = []
    kuma_push.kuma_push(
        "up", "m", "k", "T", log=logs.append, opener=opener, sleep=lambda _s: None
    )
    assert logs[-1] == "push failed (http=404) (status=up: m)"
    assert not any("not kuma" in line for line in logs)


def test_kuma_push_gives_up_after_three_transport_failures_and_never_raises():
    opener, seen = _opener(
        TimeoutError(), OSError("reset"), urllib.error.URLError("dns")
    )
    logs = []
    ok = kuma_push.kuma_push(
        "up", "m", "k", "SECRET", log=logs.append, opener=opener, sleep=lambda _s: None
    )
    assert ok is False
    assert len(seen) == kuma_push.KUMA_PUSH_ATTEMPTS
    assert logs[-1].startswith("push failed (error=URLError)")
    assert not any("SECRET" in line for line in logs), (
        "a log line leaked the push token"
    )


def test_kuma_push_without_a_token_sends_nothing():
    opener, seen = _opener()
    logs = []
    assert (
        kuma_push.kuma_push("up", "m", "k", "", log=logs.append, opener=opener) is False
    )
    assert seen == [] and logs


def test_cap_kuma_msg_cuts_to_the_limit_with_a_count_marker():
    capped = kuma_push.cap_kuma_msg("y" * 2000)
    assert len(capped) == kuma_push.KUMA_PUSH_MSG_MAX
    dropped = 2000 - capped.index(" …")
    assert capped.endswith(" …(+%d chars)" % dropped)
    assert kuma_push.cap_kuma_msg("short") == "short"
