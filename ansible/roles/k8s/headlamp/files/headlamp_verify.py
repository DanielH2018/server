"""headlamp's verify Job: prove the `headlamp` NetworkPolicy refuses a pod that is not traefik.

`templates/verify-inert/` holds the Job, and nothing renders it while Ansible owns headlamp.
headlamp's flip PR moves it to `templates/verify/`, and the renderer then emits it into
`headlamp-verify` (target slice D5, *A verify conversion lands as inert code*). It replaces the
`headlamp-netpol-probe` Job that `tasks/main.yml` applies, and keeps that probe's two legs:

- the control: `traefik:<port>` must answer, so a pod with no network cannot pass;
- `headlamp:<port>` must refuse, or every pod in the namespace can browse the cluster as the
  headlamp ServiceAccount, a cluster-wide `view` binding, past Traefik and Authelia.

The pod's `app: headlamp-verify` label is no peer of headlamp's fence, which admits
`app: traefik` alone. traefik's own policy opens its web port to every pod, so the control
needs no peer either. Inverted on purpose: the isolation leg passes when its connect is refused
or times out, which `flux_job_connect.refused` decides, and fails on any answer at all. An HTTP
error status, a reset after accept and a read timeout all mean headlamp accepted the
connection. Any other error also fails the leg, because it proves nothing about the policy, so
a pod whose DNS lookups fail fails here rather than at the control.

The control retries a refusal for 60 seconds, the helper's default, while kube-router learns
the new pod. It passes on any outcome but a refused connect: traefik answers a request with no
matching route with 404, which still proves the pod reached it. The isolation leg does not
retry: its refusal is the pass. A refusal proves the policy only if headlamp is up, and
`headlamp-verify` starts only once the `headlamp` Kustomization reads Ready, which waits on
the Deployment's rollout.

Inputs come from the Job's environment: `CONTROL_URL` and `HEADLAMP_URL`.
`HEADLAMP_RENDER_DIGEST8` is in the spec only to rename the Job, and the script prints it.
Exit 0 when both legs pass, 1 when one fails, naming it.

Run: python3 headlamp_verify.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/headlamp/tests/test_headlamp_verify.py
"""

import argparse
import os
import sys
import urllib.error
from collections.abc import Mapping

from flux_job_connect import open_url, refused

# The socket timeout of each connect, in seconds; the probe it replaces uses `nc -w 5`.
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
        url: traefik's Service port.
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
                "nothing about the policy. Check DNS, the Service, and that traefik's "
                "policy still opens its web port to every pod."
            ) from err
    return f"control ok: {url} answered"


def isolated(url: str) -> str:
    """Prove the connect to headlamp at `url` is refused or times out.

    Raises:
        Failed: headlamp accepted the connection, or the connect failed for a reason that
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
            f"no refusal from {url}: {err!r}. Either headlamp accepted the connection from "
            "a pod that is not traefik, so NetworkPolicy headlamp is not taking effect, or "
            "the connect failed for another reason, which proves nothing."
        ) from err
    raise Failed(
        f"POLICY NOT ENFORCED: {url} answered a pod that is not traefik. Headlamp serves a "
        "cluster-wide read API with no auth of its own, so every workload in the namespace "
        "can read the whole cluster."
    )


def run(
    env: Mapping[str, str], control_retry_seconds: float = CONTROL_RETRY_SECONDS
) -> list[str]:
    """Run the control, then the isolation leg, and return one report line each.

    Args:
        env: The Job's environment.
        control_retry_seconds: How long the control retries a refused connect.

    Raises:
        Failed: The first leg that failed.
    """
    return [
        f"verifying headlamp's render {env.get('HEADLAMP_RENDER_DIGEST8', 'unknown')}",
        control(env["CONTROL_URL"], control_retry_seconds),
        isolated(env["HEADLAMP_URL"]),
    ]


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    try:
        lines = run(os.environ if env is None else env)
    except Failed as err:
        print(f"headlamp-verify: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"headlamp-verify: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
