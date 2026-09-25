#!/usr/bin/env python3
"""The off-premises Healthchecks.io pings must stay honest.

WHY THIS IS A TEST AND NOT A COMMENT. Every on-prem heartbeat here pushes to Uptime
Kuma, which runs on the cluster those heartbeats watch — so a cluster or WAN outage
silences the push *and* the monitor waiting for it. The hc-ping.com calls exist purely to
be the signal that survives that, and they have two failure modes that leave a check
sitting green forever while nothing is actually watched:

1. `?create=1` auto-provisions a check from the slug in the URL. A typo therefore does
   not error — it silently creates a *different*, unwatched check, which then reads green
   because something is pinging it. The bad slug looks exactly like the good one.
2. Pinging the success endpoint unconditionally. Healthchecks.io alerts on silence and on
   an explicit `/fail`; a script that pings the bare URL whether or not it detected a
   problem reports "ran successfully" for every run, including the failing ones. That is
   strictly worse than no check, because it manufactures confidence.

Both produce a monitor that is green for the wrong reason, which is the one outcome this
whole mechanism is meant to prevent. A comment cannot catch the next one; this can.

Run: uv run pytest ansible/tests/services/test_healthchecks_pings.py
"""

import re
from pathlib import Path

import pytest
from _helpers import ANSIBLE


PING_HOST = "hc-ping.com"


# Templates, baked-in scripts and manifests — the only places a ping can be written. Scanning
# every file instead pulls in the vendored collections' binaries and fails on their bytes.
SOURCE_SUFFIXES = {".j2", ".sh", ".py", ".yml", ".yaml"}


def _read(path: Path) -> str:
    """Source with whole-line comments dropped.

    The wiring documents both traps in prose next to the code that avoids them, so a raw
    text search matches the explanation as readily as a real violation. Comments are what
    the checks below must ignore.
    """
    lines = _read_raw(path).splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _read_raw(path: Path) -> str:
    return path.read_text(errors="ignore")


def _ping_files() -> list[Path]:
    """Every file that builds or sends a Healthchecks.io ping.

    `HC_PING_URL` counts as well as the host name: pi-peer-backup's script reads its whole URL
    out of a Secret, so the host appears only in its comments — which `_read` drops — and a
    census keyed on the host alone misses the one ping that runs as a pod.
    """
    found = [
        path
        for path in sorted(ANSIBLE.rglob("*"))
        if path.is_file()
        and path.suffix in SOURCE_SUFFIXES
        and "collections" not in path.parts
        # A test's own fixtures are not ping wiring: one holding a `create=1` string would
        # fail `test_no_auto_provisioning` for a URL nothing ever sends.
        and "tests" not in path.parts
        and (PING_HOST in _read(path) or "HC_PING_URL" in _read(path))
    ]
    assert found, f"no {PING_HOST} references found — did the ping wiring move?"
    return found


def _sending_files() -> list[Path]:
    """The subset that actually curls the ping, rather than only templating the URL.

    Backslash continuations are folded first, so a curl whose URL sits on the next line —
    manifest-prune-check.sh.j2 — is still read as a sender.
    """
    return [
        path
        for path in _ping_files()
        if re.search(
            r"curl\b[^\n]*(HC_URL|HC_ALIVE_URL|HC_PING_URL|\$url)",
            _read(path).replace("\\\n", " "),
        )
    ]


@pytest.mark.parametrize("path", _ping_files(), ids=lambda p: p.name)
def test_no_auto_provisioning(path: Path) -> None:
    """Bare slugs only — `create=1` turns a typo into an unwatched, permanently-green check."""
    assert "create=1" not in _read(path), (
        f"{path} auto-provisions its check. Create the check by hand in the console so a "
        f"typo'd slug 404s instead of silently creating a second check nobody watches."
    )


@pytest.mark.parametrize("path", _sending_files(), ids=lambda p: p.name)
def test_failure_is_reported(path: Path) -> None:
    """A ping that never hits /fail reports success on every run, failures included."""
    assert "/fail" in _read(path), (
        f"{path} pings Healthchecks.io but never appends /fail. Silence covers a dead host; "
        f"only /fail covers a host that is alive and reporting a problem."
    )


# Every hc-ping curl invocation, with backslash continuations folded first so a call split over
# two lines is read whole. `HC_ALIVE_URL` is named because `_sending_files`' own regex does not
# match it — longhorn-backup-health.sh.j2 reaches this census through its OTHER ping.
_PING_CURL_RE = re.compile(
    r"curl\b[^\n]*?\$\{?(?:HC_URL|HC_ALIVE_URL|HC_PING_URL|url)\b[^\n]*"
)

# The ping sites this guard must find. Named rather than counted: a census that globs reads
# empty the day a file moves, and a parametrized check over nothing passes.
PING_SENDERS = frozenset(
    {
        "disk-health.sh.j2",
        "etcd-snapshot-offbox.sh.j2",
        "longhorn-backup-health.sh.j2",
        "manifest-prune-check.sh.j2",
        "pull-pi-peers.sh",
        "registry-gc.sh.j2",
    }
)


def _ping_curls(path: Path) -> list[str]:
    return _PING_CURL_RE.findall(_read(path).replace("\\\n", " "))


def test_the_ping_census_still_finds_every_sender() -> None:
    """Non-vacuity: the two checks below are parametrized over what this census returns."""
    assert PING_SENDERS <= {path.name for path in _sending_files()}


@pytest.mark.parametrize("path", _sending_files(), ids=lambda p: p.name)
def test_a_retried_ping_stays_off_stderr(path: Path) -> None:
    """A ping that retries must not mail its retry chatter from a healthy run (#2511).

    `--retry` writes `Warning: ... Will retry` to stderr for every attempt a later one
    recovers. cron mails whatever a job writes, so under `-S` a run that succeeded on the
    second attempt arrives looking like a failed run. Each site reports a genuine failure
    through its own `|| logger` (or `|| echo ... >&2`, in the pod), which is unaffected.
    """
    invocations = _ping_curls(path)
    assert invocations, (
        f"{path} is in the sending census but no ping curl was found in it"
    )
    for curl in invocations:
        assert not re.match(r"curl\s+-\w*S", curl), (
            f"{path} pings with -S, so curl's own retry chatter reaches stderr: {curl}"
        )
        assert "2>&1" in curl or "2>/dev/null" in curl, (
            f"{path} leaves the ping's stderr unrouted: {curl}"
        )


@pytest.mark.parametrize("path", _sending_files(), ids=lambda p: p.name)
def test_ping_is_optional(path: Path) -> None:
    """No configured key must leave the host exactly as it was — these are additive."""
    text = _read(path)
    guarded = re.search(r'-n "\$\{?(HC_PING_KEY|HC_PING_URL)', text)
    assert guarded, (
        f"{path} sends a ping without first checking the key/URL is non-empty. An "
        f"unconfigured host would curl a malformed URL on every run."
    )
