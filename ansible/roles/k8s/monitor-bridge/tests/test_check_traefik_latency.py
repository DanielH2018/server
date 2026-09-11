"""traefik_latency_verdict: the two guards that stopped it being the estate's flappiest monitor.

51 DOWN episodes in the 30 days to 2026-09-11, and a live DOWN that day naming headlamp at
"16% of 0.24 rps". Two independent faults produced them, so each has its own pair here: one
input the verdict must accept and one it must reject. A pair, because a guard that suppresses
everything and one that suppresses nothing are indistinguishable from the passing side alone.

The exemption pair and the count pair use the SAME ratio, differing only in the service name or
the request volume — so each test proves its own guard did the work rather than the shared
arithmetic.
"""

from verdicts.cluster import traefik_latency_verdict

# The deployed values, from templates/env-secret.yaml.j2 and bridge/config_service.py.
MIN_RPS = 0.05
SLOW_PCT = 5.0
BUCKET = "5.0"
MIN_SLOW = 3.0
STREAMS = ("homelab-headlamp-", "homelab-home-assistant-", "homelab-uptime-kuma-")
WINDOW = 300.0


def verdict(service, rps, slow_requests, stream_prefixes=STREAMS):
    """Run the verdict over one service at `rps` with `slow_requests` past the bucket.

    Takes a COUNT rather than a fraction: `rps * (1 - fraction)` loses the last bits, so a case
    meant to sit exactly on `min_slow_requests` lands just under it and the test asserts the
    wrong side of its own boundary.
    """
    return traefik_latency_verdict(
        {service: rps},
        {service: rps - slow_requests / WINDOW},
        MIN_RPS,
        SLOW_PCT,
        BUCKET,
        MIN_SLOW,
        stream_prefixes,
        WINDOW,
    )


def test_a_stream_service_past_the_bucket_is_clean():
    # The live 2026-09-11 DOWN, reproduced: headlamp at 0.24 rps with 16% past 5.0s. Measured
    # the same day, 94.2% of headlamp's requests finished under 0.1s and the whole service
    # averaged 1.7s, which puts the mean of that 16% near 53s — Kubernetes watch streams, timed
    # until the connection closes. Not slowness, so not a page.
    ok, msg = verdict("homelab-headlamp-324fda85bcf3c4ab5337@kubernetescrd", 0.24, 11.5)
    assert ok, msg
    # The exemption is stated, not silent: an exempt service is NOT measured, and a bare "ok"
    # would overstate what this check covers.
    assert "1 stream service(s) exempt" in msg


def test_the_same_shape_on_a_non_stream_service_is_flagged():
    # Identical numbers to the pair above, on a service that is not exempt. This is what proves
    # the exemption cleared the previous test — not the arithmetic, which is unchanged here.
    ok, msg = verdict("homelab-sonarr-1111111111111111@kubernetescrd", 0.24, 11.5)
    assert not ok
    assert "homelab-sonarr" in msg


def test_an_empty_stream_list_measures_every_service():
    # The exemption is configuration, not a hardcoded name list: cleared, headlamp is judged like
    # anything else. Without this, a typo'd TRAEFIK_STREAM_SERVICES would read green above.
    ok, msg = verdict(
        "homelab-headlamp-324fda85bcf3c4ab5337@kubernetescrd",
        0.24,
        11.5,
        stream_prefixes=(),
    )
    assert not ok
    assert "homelab-headlamp" in msg


def test_a_single_slow_request_in_the_window_is_clean():
    # One request past the bucket out of the 15 the floor admits is 6.7%, already over SLOW_PCT.
    # The ratio is arithmetically right and means nothing: this is the small-sample artifact
    # TRAEFIK_SLOW_MIN_REQUESTS exists to hold.
    ok, msg = verdict("homelab-littlelink-222@kubernetescrd", MIN_RPS, 1.0)
    assert ok, msg


def test_three_slow_requests_over_the_ratio_are_flagged():
    # Three is the first count that pages. Same service and the same ratio threshold as the test
    # above — only the count moved, which is what proves the count is the gate.
    ok, msg = verdict("homelab-littlelink-222@kubernetescrd", MIN_RPS, 3.0)
    assert not ok
    assert "homelab-littlelink" in msg


def test_a_genuinely_slow_service_still_pages():
    # The case the check exists for, kept alongside the two guards: a real backend degrading at a
    # volume where the ratio is meaningful. Neither guard may suppress this.
    ok, msg = verdict("homelab-prowlarr-333@kubernetescrd", 2.0, 300.0)
    assert not ok
    assert "homelab-prowlarr" in msg


def test_a_service_below_the_rps_floor_is_clean():
    # Too quiet to judge, and a different exclusion from the two guards: it is neither exempt nor
    # counted, so the green message names no exemption.
    ok, msg = verdict("homelab-littlelink-222@kubernetescrd", 0.001, 0.3)
    assert ok, msg
    assert "exempt" not in msg


def test_a_missing_bucket_series_is_a_config_fault_not_a_fast_service():
    # An `le=` that selected nothing must not read as "0 requests under the boundary", which
    # would page every service at once. Unchanged by this PR, pinned because the stream guard
    # now runs ahead of this branch and could have swallowed it.
    ok, msg = traefik_latency_verdict(
        {"homelab-sonarr-111@kubernetescrd": 1.0},
        {},
        MIN_RPS,
        SLOW_PCT,
        BUCKET,
        MIN_SLOW,
        STREAMS,
        WINDOW,
    )
    assert not ok
    assert "histogram buckets" in msg
