"""A staging service must answer the way it is supposed to, not merely start.

The case this exists for is measured, not hypothetical: on 2026-08-28 `ical-proxy` deployed to
staging with `68 ok, 3 changed, 0 failed` and the stabilisation gate passing while every one of
its routes returned 404. Its `ClientIP` guard named a LAN address no NAT guest can present, so
the route was unsatisfiable by construction.

Every case drives `compare` or `missing_expectations` — the same functions the runner drives.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from staging_expectations import (  # noqa: E402 — needs the path insert above
    compare,
    expectations,
    missing_expectations,
    parse_observations,
    routable_services,
    staging_entries,
)


def test_a_service_answering_as_declared_passes() -> None:
    wanted = [("freshrss", "freshrss", "/", 302)]
    assert compare(wanted, {("freshrss", "/"): 302}) == []


def test_the_ical_proxy_404_would_have_been_caught() -> None:
    """The rejecting half, replayed from the real failure.

    Before PR #548 both of ical-proxy's routes answered 404 while every other check read green.
    If this stops failing, the mechanism has stopped covering the case it was built for.
    """
    wanted = [
        ("ical-proxy", "ical-proxy", "/calendar1.ics", 200),
        ("ical-proxy", "ical-proxy", "/", 404),
    ]
    before_the_fix = {("ical-proxy", "/calendar1.ics"): 404, ("ical-proxy", "/"): 404}
    problems = compare(wanted, before_the_fix)
    assert len(problems) == 1, (
        f"expected exactly the /calendar1.ics mismatch, got {problems} — note the / → 404 "
        f"expectation must still PASS, since that 404 is the PathPrefix working"
    )
    assert "/calendar1.ics answered 404, expected 200" in problems[0]


def test_a_200_on_a_forward_authed_route_is_a_failure() -> None:
    """The direction matters. freshrss must redirect; a 200 means the Authelia middleware
    stopped applying, which is the failure that looks most like success."""
    wanted = [("freshrss", "freshrss", "/", 302)]
    assert compare(wanted, {("freshrss", "/"): 200}) == [
        "freshrss: freshrss/ answered 200, expected 302"
    ]


def test_an_unmeasured_expectation_is_a_failure_not_a_skip() -> None:
    """Silence must never read as a pass. If the probe never ran, the gate has no evidence and
    must say so rather than counting the expectation as met."""
    wanted = [("authelia", "auth", "/", 200)]
    assert compare(wanted, {}) == ["authelia: auth/ was never measured"]


def test_a_curl_failure_is_a_mismatch() -> None:
    """The remote prints 000 when curl cannot connect at all. That is not 200."""
    wanted = [("authelia", "auth", "/", 200)]
    assert compare(wanted, {("auth", "/"): 0}) == [
        "authelia: auth/ answered 0, expected 200"
    ]


def test_every_routable_staging_service_declares_an_expectation() -> None:
    """The coverage guard. A service that gains a route, or a new service added to the subset,
    must not silently join the gate unchecked."""
    missing = missing_expectations()
    assert not missing, (
        f"{missing} are routable on daniel-stage but declare no staging_expect, so the gate "
        f"would report success without ever checking them."
    )


def test_the_non_routable_services_are_not_demanded() -> None:
    """Pins the premise of the guard above. node-exporter has no IngressRoute and registry
    deliberately has none — an ingress would put push access on the LAN. If the derivation
    started claiming they are routable, the guard would demand expectations that cannot exist."""
    routable = routable_services()
    for service in ("node-exporter", "registry"):
        assert service not in routable, (
            f"{service} is being treated as routable, but it has no IngressRoute — the "
            f"manifest-list derivation is wrong."
        )


def test_the_routable_set_is_not_empty() -> None:
    """A derivation returning nothing would satisfy the coverage guard while checking nothing."""
    assert routable_services(), (
        "no staging service reads as routable, so the coverage guard is vacuous"
    )


def test_declared_expectations_are_well_formed() -> None:
    for service, host, path, status in expectations():
        assert host and not host.startswith("."), f"{service}: bad hostname {host!r}"
        assert path.startswith("/"), f"{service}: path {path!r} must start with /"
        assert 100 <= status <= 599, f"{service}: {status} is not an HTTP status"


def test_every_expectation_names_a_service_in_the_subset() -> None:
    """An expectation for a service staging does not run would be measured against prod's DNS
    or nothing at all."""
    names = {e["name"] for e in staging_entries()}
    for service, _, _, _ in expectations():
        assert service in names, (
            f"{service} declares an expectation but is not in the subset"
        )


def test_filtering_measures_only_the_named_services() -> None:
    """A tick measures what it deployed, so a bystander's failure cannot be blamed on it.

    staging_verdict_summary names the GATED set, never the failing one — so an unfiltered
    measurement of a broken traefik reported `REJECTED ['node-exporter']`, naming a service
    with no declared expectations at all.
    """
    everything = expectations()
    scoped = expectations({"freshrss"})
    assert scoped, "freshrss declares expectations; the filter dropped all of them"
    assert {s for s, _, _, _ in scoped} == {"freshrss"}
    assert len(scoped) < len(everything), (
        "the filter kept every expectation, so it is not filtering"
    )


def test_the_coverage_guard_is_not_narrowed_by_the_filter() -> None:
    """missing_expectations() takes no services argument, and must not grow one.

    It is the only live coverage check — nothing in CI or prek runs this script — and the three
    services it most needs to cover (traefik, authelia, registry) are `k8s_autodeploy: false`,
    so no tick can ever gate them. Scoping it to the gated set would make a coverage gap on
    exactly those three permanently invisible, which is the narrowing-a-derived-list failure.
    """
    assert not inspect.signature(missing_expectations).parameters, (
        "missing_expectations now takes arguments — if that is a scoping parameter, a coverage "
        "gap on a service no tick can gate becomes invisible."
    )


@pytest.mark.parametrize(
    "line,expected",
    [
        ("auth / 200", {("auth", "/"): 200}),
        ("ical-proxy /calendar1.ics 404", {("ical-proxy", "/calendar1.ics"): 404}),
        ("garbage", {}),
        ("auth / notanumber", {}),
    ],
)
def test_observation_parsing(line: str, expected: dict) -> None:
    """A malformed line is dropped rather than guessed at — and a dropped observation becomes
    'never measured' above, which fails. Parsing loosely would turn a broken probe into a pass."""
    assert parse_observations(line) == expected
