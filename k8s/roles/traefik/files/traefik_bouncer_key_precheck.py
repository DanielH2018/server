"""traefik's precheck Job: refuse a bouncer key the CrowdSec LAPI rejects, before Traefik rolls.

Flux runs it from `templates/precheck/` as `traefik-precheck`, which `traefik` depends on
(#6095). A key rotation that skips `cscli bouncers delete k8straefik` leaves the LAPI holding
the old key's hash, and the new pod's startupProbe then fails on AppSec's 401. traefik rolls
`Recreate`, so that failure is an edge outage. A failed precheck holds `traefik` instead, and
the old pod keeps serving.

DECIDED: the request is `HEAD /v1/decisions` with `X-Api-Key`, never a GET. In CrowdSec
v1.8.1, `authPlain` in `pkg/apiserver/middlewares/v1/api_key.go` answers a HEAD by looking the
key's hash up alone: it creates no `k8straefik@<ip>` row and updates no `last_pull`. A GET
from this pod's IP would create a row and stamp its `last_pull`, and `bouncer_prune.py` would
then read the precheck as an edge pull and prune while the edge is not pulling. HEAD is also
the check AppSec runs, so this precheck sees the same verdict the startupProbe would. The
`ip` filter keeps the handler's decision query to one address.

Inputs come from the Job's environment: `LAPI_URL` and `BOUNCER_KEY`. Exit 0 when the LAPI
accepts the key, 1 otherwise, naming the cause.

Run: python3 traefik_bouncer_key_precheck.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/traefik/tests/test_traefik_bouncer_key_precheck.py
"""

import argparse
import os
import sys
import urllib.error
from collections.abc import Callable, Mapping

from flux_job_connect import Request, open_url

# Seconds the LAPI has to answer once connected. `open_url` retries the connect itself.
CALL_TIMEOUT = 10
# A loopback address, which holds no decision, so the handler's query reads no rows.
PROBE_IP = "127.0.0.1"


def check(env: Mapping[str, str], opener: Callable = open_url) -> str:
    """Ask the LAPI whether it accepts `BOUNCER_KEY`; the pass line, or raise `Rejected`."""
    url = f"{env['LAPI_URL'].rstrip('/')}/v1/decisions?ip={PROBE_IP}"
    request = Request(url, headers={"X-Api-Key": env["BOUNCER_KEY"]}, method="HEAD")
    try:
        with opener(request, timeout=CALL_TIMEOUT) as response:
            status = response.status
    except urllib.error.HTTPError as err:
        with err:
            status = err.code
    if status == 200:
        return "the LAPI accepts the bouncer key"
    if status == 403:
        raise Rejected(
            "the LAPI rejects the bouncer key (403). It still holds another key's hash for "
            "k8straefik: run `cscli bouncers delete k8straefik` in the crowdsec pod and "
            "restart it, so the entrypoint re-adds the bouncer from BOUNCER_KEY_k8straefik"
        )
    raise Rejected(f"the LAPI answered {status}, not 200, to the bouncer key check")


class Rejected(Exception):
    """The LAPI did not accept the key."""


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    opener: Callable = open_url,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    try:
        line = check(os.environ if env is None else env, opener)
    except Rejected as err:
        print(f"traefik-precheck: {err}", file=sys.stderr)
        return 1
    except OSError as err:
        # A refused connect past open_url's retries, a timeout or a dropped reply.
        print(f"traefik-precheck: the LAPI gave no answer: {err!r}", file=sys.stderr)
        return 1
    print(f"traefik-precheck: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
