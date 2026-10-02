"""The home-allowlist cron's failed-push line is one monitor-bridge can read.

The cron keeps its own tuned curl rather than sourcing kuma-push-lib.sh (the DECIDED block in
the script), so nothing but this pins its log line to the library's shape. A line of the form
`kuma push failed (<status>: <msg>)` does not match `verdicts/logs.py`'s `_SWALLOWED_RE`, so a
push Kuma rejects — a token no live tile holds — would reach the Swallowed Push Verdicts tile
from every library caller and never from this one.

`push()` is sourced by name and run against a stubbed `curl`, the way
test_kuma_push_retry.py exercises the library, and each logged line goes through the reader's
own `parse_push_line` rather than a regex copied here: the oracle is the consumer.
"""

import re
import shlex
from pathlib import Path

from _shell_render import rendered_shell_text
from verdicts.logs import parse_push_line
from lib.proc_testing import run

TAG = "crowdsec-home-allowlist"


def _push_function() -> str:
    """`push()`'s body, sliced out of the RENDERED script.

    The render is what makes the slice sourceable: `PUSH_URL` is a Jinja expression over the
    domain and the push token, so the template's own text is not shell bash can run (#3178).
    """
    script = rendered_shell_text(
        "k8s", "crowdsec", "crowdsec-update-home-allowlist.sh.j2"
    )
    match = re.search(r"^push\(\) \{[^\n]*\n.*?^\}$", script, re.M | re.S)
    assert match, (
        "push() is gone from crowdsec-update-home-allowlist.sh.j2 — sourced by name"
    )
    return match.group(0)


def _run_push(
    tmp_path: Path, reply: str, curl_rc: int, status: str = "down"
) -> list[str]:
    """Run push() once with curl stubbed to print `reply` for `-w` and exit `curl_rc`.

    Returns the lines push() handed to `logger`, `-t <tag>` stripped. The stub prints through the
    same `$(...)` the production pipeline uses, so what push() parses is what curl's `-w`
    would have written.
    """
    logs = tmp_path / "logs"
    logs.write_text("")
    script = f"""
PUSH_URL=https://push.example/secret-token
curl() {{ printf '%s' {shlex.quote(reply)}; exit {curl_rc}; }}
logger() {{ shift 2; echo "$*" >> {shlex.quote(str(logs))}; }}
{_push_function()}
push {status} "home allowlist: v6 prefix rotated"
"""
    run(["bash", "-c", script], check=True)
    return logs.read_text().splitlines()


def _as_syslog(line: str) -> str:
    return f"2026-09-18T02:00:00Z daniel-box {TAG}[1234]: {line}"


def test_a_kuma_json_404_is_read_as_rejected(tmp_path):
    # ACCEPT: the measured shape of Kuma refusing a token (curl 8.5.0, `-f`).
    lines = _run_push(tmp_path, "404 application/json; charset=utf-8", 22)
    assert lines == [
        "push failed (http=404 rc=22 by=kuma) (status=down: home allowlist: v6 prefix rotated)"
    ]
    parsed = parse_push_line(_as_syslog(lines[0]))
    assert parsed == (TAG, "daniel-box", "rejected", "down", "http=404 rc=22 by=kuma")


def test_a_traefik_text_404_is_read_as_swallowed_not_rejected(tmp_path):
    # REJECT (the pair): the edge's no-router 404 is text/plain, so no `by=kuma`, and the
    # reader files it as a swallowed push rather than a rejection.
    lines = _run_push(tmp_path, "404 text/plain; charset=utf-8", 22)
    assert lines == [
        "push failed (http=404 rc=22) (status=down: home allowlist: v6 prefix rotated)"
    ]
    assert parse_push_line(_as_syslog(lines[0])) == (
        TAG,
        "daniel-box",
        "swallowed",
        "down",
        "http=404 rc=22",
    )


def test_a_delivered_push_logs_nothing(tmp_path):
    assert (
        _run_push(tmp_path, "200 application/json; charset=utf-8", 0, status="up") == []
    )


def test_the_pre_1943_line_is_invisible_to_the_reader():
    # The `kuma push failed (...)` shape, kept so the reader's silence on it is a measured
    # fact rather than an assumption.
    assert (
        parse_push_line(_as_syslog("kuma push failed (down: v6 prefix rotated)"))
        is None
    )
