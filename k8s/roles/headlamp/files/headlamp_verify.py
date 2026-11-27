"""headlamp's verify Job: prove the `headlamp` NetworkPolicy refuses a pod that is not traefik.

`templates/verify/` holds the Job, and the renderer emits it into `headlamp-verify` (target
slice D5, *A verify conversion lands as inert code*). It replaced the `headlamp-netpol-probe`
Job that `tasks/main.yml` applied, and keeps that probe's two legs:

- the control: `traefik:<port>` must answer, so a pod with no network cannot pass;
- `headlamp:<port>` must refuse, or every pod in the namespace can browse the cluster as the
  headlamp ServiceAccount, a cluster-wide `view` binding, past Traefik and Authelia.

`flux_job_verify` runs both legs and decides what passes each. The pod's
`app: headlamp-verify` label is no peer of headlamp's fence, which admits `app: traefik` alone.
traefik's own policy opens its web port to every pod, so the control needs no peer either.
traefik answers a request with no matching route with 404, which still passes the control. A
refusal proves the policy only if headlamp is up, and `headlamp-verify` starts only once the
`headlamp` Kustomization reads Ready, which waits on the Deployment's rollout.

Inputs come from the Job's environment: `CONTROL_URL` and `HEADLAMP_URL`.
`HEADLAMP_RENDER_DIGEST8` is in the spec only to rename the Job, and the script prints it.
Exit 0 when both legs pass, 1 when one fails, naming it.

Run: python3 headlamp_verify.py (in the Job; it takes no arguments)
     uv run pytest k8s/roles/headlamp/tests/test_headlamp_verify.py
"""

import sys
from collections.abc import Mapping

from flux_job_verify import CONTROL_RETRY_SECONDS, control, isolated, main


def run(
    env: Mapping[str, str], control_retry_seconds: float = CONTROL_RETRY_SECONDS
) -> list[str]:
    """Run the control, then the isolation leg, and return one report line each.

    Args:
        env: The Job's environment.
        control_retry_seconds: How long the control retries a refused connect.

    Raises:
        flux_job_verify.Failed: The first leg that failed.
    """
    return [
        f"verifying headlamp's render {env.get('HEADLAMP_RENDER_DIGEST8', 'unknown')}",
        control(
            env["CONTROL_URL"],
            "Check DNS, the Service, and that traefik's policy still opens its web port "
            "to every pod.",
            control_retry_seconds,
        ),
        isolated(
            env["HEADLAMP_URL"],
            accepted="headlamp accepted the connection from a pod that is not traefik, so "
            "NetworkPolicy headlamp is not taking effect",
            answered="a pod that is not traefik. Headlamp serves a cluster-wide read API "
            "with no auth of its own, so every workload in the namespace can read the "
            "whole cluster.",
        ),
    ]


if __name__ == "__main__":
    sys.exit(main("headlamp-verify", __doc__, run))
