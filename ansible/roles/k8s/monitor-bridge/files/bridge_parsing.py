"""Pure parsing and string helpers for check.py — no I/O, no config, no state.

WHY THIS MODULE EXISTS, AND WHAT MAY MOVE HERE
==============================================
check.py grew to ~3.5k lines. Splitting it is safe only for functions the test suite
never monkeypatches, and the reason is worth stating because it is invisible at the
call site and silently defeats tests rather than failing them.

The suite patches the `check` module object across dozens of distinct names
(`monkeypatch.setattr(check, "_get_json", ...)`, and plain `check.X = ...`). A function
reads its globals from the module it is DEFINED in, not the one it is imported into.
So moving a function here while a test patches `check.<name>` leaves the test patching
a name nothing reads: the test goes green against unpatched production code.

The rule that follows, and the only one that keeps this split honest:

  A function may live here only if it is never patched AND reads no patched
  module-level name. Everything it needs arrives as an argument.

That is why these five are pure and take their inputs explicitly. The config constants
moved to bridge_config.py on 2026-09-01 under a different rule — the tests now patch them
THERE and check.py reads them as `cfg.X` at call time — and the I/O primitives
(`_get_json`, `prom_scalar`, `push`) followed into bridge_io.py the same way. This module
predates that rule and needs neither: nothing here reads a patched name.

`sanitize` used to live here too; it moved to `bridge_common.py` because autofix-bridge's
autofix.py carried a byte-identical copy — bridge_common.py is the module both check.py and
autofix.py import it from now. Its header states the same rule this one does, checked against
both files' test suites rather than just this one's.

ENFORCED by ansible/tests/services/test_monitor_bridge_modules.py, which re-derives the patched
set from the test sources on every run. Deriving it is the point: the first census here
was a grep for one spelling of `setattr`, and it silently missed both the line-wrapped
form and every `check.X = ...` assignment.

`FETCH_BODY_MAX` lives here rather than in check.py because `describe_fetch_failure`
reads it; check.py imports it back for its three other uses. It is not patched, so the
import is safe under the rule above.
"""

import urllib.parse
from datetime import datetime

FETCH_BODY_MAX = 180


def duration_seconds(spec):
    """Seconds in a Prometheus duration like `15m` / `2h` / `90s` / `1d`. Unit-tested."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    spec = spec.strip()
    if not spec or spec[-1] not in units or not spec[:-1].isdigit():
        raise ValueError("not a Prometheus duration: %r" % spec)
    return int(spec[:-1]) * units[spec[-1]]


def parse_duration(s):
    """Parse a Prometheus-style duration ('900s', '15m', '1h', '2d') to seconds (float).

    A bare number is treated as seconds. The n8n check evaluates its failure window in
    Python (unlike the *_WINDOW vars that are interpolated straight into PromQL, which
    Prometheus parses), so it needs this.
    """
    s = str(s).strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units:
        return float(s[:-1]) * units[s[-1]]
    return float(s)


def parse_rfc3339(ts):
    """Parse an RFC3339 timestamp, tolerating nanosecond precision and a trailing 'Z'.

    datetime.fromisoformat only accepts 3- or 6-digit fractional seconds, but Kopia
    emits 9 (nanoseconds), so truncate the fractional part to microseconds first.
    """
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    if "." in ts:
        head, frac = ts.split(".", 1)
        digits = ""
        rest = ""
        for i, ch in enumerate(frac):
            if ch.isdigit():
                digits += ch
            else:
                rest = frac[i:]
                break
        ts = head + "." + digits[:6] + rest
    return datetime.fromisoformat(ts)


def endpoint_label(url):
    """host:port for `url` — deliberately NOT the path or query.

    This ends up in Kuma messages and therefore in Discord. `_get_json` is used for the
    Discord webhook probe, whose URL carries the webhook token IN THE PATH, so including
    the path would publish that token to the very channel it authenticates. Some *arr
    callers put keys in headers rather than the URL, but host:port is enough to name the
    service either way, which is the whole point.
    """
    netloc = urllib.parse.urlsplit(url).netloc
    return netloc.rsplit("@", 1)[-1] or "unknown host"


def describe_fetch_failure(url, exc, body=""):
    """Compose the message an unreachable or erroring HTTP source should page with.

    `_evaluate` otherwise renders a bare `str(exc)`, which for the common failures is close
    to content-free: a socket timeout stringifies to just "timed out", naming neither the
    endpoint nor the service. The 2026-08-02 B2 transaction-cap outage paged for 13h as
    `backup check error: timed out` — indistinguishable from a Kopia hiccup, while the real
    cause ("Transaction cap exceeded") sat in Kopia's own log.

    Where the server did answer, its error body carries that cause, and urllib's HTTPError
    discards it unless read explicitly — so the body is the most valuable part when present.
    """
    where = endpoint_label(url)
    detail = " ".join((body or "").split())
    if detail:
        return "%s: %s: %s" % (where, exc, detail[:FETCH_BODY_MAX])
    return "%s: %s" % (where, exc)
