"""pihole's gravity reconcile as a Flux mutation Job: the declared blocklists, applied through the API.

`templates/verify/gravity.yaml.j2` runs this script. The renderer emits the Job into
`pihole-verify`, which starts only after `pihole-deferred` is Ready (target slice D5).

It does what the Ansible tasks "Reconcile the declared adlists and regex denies into
gravity.db" and "Rebuild gravity after a blocklist change" did, without an exec into either pod.
Each FTL serves Pi-hole v6's REST API on its web port, and each instance has its own Service
(`pihole` and `pihole-2-web`), so the Job reaches each FTL by name:

- every declared adlist that is absent is added, and one whose `enabled` differs is updated;
- every declared regex deny that is absent is added;
- the declared state is read back, so a write the API accepted and did not store fails;
- if either instance changed, gravity rebuilds on both, so the two stay equivalent. Each run
  downloads big.oisd and takes about one to two minutes.

A second run with the same declaration writes nothing and rebuilds nothing.

Each instance gets one login and no retry, and its session is deleted on the way out. FTL caps
its API sessions (`webserver.api.max_sessions`), and pihole-exporter needs a free one on every
scrape, so a leaked session per run would eventually lock the exporter out.

The Job census (`scripts/renderer/tests/_job_census.py`) admits no `urllib.request`, so the
script connects only through `flux_job_connect`.

Environment:
    PIHOLE_TARGETS   space-separated `<instance>=<base URL>` pairs, one per FTL.
    PIHOLE_PASSWORD  the admin password, from the `pihole-env` Secret.
    DECLARED_PATH    the JSON file holding `adlists` and `regex_deny`, as the role's defaults
                     declare them.

Exit 0 when both instances match the declaration, 1 when anything fails, naming it.

Run: python3 pihole_gravity.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/pihole/tests/test_pihole_gravity.py
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping

from flux_job_connect import Request, open_url

# The socket timeout of each API request, in seconds.
TIMEOUT = 15
# The socket timeout of each read of gravity's streamed output, in seconds. gravity prints a
# line per step, but a slow list download can leave the stream quiet for a minute or more.
GRAVITY_READ_TIMEOUT = 300
# How long the first connect to each instance retries a refusal, in seconds. It waits out
# kube-router learning this new pod's address.
REACH_RETRY_SECONDS = 60
# The comment on every row the reconcile adds. It matches the rows the Ansible task wrote.
COMMENT = "ansible-declared"
# The line gravity.sh prints once the new database has replaced the old one
# (`gravity_swap_databases`: `echo -e "${OVER}  ${TICK} ${str}"`). A run that fails earlier
# leaves the old database in place and never prints it. FTL sets FORCE_COLOR only for
# `?color=true`, which this script never sends, so TICK is the plain `[✓]`.
GRAVITY_DONE = "[✓] Swapping databases"


class Failed(Exception):
    """The reconcile failed; the message names what is wrong."""


class Api:
    """One FTL's REST API, with the session ID it holds after `login`."""

    def __init__(self, name: str, base_url: str, opener: Callable = open_url) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._open = opener
        self._sid = ""
        self.logged_in = False

    def call(
        self,
        method: str,
        path: str,
        body: object = None,
        timeout: float = TIMEOUT,
        retry_seconds: float = 0,
    ) -> str:
        """Sends one request and returns the response body.

        Raises:
            urllib.error.URLError: The connect failed, or the API answered an error status.
        """
        headers = {"Accept": "application/json"}
        if self._sid:
            headers["X-FTL-SID"] = self._sid
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        with self._open(request, timeout=timeout, retry_seconds=retry_seconds) as resp:
            return resp.read().decode()

    def json(self, method: str, path: str, body: object = None) -> dict:
        text = self.call(method, path, body)
        return json.loads(text) if text.strip() else {}

    def login(self, password: str) -> None:
        """Logs in once. The first connect retries a refusal; a rejected password does not.

        Raises:
            Failed: FTL refused the password or has no free session.
        """
        try:
            text = self.call(
                "POST",
                "/api/auth",
                {"password": password},
                retry_seconds=REACH_RETRY_SECONDS,
            )
        except urllib.error.HTTPError as err:
            err.close()
            raise Failed(
                f"{self.name}: login refused with HTTP {err.code}"
                + (
                    "; FTL has no free API session"
                    if err.code == 429
                    else "; check the pihole-env Secret"
                )
            ) from err
        session = (json.loads(text) if text.strip() else {}).get("session") or {}
        if not session.get("valid"):
            raise Failed(f"{self.name}: login answered no valid session")
        # FTL answers a server with no password set with a valid session and no SID.
        self._sid = session.get("sid") or ""
        self.logged_in = True

    def logout(self) -> None:
        if self._sid:
            self.call("DELETE", "/api/auth")
        self._sid = ""
        self.logged_in = False

    def adlists(self) -> dict[str, dict]:
        lists = self.json("GET", "/api/lists?type=block").get("lists") or []
        return {item["address"]: item for item in lists}

    def regex_denies(self) -> set[str]:
        domains = self.json("GET", "/api/domains/deny/regex").get("domains") or []
        return {item["domain"] for item in domains}

    def write(self, method: str, path: str, body: Mapping) -> None:
        """Sends a write and fails on any error FTL reports inside a success answer."""
        answer = self.json(method, path, body)
        errors = (answer.get("processed") or {}).get("errors") or []
        if errors:
            raise Failed(
                f"{self.name}: {method} {path} reported "
                + "; ".join(f"{e.get('item')}: {e.get('error')}" for e in errors)
            )

    def gravity(self) -> str:
        """Runs `pihole -g` and returns its output, read to the end of the stream."""
        return self.call("POST", "/api/action/gravity", timeout=GRAVITY_READ_TIMEOUT)


