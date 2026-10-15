"""The deploy-pipeline exporter's four gauges, each as a pair, and the reads that feed them.

D2's item 12 names the pairs: DEPLOY_HOLD `none` against a `failed=` hold, PROD_DIGEST equal
to and different from `prod`'s digest, a green run `prod` contains against one it lacks, and a
missing variable that exports no series rather than a 0. The fixtures in
`_deploy_pipeline_fakes.py` answer each GET by URL, as GitHub and GHCR answered on 2026-10-15.
"""

import json
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

import deploy_pipeline
import forge
from _deploy_pipeline_fakes import (
    NEWER,
    NOW,
    OLDER,
    PROD,
    PROD_DIGEST,
    manifest_bytes,
    green_run,
    stamp,
    github,
)
from _fake_sources import FakeSources
from bridge.sources import Sources


@pytest.fixture
def piped(cfg):
    """The rendered config with a token set, so the family reads."""
    return replace(cfg, DEPLOY_PIPELINE_GITHUB_TOKEN="read-only-token")


def gauges(cfg, src) -> dict[str, float]:
    return deploy_pipeline.collect(cfg, src, NOW)[0]


def test_no_hold_exports_zero_for_both_hold_gauges(piped):
    found = gauges(piped, github(hold="none"))
    assert found["homelab_deploy_hold_active"] == 0
    assert found["homelab_deploy_hold_age_seconds"] == 0


def test_a_failed_hold_exports_one_and_its_age(piped):
    found = gauges(piped, github(hold="%s failed=bento-pdf" % PROD, hold_ago=5400))
    assert found["homelab_deploy_hold_active"] == 1
    assert found["homelab_deploy_hold_age_seconds"] == 5400


def test_a_prod_digest_equal_to_the_manifest_matches(piped):
    assert gauges(piped, github())["homelab_deploy_prod_digest_match"] == 1


def test_a_prod_digest_other_than_the_manifest_does_not_match(piped):
    found = gauges(piped, github(digest="sha256:" + "0" * 64))
    assert found["homelab_deploy_prod_digest_match"] == 0


def test_no_prod_tag_and_a_prod_digest_of_none_matches(piped):
    found = gauges(piped, github(digest="none", manifest=None, runs=[]))
    assert found["homelab_deploy_prod_digest_match"] == 1


def test_a_prod_tag_with_prod_digest_still_none_does_not_match(piped):
    assert gauges(piped, github(digest="none"))["homelab_deploy_prod_digest_match"] == 0


def test_a_green_run_prod_contains_is_not_unpromoted(piped):
    # OLDER was pushed after prod's run, so it is the candidate, and the compare says prod
    # holds it.
    runs = [green_run(OLDER, 3000, 2900), green_run(PROD, 7200, 7000)]
    src = github(runs=runs, compare="ahead")
    assert gauges(piped, src)["homelab_deploy_unpromoted_age_seconds"] == 0
    assert src.queries("get_json")[-1].endswith(
        "/compare/%s...%s?per_page=1" % (OLDER, PROD)
    )


def test_a_green_run_prod_lacks_sets_the_age_from_when_it_went_green(piped):
    runs = [
        green_run(NEWER, 1800, 1700),
        green_run(OLDER, 9000, 8800),
        green_run(PROD, 10800, 10600),
    ]
    src = github(runs=runs, compare="behind")
    assert gauges(piped, src)["homelab_deploy_unpromoted_age_seconds"] == 8800
    assert "/compare/%s...%s" % (OLDER, PROD) in src.queries("get_json")[-1]


def test_nothing_pushed_after_prod_reads_zero_without_a_compare(piped):
    src = github(runs=[green_run(PROD, 7200, 7000), green_run(OLDER, 9000, 8800)])
    assert gauges(piped, src)["homelab_deploy_unpromoted_age_seconds"] == 0
    assert not any("/compare/" in q for q in src.queries("get_json"))


