"""The HTTP layer under `forge.py`: one request, one response, and reading what came back.

`forge.py` re-exports `ForgeError`, `Response`, `Transport`, `urllib_transport`, `urllib_post`
and `Discord`, so a caller names them as `forge.<name>` and never imports this module. It
holds no forge knowledge: no registry, repository or token. `Discord` is here because a
webhook is one POST and no forge, and `forge.py` is at its length cap, as `Registry` is in `forge_registry.py`. Every request `promote.py` makes still goes through `forge.py`
(scripts/deploy_tools/tests/test_promote_forge_boundary.py).
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

# Seconds for one HTTP request or one subprocess. A timeout is an error, never an absence.
TIMEOUT_S = 30

USER_AGENT = "homelab-promote"


class ForgeError(RuntimeError):
    """A forge read failed: a status that means neither present nor absent, or no answer."""


@dataclass(frozen=True)
class Response:
    """One HTTP response, whatever its status.

    Attributes:
        status: the HTTP status code.
        headers: the response headers, names lower-cased.
        body: the raw body, empty for a `HEAD`.
    """

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


class Transport(Protocol):
    """Sends one request and returns its response, raising `ForgeError` on no answer."""

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str]
    ) -> Response: ...


def urllib_transport(
    method: str, url: str, headers: Mapping[str, str], timeout: float = TIMEOUT_S
) -> Response:
    """Send one request with `urllib`, returning a non-2xx status rather than raising it.

    `timeout` is in seconds. `probe.py landing` passes its own, measured against the live
    registry, and every `promote.py` read keeps TIMEOUT_S.

    Raises:
        ForgeError: the request timed out or never reached the server.
    """
    request = urllib.request.Request(
        url, method=method, headers={"User-Agent": USER_AGENT, **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Response(response.status, _lower(response.headers), response.read())
    except urllib.error.HTTPError as err:
        return Response(err.code, _lower(err.headers or {}), err.read() or b"")
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise ForgeError(f"{method} {url}: {err}") from err


def urllib_post(
    url: str, body: bytes, headers: Mapping[str, str], timeout: float = TIMEOUT_S
) -> Response:
    """POST `body` to `url` with `urllib`, returning a non-2xx status rather than raising it.

    The error names neither the URL nor urllib's message, which repeats it, because the one
    caller posts to a Discord webhook whose URL is its credential.

    Raises:
        ForgeError: the request timed out or never reached the server.
    """
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"User-Agent": USER_AGENT, **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Response(response.status, _lower(response.headers), response.read())
    except urllib.error.HTTPError as err:
        return Response(err.code, _lower(err.headers or {}), err.read() or b"")
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise ForgeError(f"POST to the webhook: {type(err).__name__}") from None


def _lower(headers) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items()}


def _json(response: Response, what: str):
    try:
        return json.loads(response.body)
    except ValueError as err:
        raise ForgeError(f"{what}: the body is not JSON") from err


def _api_message(response: Response) -> str:
    """`: <message>` from a GitHub API error body, or nothing when it carries none.

    A 403 means either a missing permission or a rate limit, and only the message tells which.
    """
    try:
        body = json.loads(response.body)
    except ValueError:
        return ""
    message = body.get("message") if isinstance(body, dict) else None
    return f": {message}" if isinstance(message, str) and message else ""


def _updated(entry: dict) -> datetime:
    """A commit status's `updated_at`, or the earliest time when it is missing or malformed."""
    try:
        return datetime.fromisoformat(str(entry.get("updated_at")))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


def _error_codes(response: Response) -> set[str]:
    """The `code` of each entry in an OCI error body, or nothing when the body has none."""
    try:
        body = json.loads(response.body)
    except ValueError:
        return set()
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return set()
    return {str(e.get("code")) for e in errors if isinstance(e, dict)}


# Discord refuses a message longer than this many characters.
DISCORD_MAX = 2000
# The longest `retry_after` a 429 may ask for and still get its one retry. Discord limits a
# webhook to about five requests in two seconds, so a post of several messages can meet one.
DISCORD_RETRY_MAX_S = 10.0

# Sends one POST: the URL, the body and the headers. `urllib_post`, or the tests' fake.
Sender = Callable[[str, bytes, Mapping[str, str]], Response]


class Discord:
    """Posts messages to one Discord webhook, and never names the webhook's URL.

    The URL is the credential, so every error says what failed and leaves the URL out.
    """

    def __init__(
        self,
        webhook: str,
        send: Sender = urllib_post,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Holds the webhook, the sender and the pause before a rate-limited retry.

        Args:
            webhook: the webhook URL, from the `DEPLOY_DISCORD_WEBHOOK` repository secret.
            send: sends one POST.
            sleep: waits the given seconds.
        """
        self._webhook = webhook.strip()
        self._send = send
        self._sleep = sleep

    def post(self, content: str) -> None:
        """Post `content` as one message.

        A 429 gets one retry after its `retry_after`, when that is at most
        DISCORD_RETRY_MAX_S.

        Raises:
            ForgeError: the webhook is unset, the message is over DISCORD_MAX characters,
                no answer came, or Discord answered anything but a 2xx.
        """
        if not self._webhook:
            raise ForgeError("DEPLOY_DISCORD_WEBHOOK is empty or unset")
        if len(content) > DISCORD_MAX:
            raise ForgeError(
                f"a message of {len(content)} characters is over Discord's {DISCORD_MAX}"
            )
        body = json.dumps({"content": content}).encode()
        headers = {"Content-Type": "application/json"}
        response = self._send(self._webhook, body, headers)
        wait = _retry_after(response) if response.status == 429 else None
        if wait is not None and wait <= DISCORD_RETRY_MAX_S:
            self._sleep(wait)
            response = self._send(self._webhook, body, headers)
        if not 200 <= response.status < 300:
            raise ForgeError(f"the Discord webhook answered HTTP {response.status}")


def _retry_after(response: Response) -> float | None:
    """The seconds a 429's JSON `retry_after` asks for, or None when it names none."""
    try:
        wait = json.loads(response.body).get("retry_after")
    except ValueError, AttributeError:
        return None
    return float(wait) if isinstance(wait, int | float) and wait >= 0 else None