def list_path(address: str) -> str:
    """The path of one adlist. Its address is a URL, so every reserved character is encoded."""
    return f"/api/lists/{urllib.parse.quote(address, safe='')}?type=block"


def drift(api: Api, declared: Mapping) -> list[str]:
    """Each way the instance differs from the declaration, as a line naming it."""
    lines = []
    live = api.adlists()
    for entry in declared["adlists"]:
        address, enabled = entry["address"], bool(entry["enabled"])
        if address not in live:
            lines.append(f"adlist {address} is absent")
        elif bool(live[address].get("enabled")) != enabled:
            lines.append(f"adlist {address} has enabled={not enabled}")
    regex = api.regex_denies()
    lines += [
        f"regex deny {r} is absent" for r in declared["regex_deny"] if r not in regex
    ]
    return lines


def reconcile(api: Api, declared: Mapping) -> list[str]:
    """Writes what differs, then reads it back. Returns one line per write; none means a match.

    Mirrors the Ansible task: an absent adlist is added, an enabled flag that differs is set,
    an absent regex deny is added. Nothing is ever deleted.

    Raises:
        Failed: A write was refused, or the read-back still differs.
    """
    live = api.adlists()
    lines = []
    for entry in declared["adlists"]:
        address, enabled = entry["address"], bool(entry["enabled"])
        current = live.get(address)
        if current is None:
            api.write(
                "POST",
                "/api/lists?type=block",
                {
                    "address": address,
                    "comment": COMMENT,
                    "groups": [0],
                    "enabled": enabled,
                },
            )
            lines.append(f"added adlist {address} (enabled={enabled})")
        elif bool(current.get("enabled")) != enabled:
            # PUT replaces the row, so the comment and groups it already has go back as they were.
            api.write(
                "PUT",
                list_path(address),
                {
                    "comment": current.get("comment"),
                    "type": "block",
                    "groups": current.get("groups") or [0],
                    "enabled": enabled,
                },
            )
            lines.append(f"set adlist {address} enabled={enabled}")
    regex = api.regex_denies()
    for pattern in declared["regex_deny"]:
        if pattern not in regex:
            # FTL compiles the pattern before it stores it, which the SQL insert never did, so
            # an invalid one now fails here rather than sitting unused in gravity.db.
            api.write(
                "POST",
                "/api/domains/deny/regex",
                {"domain": pattern, "comment": COMMENT, "groups": [0], "enabled": True},
            )
            lines.append(f"added regex deny {pattern}")
    if lines:
        missed = drift(api, declared)
        if missed:
            raise Failed(f"{api.name}: NOT APPLIED: {'; '.join(missed)}")
    return [f"{api.name}: {line}" for line in lines]


def rebuild(api: Api) -> str:
    """Rebuilds gravity on one instance.

    Raises:
        Failed: Another rebuild is running, or this one stopped before the database swap.
    """
    try:
        output = api.gravity()
    except urllib.error.HTTPError as err:
        err.close()
        why = "a rebuild is already running" if err.code == 409 else "it failed"
        raise Failed(f"{api.name}: gravity answered HTTP {err.code}: {why}") from err
    if GRAVITY_DONE not in output:
        tail = " | ".join(line.strip() for line in output.strip().splitlines()[-3:])
        raise Failed(f"{api.name}: gravity never swapped in its new database: {tail}")
    return f"{api.name}: gravity rebuilt"


def targets(value: str) -> list[tuple[str, str]]:
    """The `<instance>=<url>` pairs of `PIHOLE_TARGETS`.

    Raises:
        Failed: The value is empty or a pair has no `=`.
    """
    pairs = [item.partition("=") for item in value.split()]
    if not pairs or any(not name or not sep or not url for name, sep, url in pairs):
        raise Failed(f"PIHOLE_TARGETS is {value!r}, not <instance>=<url> pairs")
    return [(name, url) for name, _sep, url in pairs]


def run(env: Mapping[str, str], opener: Callable = open_url) -> list[str]:
    """Reconciles every instance, then rebuilds gravity on all of them if any changed.

    Raises:
        Failed: The first step that failed.
    """
    with open(env["DECLARED_PATH"], encoding="utf-8") as fh:
        declared = json.load(fh)
    password = env.get("PIHOLE_PASSWORD", "")
    apis = [
        Api(name, url, opener) for name, url in targets(env.get("PIHOLE_TARGETS", ""))
    ]
    lines: list[str] = []
    try:
        for api in apis:
            api.login(password)
            lines += reconcile(api, declared)
        if lines:
            lines += [rebuild(api) for api in apis]
        else:
            lines.append(f"{len(apis)} instances match the declaration; nothing to do")
    finally:
        for api in apis:
            if api.logged_in:
                try:
                    api.logout()
                except OSError as err:
                    print(
                        f"pihole-gravity: {api.name}: logout failed: {err}",
                        file=sys.stderr,
                    )
    return lines


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        lines = run(env)
    except (Failed, OSError, ValueError, KeyError) as err:
        print(f"pihole-gravity: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"pihole-gravity: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