def test_a_missing_variable_exports_no_series_rather_than_a_zero(piped):
    found, problems = deploy_pipeline.collect(
        piped, github(hold=None, digest=None), NOW
    )
    assert "homelab_deploy_hold_active" not in found
    assert "homelab_deploy_hold_age_seconds" not in found
    assert "homelab_deploy_prod_digest_match" not in found
    assert "the variable DEPLOY_HOLD does not exist" in problems
    # The read it does not feed still exports.
    assert found["homelab_deploy_unpromoted_age_seconds"] == 0


def test_a_failed_read_drops_only_the_gauges_it_feeds(piped):
    found, problems = deploy_pipeline.collect(piped, github(fail="/runs?"), NOW)
    assert set(found) == {
        "homelab_deploy_hold_active",
        "homelab_deploy_hold_age_seconds",
        "homelab_deploy_prod_digest_match",
    }
    assert [p for p in problems if p.startswith("unpromoted: ")]


def test_an_empty_token_reads_nothing_and_exports_nothing(cfg):
    assert cfg.DEPLOY_PIPELINE_GITHUB_TOKEN == ""
    src = FakeSources()
    found, problems = deploy_pipeline.collect(cfg, src, NOW)
    assert found == {}
    assert problems == ["DEPLOY_PIPELINE_GITHUB_TOKEN is empty, so nothing is read"]
    assert src.calls == []


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, *args) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_a_cycle_sends_three_gets_to_github_and_nothing_else(piped):
    """The transport the live `Sources` sends, through a fake opener: GETs only, and three."""
    runs = [green_run(NEWER, 600, 500), green_run(PROD, 7200, 7000)]
    answers = {
        "/actions/variables": {
            "variables": [
                {"name": "DEPLOY_HOLD", "value": "none", "updated_at": stamp(60)},
                {"name": "PROD_DIGEST", "value": PROD_DIGEST, "updated_at": stamp(60)},
            ]
        },
        "/actions/workflows/ci.yml/runs": {"workflow_runs": runs},
        "/compare/": {"status": "behind"},
        "ghcr.io/token": {"token": "anonymous-pull"},
    }
    sent: list[urllib.request.Request] = []

    def opener(request, timeout):
        sent.append(request)
        if request.full_url.endswith("/manifests/prod"):
            return _Response(manifest_bytes())
        [body] = [b for frag, b in answers.items() if frag in request.full_url]
        return _Response(json.dumps(body).encode())

    found, problems = deploy_pipeline.collect(piped, Sources(piped, opener=opener), NOW)
    assert problems == []
    assert found["homelab_deploy_unpromoted_age_seconds"] == 500
    assert {r.get_method() for r in sent} == {"GET"}
    assert len([r for r in sent if r.host == "api.github.com"]) == 3
    assert len([r for r in sent if r.host == "ghcr.io"]) == 2


def test_the_endpoint_serves_the_four_gauges_and_nothing_else(piped):
    exposition = deploy_pipeline.Exposition()
    found, _ = deploy_pipeline.collect(piped, github(), NOW)
    exposition.publish(found)
    server = exposition.serve(0)
    try:
        port = server.server_address[1]
        with urllib.request.urlopen("http://127.0.0.1:%d/metrics" % port) as resp:
            body = resp.read().decode()
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen("http://127.0.0.1:%d/" % port)
        assert missing.value.code == 404
        missing.value.close()
    finally:
        server.shutdown()
        server.server_close()
    series = [line for line in body.splitlines() if not line.startswith("#")]
    assert sorted(line.split()[0] for line in series) == sorted(
        name for name, _ in deploy_pipeline.GAUGES
    )
    assert "homelab_deploy_hold_active 0.0" in series


def test_the_names_match_what_promote_writes():
    """The pod cannot import forge.py, so each restated name is held to forge's here."""
    assert deploy_pipeline.REPOSITORY == forge.REPOSITORY
    assert deploy_pipeline.ARTIFACT == forge.ARTIFACT
    assert deploy_pipeline.REGISTRY == forge.REGISTRY
    assert deploy_pipeline.HOLD_VARIABLE == forge.HOLD_VARIABLE
    assert deploy_pipeline.MANIFEST_ACCEPT == forge.MANIFEST_ACCEPT
