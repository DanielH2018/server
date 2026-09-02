#!/usr/bin/env python3
"""Check that staging's services ANSWER the way they are supposed to, not just that they start.

SLICE 2 OF PHASE C (docs/staging-phase-c.md, Decision 2). Wired to nothing: slice 3 runs this
after a staging deploy and reports the verdict, slice 4 gates on it.

WHY A SECOND CHECK AT ALL. Slice 1's verdict is the playbook's exit code, and that is a real
signal — the play carries its own rollout wait and a post-Available soak that hard-fails on a
restart-count delta. It still cannot see a route. On 2026-08-28 `ical-proxy` deployed to staging
with `68 ok, 3 changed, 0 failed` and the stabilisation gate passing while every one of its
routes returned 404: its `ClientIP` guard named a LAN address no NAT guest can present, so the
route was unsatisfiable by construction. Pod health cannot see that, and neither can
`rollout status`.

WHY THE EXPECTED STATUS IS DECLARED, NOT DEFAULTED. "Returns 200" is the wrong expectation for
half this subset. `freshrss` sits behind forward-auth and must answer **302** — a 200 there
would mean the Authelia middleware stopped applying, which is a failure that looks like success.
`ical-proxy` must answer 200 on `/calendar1.ics` and **404** on `/`, because its route carries a
`PathPrefix(/calendar)`. So the expectation belongs next to the service, in the inventory entry
that already describes its hostname and port.

WHY ROUTABILITY IS DERIVED. A service that gains a route later, or a new service added to the
subset, must not silently join the gate uncovered. `routable_services()` reads each role's own
manifest list and `missing_expectations()` fails when a routable service declares none. Growing
the subset therefore cannot quietly grow the blind spot.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.repo_paths import SCRIPTS

sys.path.insert(0, str(SCRIPTS / "validate"))

from validate_k8s_manifests import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    K8S_ROLES,
    load_yaml,
    register_ansible_filters,
    resolve_vars,
    role_defaults,
)

HOST = "daniel-stage"
HOST_VARS = ANSIBLE / "inventory" / "host_vars" / f"{HOST}.yml"
REMOTE_HOST = "daniel-server"
REMOTE_SCRIPT = Path(__file__).resolve().parent / "staging_expect_remote.sh"

# Same vocabulary as staging_gate.py, and for the same reason: a check that cannot be run is not
# a check that failed. Decision 4 needs those told apart.
PASS = 0
FAILED = 1
NO_VERDICT = 2

# The remote's own prep-failure code (no checkout, undecryptable domain).
PREP_FAILED = 70
SSH_FAILURE = 255


def host_context() -> dict:
    base = {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        **load_yaml(HOST_VARS),
        "playbook_dir": str(ANSIBLE),
    }
    return resolve_vars(base, base)


def staging_entries() -> list[dict]:
    return [
        c
        for c in (host_context().get("containers_list") or [])
        if c.get("platform") == "k8s"
    ]


def routable_services() -> set[str]:
    """Services whose deployed manifest list includes an IngressRoute, with staging's flags applied.

    Read from the role's own `manifests_files` rather than from its templates directory, because
    a per-cluster flag can retire a route that still has a template on disk. `registry` and
    `node-exporter` have no route at all and are correctly absent.
    """
    base = host_context()
    env = Environment(undefined=StrictUndefined)
    register_ansible_filters(env)

    routable: set[str] = set()
    for entry in staging_entries():
        role = entry["name"]
        tasks_file = K8S_ROLES / role / "tasks" / "main.yml"
        if not tasks_file.exists():
            continue
        ctx = {**role_defaults(role, base), **base, "container_item": entry}
        names: list[str] = []
        for task in yaml.safe_load(tasks_file.read_text()) or []:
            include = (
                task.get("ansible.builtin.include_role")
                or task.get("include_role")
                or {}
            )
            if include.get("name") != "k8s/manifests":
                continue
            value = (task.get("vars") or {}).get("manifests_files")
            if isinstance(value, str):
                value = ast.literal_eval(env.from_string(value).render(ctx))
            names += value or []
        if any("ingressroute" in n for n in names):
            routable.add(role)
    return routable


def missing_expectations() -> list[str]:
    """Routable services that declare no `staging_expect`. The verdict for the coverage guard."""
    declared = {e["name"] for e in staging_entries() if e.get("staging_expect")}
    return sorted(routable_services() - declared)


def expectations(services: set[str] | None = None) -> list[tuple[str, str, str, int]]:
    """(service, hostname, path, expected_status) for everything declared.

    `services` narrows this to what the caller actually deployed. Unfiltered, a tick gating
    node-exporter also measures traefik, authelia and ical-proxy — none of which it deployed,
    three of which are `k8s_autodeploy: false` and so can never be gated at all. A broken
    staging traefik then produced `staging: REJECTED ['node-exporter'] — deployed, but a
    service did not answer as declared`, naming a service with no declared expectations,
    because staging_verdict_summary interpolates the gated set rather than the failing one.
    That blames a bystander and corrupts the false-failure rate slice 4 waits on.

    Deliberately NOT applied to missing_expectations(): that is the coverage guard, and the
    services it most needs to cover are exactly the ones a tick can never gate.
    """
    out = []
    for entry in staging_entries():
        if services is not None and entry["name"] not in services:
            continue
        for want in entry.get("staging_expect") or []:
            out.append(
                (entry["name"], want["hostname"], want["path"], int(want["status"]))
            )
    return out


def compare(
    wanted: list[tuple[str, str, str, int]], observed: dict[tuple[str, str], int]
) -> list[str]:
    """The verdict: one line per expectation that was not met.

    A missing observation is a mismatch, not a skip — silence must never read as a pass.
    """
    problems = []
    for service, host, path, want in wanted:
        got = observed.get((host, path))
        if got is None:
            problems.append(f"{service}: {host}{path} was never measured")
        elif got != want:
            problems.append(f"{service}: {host}{path} answered {got}, expected {want}")
    return problems


def parse_observations(text: str) -> dict[tuple[str, str], int]:
    observed: dict[tuple[str, str], int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and re.fullmatch(r"\d{3}", parts[2]):
            observed[(parts[0], parts[1])] = int(parts[2])
    return observed


def measure(wanted: list[tuple[str, str, str, int]], timeout: float) -> tuple[int, str]:
    """Run the probes on daniel-server. Returns (returncode, stdout)."""
    # The probe list goes as ARGUMENTS after `--`; stdin carries the script, because `bash -s`
    # reads stdin as the program. Sending both down stdin means the loop reads nothing.
    probes: list[str] = []
    for _, host, path, _ in wanted:
        probes += [host, path]
    try:
        completed = subprocess.run(
            ["ssh", REMOTE_HOST, "bash", "-s", "--", *probes],
            input=REMOTE_SCRIPT.read_text(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SSH_FAILURE, ""
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    return completed.returncode, completed.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--timeout", type=float, default=180.0, help="seconds for the whole probe run"
    )
    parser.add_argument(
        "--services",
        help="comma-separated services to measure (default: every declared expectation). "
        "The coverage guard below ignores this and always checks the whole inventory.",
    )
    args = parser.parse_args()

    # UNFILTERED on purpose, even when --services narrows the measurement below. This is the
    # only live caller of the coverage guard — nothing in CI or prek runs it — and the services
    # it most needs to cover (traefik, authelia, registry) are `k8s_autodeploy: false`, so a
    # tick can never gate them. Scoping it would make a coverage gap on exactly those three
    # permanently invisible. It is a local manifest read with no network cost.
    uncovered = missing_expectations()
    if uncovered:
        print(
            f"staging-expect: {uncovered} are routable on {HOST} but declare no "
            f"staging_expect, so the gate would pass without checking them",
            file=sys.stderr,
        )
        # NO_VERDICT, not FAILED: a hole in this harness is not evidence that the change under
        # test is bad, and FAILED renders as "deployed, but a service did not answer as
        # declared" — a sentence about the change, describing a gap in the checker.
        return NO_VERDICT

    wanted = expectations(
        {s for s in args.services.split(",") if s} if args.services else None
    )
    if not wanted:
        print("staging-expect: nothing declared", file=sys.stderr)
        return NO_VERDICT

    rc, out = measure(wanted, args.timeout)
    if rc in (PREP_FAILED, SSH_FAILURE):
        print(f"staging-expect: could not run the probes (exit {rc})", file=sys.stderr)
        return NO_VERDICT

    problems = compare(wanted, parse_observations(out))
    for line in problems:
        print(f"staging-expect: {line}", file=sys.stderr)
    print(
        f"staging-expect: {len(wanted) - len(problems)}/{len(wanted)} expectations met"
    )
    return FAILED if problems else PASS


if __name__ == "__main__":
    sys.exit(main())
