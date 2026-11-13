"""n8n's verify Job: prove the `n8n-broker` NetworkPolicy refuses a pod it does not name.

`templates/verify/` holds the Job, and the renderer emits it into `n8n-verify`, which Flux
applies after `n8n` (target slice D5). It replaced the `n8n-netpol-probe` Job that
`tasks/main.yml` applied until n8n's flip (#5546), and keeps that probe's three legs:

- the control: `traefik:80` must answer, so a pod with no network cannot pass;
- `n8n:<web port>` must refuse, or the pod skipped Authelia, CrowdSec and the rate-limit;
- `n8n:<broker port>` must refuse, or the pod could register a task runner.

The pod's `app: n8n-verify` label is a peer of neither n8n port, so the last two legs pass
only on a refusal. Inverted on purpose: an isolation leg passes when its connect is refused or
times out, which `flux_job_connect.refused` decides, and fails on any answer at all. An HTTP
error status, a reset after accept and a read timeout all mean n8n accepted the connection.
Any other error also fails the leg, because it proves nothing about the policy, so a pod
whose DNS lookups fail fails here rather than at the control.

The control retries a refusal for 60 seconds, the helper's default, while kube-router learns
the new pod. traefik's web entrypoint answers with a redirect to https, which urllib follows, so the
control passes on any outcome but a refused connect. An isolation leg does not retry: its
refusal is the pass. A refusal proves the policy only if n8n is up, and `n8n-verify` starts
only once the `n8n` Kustomization reads Ready, which waits on the Deployment's rollout.

Inputs come from the Job's environment: `CONTROL_URL`, `N8N_WEB_URL` and `N8N_BROKER_URL`.
`N8N_RENDER_DIGEST8` is in the spec only to rename the Job, and the script prints it. Exit 0
when every leg passes, 1 when one fails, naming it.

Run: python3 n8n_verify.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/n8n/tests/test_n8n_verify.py
"""

import argparse
import os
import sys
import urllib.error
from collections.abc import Mapping

from flux_job_connect import open_url, refused

# The socket timeout of each connect, in seconds; the probe it replaces used `nc -w 5`.
TIMEOUT = 5
# How long the control retries a refused connect, in seconds: the helper's own default.
CONTROL_RETRY_SECONDS = 60


class Failed(Exception):
    """A leg failed; the message names what is wrong."""


def _close(err: BaseException) -> None:
    """Close the response an `HTTPError` carries, which nothing else here reads."""
    if isinstance(err, urllib.error.HTTPError):
        err.close()


def control(url: str, retry_seconds: float) -> str:
    """Prove this pod reaches traefik, so a refusal below is the policy and not the pod.

    Args:
        url: traefik's web entrypoint.
        retry_seconds: How long a refused connect is retried.

    Raises:
        Failed: The connect was still refused or timing out after the helper's retries.
    """
    try:
        with open_url(url, timeout=TIMEOUT, retry_seconds=retry_seconds):
            pass
    except Exception as err:
        _close(err)
        if refused(err):
            raise Failed(
                f"CONTROL FAILED: cannot reach {url} either ({err}), so this Job proves "
                "nothing about the policy. Check DNS and the Service."
            ) from err
    return f"control ok: {url} answered"


def isolated(url: str, fence: str) -> str:
    """Prove the connect to `url` is refused or times out.

    Args:
        url: The n8n port under test.
        fence: Which peers the policy admits on that port, for the failure message.

    Raises:
        Failed: n8n accepted the connection, or the connect failed for a reason that is no
            refusal, such as a DNS error.
    """
    try:
        with open_url(url, timeout=TIMEOUT, retry_seconds=0):
            pass
    except Exception as err:
        _close(err)
        if refused(err):
            return f"isolated: {url} refused, as intended"
        raise Failed(
            f"no refusal from {url}: {err!r}. Either n8n accepted the connection from a pod "
            f"that is not {fence}, so NetworkPolicy n8n-broker is not taking effect (#1926), "
            "or the connect failed for another reason, which proves nothing."
        ) from err
    raise Failed(
        f"POLICY NOT ENFORCED: {url} answered a pod that is not {fence}. "
        "NetworkPolicy n8n-broker is not taking effect (#1926)."
    )


def run(
    env: Mapping[str, str], control_retry_seconds: float = CONTROL_RETRY_SECONDS
) -> list[str]:
    """Run the control, then both isolation legs, and return one report line each.

    Args:
        env: The Job's environment.
        control_retry_seconds: How long the control retries a refused connect.

    Raises:
        Failed: The first leg that failed.
    """
    return [
        f"verifying n8n's render {env.get('N8N_RENDER_DIGEST8', 'unknown')}",
        control(env["CONTROL_URL"], control_retry_seconds),
        isolated(env["N8N_WEB_URL"], "traefik or monitor-bridge"),
        isolated(env["N8N_BROKER_URL"], "n8n-runners"),
    ]


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    try:
        lines = run(os.environ if env is None else env)
    except Failed as err:
        print(f"n8n-verify: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"n8n-verify: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
