"""The flaresolverr isolation probe must outlive prowlarr's own restart.

The probe's control connects to the `prowlarr` Service to prove that a failure to reach
flaresolverr is attributable to the NetworkPolicy rather than to DNS, the image, or a pod with
no network. That control races the very deploy it runs in: any change to this role's rendered
manifests fires an unwaited `rollout restart deploy/prowlarr` a few tasks earlier
(`roles/k8s/manifests/tasks/main.yml`), and prowlarr is `Recreate` with a 15s readiness
initialDelay — so at the moment the Job starts, the prowlarr Service routinely has no ready
endpoint.

With the original single-shot `nc`, that meant the probe reported CONTROL FAILED and failed the
deploy on precisely the runs that changed something, which is every run worth probing. The fix is
a bounded retry, and it only holds while three numbers stay in the right order:

    control retry budget  <  Job activeDeadlineSeconds  <  Ansible `kubectl wait --timeout`

Shrink the deadline below the retry budget and the Job is killed mid-control; shrink the wait
below the deadline and a real failure surfaces as an opaque wait timeout instead of the probe's
own diagnostic. Either regression reads as "the policy is broken" when the policy is fine, which
is the failure mode this file exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

from _k8s_render import rendered_docs

_REPO = Path(__file__).resolve().parents[2]
_TASKS = _REPO / "ansible/roles/k8s/prowlarr/tasks/main.yml"


def _probe_job() -> dict:
    for role, tpl, doc in rendered_docs():
        if role == "prowlarr" and doc.get("kind") == "Job":
            if doc["metadata"]["name"] == "flaresolverr-netpol-probe":
                return doc
    raise AssertionError(
        "flaresolverr-netpol-probe Job not found in the rendered manifests"
    )


def _control_script() -> str:
    containers = _probe_job()["spec"]["template"]["spec"]["containers"]
    return "\n".join("\n".join(c.get("command", [])) for c in containers)


def _retry_budget_seconds() -> int:
    """Worst-case seconds the control spends retrying, derived from the script itself."""
    script = _control_script()
    attempts = re.search(r"-ge\s+(\d+)\s*\]", script)
    connect = re.search(r"nc\s+-w\s+(\d+)\s+-z\s+prowlarr", script)
    sleep = re.search(r"sleep\s+(\d+)", script)

    assert attempts, "no bounded attempt ceiling found in the control loop"
    assert connect, "no `nc -w <n> -z prowlarr` control found"
    assert sleep, "no sleep between control retries found"

    return int(attempts.group(1)) * (int(connect.group(1)) + int(sleep.group(1)))


def _wait_timeout_seconds() -> int:
    text = _TASKS.read_text()
    match = re.search(r"job/flaresolverr-netpol-probe\s+--timeout=(\d+)s", text)
    assert match, (
        "no `kubectl wait ... job/flaresolverr-netpol-probe --timeout=<n>s` task found"
    )
    return int(match.group(1))


def test_control_retries_rather_than_shooting_once():
    """A single-shot control fails on every deploy that changed prowlarr's manifests."""
    script = _control_script()
    assert "until nc" in script, (
        "the probe's control must retry while prowlarr restarts. A bare `if ! nc ...` races the "
        "unwaited `rollout restart deploy/prowlarr` that any manifest change to this role fires."
    )


def test_deadline_outlives_the_retry_budget():
    budget = _retry_budget_seconds()
    deadline = _probe_job()["spec"]["activeDeadlineSeconds"]
    assert deadline > budget, (
        f"activeDeadlineSeconds ({deadline}s) must exceed the control's retry budget ({budget}s), "
        "or the Job is killed mid-control and the race comes back as a different error message."
    )


def test_wait_outlives_the_deadline():
    deadline = _probe_job()["spec"]["activeDeadlineSeconds"]
    timeout = _wait_timeout_seconds()
    assert timeout > deadline, (
        f"the Ansible wait ({timeout}s) must outlive activeDeadlineSeconds ({deadline}s), so a "
        "genuine failure surfaces as the probe's own message rather than an opaque wait timeout."
    )
