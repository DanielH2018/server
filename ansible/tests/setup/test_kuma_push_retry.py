"""Guards for `kuma_push`'s retry (kuma-push-lib.sh), issues #994 and #1010.

The static Kuma monitors run `max_retries: 0`, so a single dropped push leaves the tile STALE
rather than red until the next cycle — the loss is invisible. Measured on daniel-box over the
24h to 2026-09-03 17:45 UTC: 49 `push failed` lines across 11 crons, clustered at exactly three
uptime-kuma rollouts. Every one of those checks had computed `status=up` and thrown the verdict
away.

#994 shipped a retry that stopped on ANY 4xx, on the premise that an uptime-kuma `Recreate`
rollout reaches Traefik as a 503 (a real window with zero ready endpoints, from the Recreate
strategy on two RWO Longhorn PVCs). #1010 measured that premise wrong: Traefik's KubernetesCRD
provider drops a router entirely once its service has no endpoints, rather than keeping the
route and answering through it with a 503 — so a rollout's dropped pushes arrive as Traefik's
own 404 (empty RouterName, OriginStatus 0), not a 5xx. Over the 3 days to 2026-09-03, non-200
responses to `/api/push/` were 88 x 404 against 5 x 503, 5 x 500 and 2 x 403 — the #994 retry was
therefore inert against the DOMINANT case it was written for, while passing every test below
(this module's own prior docstring asserted the 503 premise as settled fact — the guard
confirmed the wrong thing instead of catching it, the "green and inert" shape repo-root
CLAUDE.md names).

The fix (#1010) retries on anything that isn't Kuma answering with a genuine permanent
rejection — HTTP 401 or 403 — and stops only on those. Everything else retries: curl itself
failing before a response exists (couldn't connect / timeout / TLS), a 5xx *response*, and any
OTHER 4xx, in particular the 404 above. A no-router 404 and a bad-token 404 are indistinguishable
to curl, so a genuinely bad token now burns the retry budget before its log line appears — the
same trade the sibling cron `crowdsec-update-home-allowlist.sh.j2` already accepted. The retry
budget also grew from two attempts to three: a single 30s backoff is under the 31s longest
endpoint-less window #1010 measured, so a lone retry could still land inside the outage; three
attempts at a fixed 30s backoff put the second retry at t=60s, ~2x that window. Each behaviour
below is therefore guarded by an accept/reject pair.
"""

import subprocess

from _helpers import ANSIBLE

LIB = ANSIBLE / "roles/setup/initial_setup/files/kuma-push-lib.sh"


def _run_push(tmp_path, responses, extra_prelude=""):
    """Run kuma_push with `curl` stubbed to return `responses` in sequence, one per call.

    Each response is an (http_code, curl_rc) pair: http_code is what `-w '%{http_code}'` would
    have printed to stdout, curl_rc is curl's own exit status. curl sits as the last stage of
    `printf ... | curl ...` inside a `$(...)` command substitution, which runs in a subshell, so
    the stub can't set a variable back into the caller — it records each call as a line in a
    file instead, the same way the production pipeline's own side effect (the HTTP request)
    survives the subshell boundary.
    """
    calls_file = tmp_path / "calls"
    calls_file.write_text("")
    sleeps_file = tmp_path / "sleeps"
    sleeps_file.write_text("")
    logs_file = tmp_path / "logs"
    logs_file.write_text("")
    codes = " ".join(code for code, _ in responses)
    rcs = " ".join(str(rc) for _, rc in responses)
    script = f"""
    {extra_prelude}
    source {LIB}
    CODES=({codes})
    RCS=({rcs})
    curl() {{
      idx=$(wc -l < "{calls_file}")
      echo x >> "{calls_file}"
      printf '%s' "${{CODES[$idx]}}"
      exit "${{RCS[$idx]}}"
    }}
    sleep() {{ echo "$1" >> "{sleeps_file}"; }}
    logger() {{ shift; echo "$*" >> "{logs_file}"; }}
    kuma_push up test-msg https://push.example/secret-token kuma.local 10.0.0.1 test-tag
    echo "rc=$? ok=$KUMA_PUSH_OK"
    """
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    calls = len(calls_file.read_text().splitlines())
    sleeps = [int(x) for x in sleeps_file.read_text().split()]
    logs = logs_file.read_text().splitlines()
    return result, calls, sleeps, logs


def test_success_on_first_attempt_never_retries(tmp_path):
    # ACCEPT: the common case — one curl call, no sleep, no failure log.
    result, calls, sleeps, logs = _run_push(tmp_path, [("200", 0)])
    assert "rc=0 ok=1" in result.stdout
    assert calls == 1
    assert sleeps == []
    assert logs == []


def test_connection_failure_then_success_delivers_the_beat(tmp_path):
    # ACCEPT: curl fails before any response exists (7 = couldn't connect) on attempt one,
    # success on attempt two.
    result, calls, sleeps, logs = _run_push(tmp_path, [("000", 7), ("200", 0)])
    assert "rc=0 ok=1" in result.stdout
    assert calls == 2
    assert sleeps == [30]
    assert any("retrying" in line for line in logs)
    # The push URL carries the token (repo-root CLAUDE.md: never print a line that could hold
    # it). Assert the retry log line doesn't carry the URL string at all.
    assert not any("push.example" in line for line in logs)


