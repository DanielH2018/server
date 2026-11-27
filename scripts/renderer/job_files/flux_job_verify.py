"""The legs a NetworkPolicy verify Job shares, and the `main` that reports them.

A verify Job that proves a fence holds runs two kinds of leg:

- the control: a Service the Job's pod may reach must answer, so a pod with no network
  cannot pass. It retries a refusal for 60 seconds while kube-router learns the new pod, and
  passes on any outcome but a refused connect: an error status or a redirect still proves the
  pod reached the Service;
- an isolation leg: a fenced port must refuse. Inverted on purpose, it passes when its connect
  is refused or times out, which `flux_job_connect.refused` decides, and fails on any answer
  at all. An HTTP error status, a reset after accept and a read timeout all mean the server
  accepted the connection. Any other error also fails the leg, because it proves nothing
  about the policy, so a pod whose DNS lookups fail fails here rather than at the control.
  It does not retry: its refusal is the pass.

The role's script keeps its own targets and failure messages, which name what an open fence
exposes. A Job that imports this file carries it beside its script as a byte-identical copy,
as the Job census requires (`scripts/renderer/tests/_job_census.py`); `digest_job` adds it when
its caller passes `verify_legs=true`.

Typical usage example:

    from flux_job_verify import control, isolated, main

    def run(env):
        return [
            control(env["CONTROL_URL"], "Check DNS and the Service."),
            isolated(env["APP_URL"], accepted="...", answered="..."),
        ]

    if __name__ == "__main__":
        sys.exit(main("app-verify", __doc__, run))
"""

import argparse
import os
import sys
import urllib.error
from collections.abc import Callable, Mapping

from flux_job_connect import open_url, refused

# The socket timeout of each connect, in seconds.
TIMEOUT = 5
# How long the control retries a refused connect, in seconds: the connect helper's default.
CONTROL_RETRY_SECONDS = 60


class Failed(Exception):
    """A leg failed; the message names what is wrong."""


def _close(err: BaseException) -> None:
    """Close the response an `HTTPError` carries, which nothing else here reads."""
    if isinstance(err, urllib.error.HTTPError):
        err.close()


def control(url: str, hint: str, retry_seconds: float = CONTROL_RETRY_SECONDS) -> str:
    """Prove this pod reaches `url`, so a refusal elsewhere is the policy and not the pod.

    Args:
        url: A Service port the Job's pod may reach.
        hint: What to check when the control fails, appended to the failure message.
        retry_seconds: How long a refused connect is retried.

    Raises:
        Failed: The connect was still refused or timing out after the retries.
    """
    try:
        with open_url(url, timeout=TIMEOUT, retry_seconds=retry_seconds):
            pass
    except Exception as err:
        _close(err)
        if refused(err):
            raise Failed(
                f"CONTROL FAILED: cannot reach {url} either ({err}), so this Job proves "
                f"nothing about the policy. {hint}"
            ) from err
    return f"control ok: {url} answered"


def isolated(url: str, *, accepted: str, answered: str) -> str:
    """Prove the connect to `url` is refused or times out.

    Args:
        url: The fenced port under test.
        accepted: What an accepted connection means, as the clause after "Either".
        answered: Who answered and what that exposes, after "{url} answered".

    Raises:
        Failed: The server accepted the connection, or the connect failed for a reason that
            is no refusal, such as a DNS error.
    """
    try:
        with open_url(url, timeout=TIMEOUT, retry_seconds=0):
            pass
    except Exception as err:
        _close(err)
        if refused(err):
            return f"isolated: {url} refused, as intended"
        raise Failed(
            f"no refusal from {url}: {err!r}. Either {accepted}, or the connect failed "
            "for another reason, which proves nothing."
        ) from err
    raise Failed(f"POLICY NOT ENFORCED: {url} answered {answered}")


def main(
    name: str,
    doc: str | None,
    run: Callable[[Mapping[str, str]], list[str]],
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run a verify Job's legs and print one line each, prefixed with the Job's name.

    Args:
        name: The Job's name, which prefixes every line.
        doc: The script's docstring; its first line is the `--help` description.
        run: Runs the legs against the Job's environment and returns one line each.
        argv: The arguments; the Job passes none.
        env: The Job's environment; a test passes its own.

    Returns:
        0 when every leg passes, 1 when one raises `Failed`, which goes to stderr.
    """
    parser = argparse.ArgumentParser(description=doc and doc.splitlines()[0])
    parser.parse_args(argv)
    try:
        lines = run(os.environ if env is None else env)
    except Failed as err:
        print(f"{name}: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"{name}: {line}")
    return 0
