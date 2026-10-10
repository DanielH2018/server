"""Guards for the host-side WAN gate in kuma-push-lib.sh.

monitor-bridge's `wan_reachable` gate suppresses the four bridge checks that reach the internet,
so one WAN outage pages once. It cannot reach the crons that run on the host and push their own
tiles: they are outside its loop. During a WAN outage those crons light four more tiles
beside the WAN tile — crowdsec-home-allowlist, github-ruleset-drift, release-staleness-check
and docs-refresh.

`wan_reachable` and `reachout_verdict` are the host-side half. The behaviour that has to stay
true, each held by an accept/reject pair so a rule that stopped matching fails its own test:

  * ONE endpoint answering is enough. Requiring both would let one provider's outage silence a
    cron reading the other, which is backwards — the same reason the Python gate gives.
  * a source that ANSWERS and refuses still reports `down`. The probe is what classifies, so an
    HTTP-level failure while the link is up must not become a skip.
  * `reachout_verdict` hands back BOTH the status and the message prefix, because the caller has
    to apply them to its own `logger` line as well as to the push: `probe.py alerts` rebuilds a
    host cron's DOWN episodes from `{job="syslog"} |= "status=down"`, so a skip that reached only
    the push would still read as an alert in the place an operator looks.
"""

import pytest
from _helpers import ANSIBLE
from lib.proc_testing import run

LIB = ANSIBLE / "roles/setup/initial_setup/templates/kuma-push-lib.sh.j2"


def _run(tmp_path, script_body, rcs):
    """Run `script_body` with `curl` stubbed to exit `rcs` in sequence, one per call.

    Returns (stdout, probe_calls, logger_lines). The stub records each call as a line in a file
    rather than in a variable: `wan_reachable`'s curl runs inside an `&&` list in the function's
    own shell here, but the crons call it from command substitutions too, so a file-backed
    counter is the shape that survives either.

    The stub `return`s rather than `exit`s: `exit` inside a shell function ends the whole shell,
    and `wan_reachable` calls curl in its own shell rather than behind a command substitution.
    """
    calls = tmp_path / "calls"
    calls.write_text("")
    logs = tmp_path / "logs"
    logs.write_text("")
    script = f"""
    source {LIB}
    RCS=({" ".join(str(rc) for rc in rcs)})
    curl() {{
      idx=$(wc -l < "{calls}")
      echo x >> "{calls}"
      return "${{RCS[$idx]}}"
    }}
    logger() {{ shift; echo "$*" >> "{logs}"; }}
    {script_body}
    """
    result = run(["bash", "-c", script])
    assert result.returncode == 0, result.stderr
    return (
        result.stdout,
        len(calls.read_text().splitlines()),
        logs.read_text().splitlines(),
    )


_PROBE = "wan_reachable test-tag https://a.example https://b.example; echo rc=$?"


def test_one_endpoint_answering_makes_the_wan_reachable(tmp_path):
    # The second provider is never asked: a healthy path costs one request.
    out, probes, logs = _run(tmp_path, _PROBE, [0, 0])
    assert "rc=0" in out
    assert probes == 1
    assert logs == []


def test_the_second_endpoint_still_answers_for_the_first_provider_own_outage(tmp_path):
    out, probes, _ = _run(tmp_path, _PROBE, [6, 0])
    assert "rc=0" in out, "one provider failing must not read as no WAN"
    assert probes == 2


def test_every_endpoint_failing_is_the_only_no_wan_verdict(tmp_path):
    out, probes, logs = _run(tmp_path, _PROBE, [6, 28])
    assert "rc=1" in out
    assert probes == 2
    assert any("no endpoint answered" in line for line in logs), logs


def test_no_endpoints_at_all_disables_the_gate(tmp_path):
    # An empty `wan_probe_urls` must suppress nothing, the direction boot_grace_active fails in.
    out, probes, _ = _run(tmp_path, "wan_reachable test-tag; echo rc=$?", [])
    assert "rc=0" in out
    assert probes == 0


_VERDICT = (
    "reachout_verdict test-tag https://a.example https://b.example\n"
    'echo "status=$REACHOUT_STATUS note=[$REACHOUT_NOTE]"'
)


def test_a_reachable_wan_leaves_the_failure_reported_as_down(tmp_path):
    out, _, _ = _run(tmp_path, _VERDICT, [0])
    assert "status=down" in out
    assert "note=[]" in out, "a real fault must carry no skip prefix"


def test_an_unreachable_wan_turns_the_failure_into_a_named_skip(tmp_path):
    out, _, _ = _run(tmp_path, _VERDICT, [6, 6])
    assert "status=up" in out
    assert "note=[skipped: WAN unreachable — ]" in out


# The four crons that push their own tiles, and the function each one has to reach the gate
# through. A frozen census rather than a glob: a cron renamed or a wiring silently dropped is the
# regression this guard exists to name, and an `all()` over an empty census passes.
WIRED_CRONS = {
    "roles/setup/k3s/templates/release-staleness-check.sh.j2": "reachout_verdict",
    "roles/setup/gitops_deploy/templates/github-ruleset-drift.sh.j2": "reachout_verdict",
    "roles/setup/initial_setup/templates/docs-refresh.sh.j2": "reachout_verdict",
    # Self-contained by design (its own tuned push curl), so it carries its own copy — held equal
    # to the library's by ansible/tests/services/test_crowdsec_allowlist_wan_skip.py.
    "roles/k8s/crowdsec/templates/crowdsec-update-home-allowlist.sh.j2": "wan_reachable",
}


@pytest.mark.parametrize("path,fn", sorted(WIRED_CRONS.items()))
def test_every_internet_fetching_cron_consults_the_wan_gate(path, fn):
    text = (ANSIBLE / path).read_text()
    called = [
        line
        for line in text.splitlines()
        if fn in line and not line.lstrip().startswith("#")
    ]
    assert called, f"{path} no longer calls {fn} outside a comment"


@pytest.mark.parametrize("path", sorted(WIRED_CRONS))
def test_every_wired_cron_renders_the_shared_endpoint_list(path):
    # The URLs are arguments, not a library constant, so a cron that hardcoded its own pair would
    # pass the test above while probing something the bridge never agreed to.
    if path.startswith("roles/k8s/crowdsec/"):
        pytest.skip(
            "renders one curl per URL from the list; its own guard reads that shape"
        )
    assert "wan_probe_urls" in (ANSIBLE / path).read_text()
