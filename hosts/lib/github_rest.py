"""The GitHub token lookup and the REST read every host GitHub reader shares.

Used by checkout_sync.py, the builder, renovate_notify.py and the repo's await_ci.py and forge.
A host component's package ships a copy beside its script (a ``lib:`` entry in its
``component.yml``), as it does ``host_lib.py``. Stdlib only.

Typical usage example:

    token = github_token(os.environ, subprocess.run)
    runs = github_get("repos/o/n/actions/runs?head_sha=abc", token, user_agent="await-ci")
"""

import functools
import importlib.util
import json
import os
import urllib.request
from collections.abc import Callable, Mapping

GITHUB_API = "https://api.github.com"
GITHUB_TIMEOUT_S = 15
# A component's copy of this module sits in its own directory, where a bare `import gh_quota`
# fails. `GH_QUOTA_MODULE` in the environment points elsewhere, for a test's own ledger.
GH_QUOTA_MODULE = "/usr/lib/homelab/gh_quota.py"


def github_token(source: Mapping[str, str], run: Callable) -> str | None:
    """A GitHub token for REST reads, or None to query anonymously.

    ``GH_TOKEN`` then ``GITHUB_TOKEN`` in ``source`` win, then ``gh auth token``. ``source`` is
    wherever the caller keeps its settings: await_ci passes ``os.environ``, renovate_notify
    its ``config.env``, checkout-sync and the builder their systemd credential. The gh CLI on
    daniel-box is logged in as the repo owner, and the callers that let it answer run as that
    user. The lookup is best-effort: a missing gh, an expired login, or a slow
    keyring all return None, and the caller queries anonymously.

    Why authenticate a read of a public repo. The anonymous limit is 60 requests/hour PER
    SOURCE IP, and every GitHub call from this host shares it: the tick's gate, ``await_ci.py``
    during a landing, renovate_notify, the ruleset-drift cron. ``await_ci.py`` polls every 20s
    for up to land.sh's CI budget, which would be hundreds of polls of 2 or more requests each.
    When this returns None it makes at most 15 polls, at least 90s apart, and skips its
    descendant walk, which bounds a wait at 30 requests (``await_ci.ANONYMOUS_MAX_POLLS``).
    Without that back-off, two ``land.sh`` runs in an hour exhaust the limit, after which the
    tick's gate reads ``HTTP Error 403: rate limit exceeded`` and defers as ``CI not finished``.
    That is correct fail-closed behaviour, and also a deploy outage nobody asked for.
    Authenticated, the limit is 5000/hour per token.
    """
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = (source.get(name) or "").strip()
        if value:
            return value
    try:
        proc = run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def github_auth_headers(token: str | None) -> dict[str, str]:
    """The ``Authorization`` header for ``token``, or nothing for an anonymous request."""
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def github_get(
    path: str,
    token: str | None,
    *,
    user_agent: str,
    timeout: float = GITHUB_TIMEOUT_S,
    opener: Callable | None = None,
):
    """GET ``https://api.github.com/<path>`` and return the parsed JSON body.

    The one request shape every host GitHub reader shares, so the readers cannot drift on the
    headers, the auth or the timeout. What a failure MEANS stays with the caller:
    checkout-sync's and the builder's CI gates read an error as ``pending``, a landing lets it
    raise.

    An authenticated read is counted in the host's gh-quota ledger before it is sent, against
    the caller's Claude session or systemd service, so ``gh-quota status`` names these
    readers instead of reporting them as spend beyond the ledger (docs/github-quota.md).

    Args:
        path: the part after the API root, query string included, e.g. ``repos/o/n/pulls``.
        token: from ``github_token``, looked up once by the caller; None reads anonymously.
        user_agent: names the caller in GitHub's logs. GitHub refuses a request without one.
        timeout: seconds for the whole request.
        opener: the ``urlopen`` that carries the request, ``urllib.request.urlopen`` when None,
            resolved per call so a stub of the stdlib name reaches it too.

    Raises:
        urllib.error.URLError: the API could not be reached, or answered non-2xx
            (``HTTPError`` is a subclass).
        TimeoutError, OSError: the socket failed.
        ValueError: the body was not JSON.
    """
    count_github_call("GET", path.lstrip("/"), token)
    req = urllib.request.Request(
        "%s/%s" % (GITHUB_API, path.lstrip("/")),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": user_agent,
            **github_auth_headers(token),
        },
    )
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        return json.load(resp)


def count_github_call(method: str, path: str, token: str | None) -> None:
    """Count one REST request in the host's gh-quota ledger, under the token that sends it.

    Call it just before sending. A GET records as `api rest` plus the path's template, any other method as `api write`.
    An anonymous request spends the source IP's limit, not a token owner's, so it is not
    counted. A host without the gh-quota component counts nothing, and a ledger module that
    fails to load counts nothing too: the ledger must never be why a GitHub request fails.

    Args:
        method: the HTTP method, such as ``GET`` or ``DELETE``.
        path: the part after the API root, as ``github_get`` takes it.
        token: the bearer token the request sends, or None for an anonymous one.
    """
    if not token:
        return
    ledger = _gh_quota(os.environ.get("GH_QUOTA_MODULE") or GH_QUOTA_MODULE)
    if ledger is not None:
        ledger.record(
            ["api", "-X", method, path], env={**os.environ, "GH_TOKEN": token}
        )


@functools.cache
def _gh_quota(module_path: str):
    spec = importlib.util.spec_from_file_location("gh_quota", module_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module