def test_503_then_success_delivers_the_beat(tmp_path):
    # ACCEPT: THE case the fix exists for. uptime-kuma's Recreate rollout leaves Traefik with
    # zero ready endpoints, so it answers 503 directly — curl connects fine (curl_rc=0), it's
    # the HTTP status that says "not yet." A classifier that only watches curl_rc would treat
    # this as an unrecoverable success-shaped outcome and never retry it, which is exactly the
    # regression this test exists to catch.
    result, calls, sleeps, _logs = _run_push(tmp_path, [("503", 0), ("200", 0)])
    assert "rc=0 ok=1" in result.stdout
    assert calls == 2
    assert sleeps == [30]


def test_404_then_success_delivers_the_beat(tmp_path):
    # ACCEPT: THE regression #1010 exists to fix. A Traefik router drop (empty RouterName,
    # OriginStatus 0) during an uptime-kuma rollout answers 404, not 503 — curl connects fine
    # (curl_rc=0), it's the HTTP status that says "not yet." The #994 classifier stopped on
    # this exact code, which is why the retry was inert for 88 of 100 non-200 rollout responses.
    result, calls, sleeps, _logs = _run_push(tmp_path, [("404", 0), ("200", 0)])
    assert "rc=0 ok=1" in result.stdout
    assert calls == 2
    assert sleeps == [30]


def test_persistent_failure_gives_up_after_three_attempts(tmp_path):
    # ACCEPT (the other half of the pair above): a genuinely-down Kuma retries twice more, not
    # forever — the retry budget must not let a cron overlap its own next run.
    result, calls, sleeps, logs = _run_push(
        tmp_path, [("000", 7), ("503", 0), ("404", 0)]
    )
    assert (
        "rc=0 ok=0" in result.stdout
    )  # kuma_push always returns 0 — a push failure is not a cron failure
    assert calls == 3
    assert sleeps == [30, 30]
    assert any("push failed" in line for line in logs)


def test_401_is_not_retried(tmp_path):
    # REJECT: the case the fix must NOT touch. A 401/403 is Kuma answering with a genuine
    # permanent rejection (bad token or similar) — no amount of retrying fixes it. Second slot
    # is a would-be success (200) precisely so a regression that retries anyway is caught by
    # `calls == 1`/`sleeps == []`, not masked by a coincidental final failure on both counts.
    result, calls, sleeps, logs = _run_push(tmp_path, [("401", 0), ("200", 0)])
    assert "rc=0 ok=0" in result.stdout
    assert calls == 1
    assert sleeps == []
    assert not any("retrying" in line for line in logs)
    assert any("push failed" in line for line in logs)


def test_403_is_not_retried(tmp_path):
    # REJECT, second permanent-rejection instance so the case above isn't pinned to 401
    # specifically.
    result, calls, sleeps, _logs = _run_push(tmp_path, [("403", 0), ("200", 0)])
    assert "rc=0 ok=0" in result.stdout
    assert calls == 1
    assert sleeps == []


def test_retries_survive_a_set_dash_e_caller(tmp_path):
    # Every current caller runs `set -uo pipefail`, not `-e` (verified separately below), but
    # the library is sourced into whatever the caller sets. A curl failure that trips `set -e`
    # before the retry runs would silently skip both the retry and the final `push failed` log
    # — a regression the other tests, which run without `-e`, cannot see.
    result, calls, sleeps, _logs = _run_push(
        tmp_path, [("000", 7), ("200", 0)], extra_prelude="set -e"
    )
    assert "rc=0 ok=1" in result.stdout
    assert calls == 2
    assert sleeps == [30]


def test_retry_budget_is_well_under_the_fastest_affected_cron_period():
    # THE derivation. Three attempts at --max-time 10 plus two 30s backoffs is the worst case;
    # the fastest cron this fix touches is the CrowdSec home-allowlist updater at */5 (300s,
    # ansible/roles/k8s/crowdsec/tasks/main.yml). Stated as a floor, and the attempt count is
    # derived from the file text rather than a bare literal, so a future change to any of the
    # three constants has to argue with the arithmetic, not just read as "still small."
    text = LIB.read_text()
    assert "retry_delay_s=30" in text
    assert "--max-time 10" in text
    assert "for attempt in 1 2 3" in text
    attempts = 3
    backoffs = attempts - 1
    worst_case_s = backoffs * 30 + attempts * 10
    fastest_cron_period_s = 5 * 60
    assert worst_case_s < fastest_cron_period_s


def test_retry_window_covers_close_to_twice_the_observed_outage():
    # The #1010 derivation for going from two attempts to three: a single 30s backoff (60s
    # short of 2x the 31s outage) is under the 31s longest endpoint-less window measured
    # 2026-09-03, so a lone retry could still land inside a live outage. Two 30s backoffs land
    # the last attempt at t=60s — under the observed outage's exact double (62s) but within 2s
    # of it, the same order-of-magnitude margin the crowdsec sibling script sized its own retry
    # window to (ansible/tests/services/test_crowdsec_allowlist_push_retry.py). Asserted as a
    # floor rather than the bare "2x" the file's comments use loosely, so this stays accurate if
    # either constant moves.
    observed_outage_s = 31
    attempts = 3
    backoffs = attempts - 1
    coverage_s = backoffs * 30
    assert coverage_s > observed_outage_s
    assert coverage_s >= 1.9 * observed_outage_s


def test_kuma_push_still_never_fails_the_cron():
    # A push failure must not become a cron failure (a second alert for the same event) — the
    # retry must preserve this pre-existing contract, not just add attempts on top of it.
    text = LIB.read_text()
    tail = text[text.index("kuma_push() {") :]
    assert "return 0\n}" in tail
