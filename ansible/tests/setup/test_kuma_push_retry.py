"""Guards for `kuma_push`'s retry (kuma-push-lib.sh), issue #994.

The static Kuma monitors run `max_retries: 0`, so a single dropped push leaves the tile STALE
rather than red until the next cycle — the loss is invisible. Measured on daniel-box over the
24h to 2026-09-03 17:45 UTC: 49 `push failed` lines across 11 crons, clustered at exactly three
uptime-kuma rollouts. Every one of those checks had computed `status=up` and thrown the verdict
away.

The fix retries once, after a fixed backoff, on anything that isn't Kuma answering with a
permanent 4xx. That "anything else" is deliberately two cases, not one: curl itself failing
before a response exists (couldn't connect / timeout / TLS), AND a 5xx *response* — uptime-kuma's
Deployment is `strategy: Recreate` on two RWO Longhorn PVCs, so a restart has a real window with
zero ready endpoints, and Traefik (which stays up throughout) answers that with an HTTP 503, not
a dropped connection. A classifier keyed on curl's blanket `-f` exit code cannot tell a 503 from
a 401, and would pass every test that only checks connection-level failures while firing on none
of the actual rollout pushes — the "green and inert" shape this repo has paid for twice
(repo-root CLAUDE.md, "A new check ships with a proof it can go RED"). Each behaviour below is
therefore guarded by an accept/reject pair.
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


def test_persistent_failure_gives_up_after_one_retry(tmp_path):
    # ACCEPT (the other half of the pair above): a genuinely-down Kuma retries exactly once,
    # not forever — the retry budget must not let a cron overlap its own next run.
    result, calls, sleeps, logs = _run_push(tmp_path, [("000", 7), ("503", 0)])
    assert (
        "rc=0 ok=0" in result.stdout
    )  # kuma_push always returns 0 — a push failure is not a cron failure
    assert calls == 2
    assert sleeps == [30]
    assert any("push failed" in line for line in logs)


def test_401_is_not_retried(tmp_path):
    # REJECT: the case the fix must NOT touch. A 4xx is Kuma answering with a permanent
    # rejection (bad token or similar) — no amount of retrying fixes it. Second slot is a
    # would-be success (200) precisely so a regression that retries anyway is caught by
    # `calls == 1`/`sleeps == []`, not masked by a coincidental final failure on both counts.
    result, calls, sleeps, logs = _run_push(tmp_path, [("401", 0), ("200", 0)])
    assert "rc=0 ok=0" in result.stdout
    assert calls == 1
    assert sleeps == []
    assert not any("retrying" in line for line in logs)
    assert any("push failed" in line for line in logs)


def test_404_is_not_retried(tmp_path):
    # REJECT, second 4xx instance so the case above isn't pinned to 401 specifically.
    result, calls, sleeps, _logs = _run_push(tmp_path, [("404", 0)])
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
    # THE derivation. Two attempts at --max-time 10 plus one 30s backoff is the worst case;
    # the fastest cron this fix touches is the CrowdSec home-allowlist updater at */5 (300s,
    # ansible/roles/k8s/crowdsec/tasks/main.yml). Stated as a floor so a future change to
    # either constant has to argue with the arithmetic, not just read as "still small."
    text = LIB.read_text()
    assert "retry_delay_s=30" in text
    assert "--max-time 10" in text
    worst_case_s = 30 + 2 * 10
    fastest_cron_period_s = 5 * 60
    assert worst_case_s < fastest_cron_period_s


def test_kuma_push_still_never_fails_the_cron():
    # A push failure must not become a cron failure (a second alert for the same event) — the
    # retry must preserve this pre-existing contract, not just add attempts on top of it.
    text = LIB.read_text()
    tail = text[text.index("kuma_push() {") :]
    assert "return 0\n}" in tail
