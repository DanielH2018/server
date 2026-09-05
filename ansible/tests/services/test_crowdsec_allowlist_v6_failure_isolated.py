"""Guards for the home-allowlist cron's v6-failure path (issue #1248).

`fail()` calls `exit 1`. It used to sit on the v6 fetch, ahead of the IPv4 sync — so a v6
routing loss took the IPv4 half down with it, even though nothing was wrong with IPv4.
Confirmed live on daniel-box 2026-09-05: systemd-networkd put `eno1` into `routable (failed)`
during the memory-exhaustion outage (`networkctl status eno1`), leaving the host with SLAAC
addresses (`valid_lft` still counting down) but no v6 default route — `curl -v` to
api6.ipify.org returned "Network is unreachable", not a DNS failure, and the cron logged
`status=down host has global IPv6 but failed to resolve public IPv6 from ipify` every 5
minutes from 16:08 onward with the IPv4 sync never running.

The fix carries the v6 fault forward as `V6_STATUS`/`V6_MSG` instead of exiting, falls
`CURRENT_PREFIX6` back to the stored value so `sync_entry` (empty `current` means "remove it")
never truncates the good entry, and reports the whole run `down` even when the IPv4 half is
clean — a stale v6 prefix can rotate out from under a green tile, so laundering the fault into
`up` is the failure this script exists to catch (same shape as docs-refresh's `GENERATORS_OK`).

Two things are executed (sourced by name, not pattern-matched), one is structural because there
is no clean way to execute a fast-path `if` in isolation without re-deriving it:

- `sync_entry` (unchanged, real production code) proves the empty-vs-fallback distinction: an
  empty `current` really does remove the stored entry and truncate the state file, and a
  `current` equal to `stored` really does leave both alone.
- `decide_final_push` (new, extracted in the same fix) proves the down-overrides-clean-sync
  decision, run against a stubbed `push`.
- The fast-path guard and the v6 block's exit-freedom are checked as text, each with a fixture
  showing the check can go red.
"""

import shlex
import subprocess
from pathlib import Path

from _helpers import ROLES

SCRIPT_PATH = ROLES / "k8s/crowdsec/templates/crowdsec-update-home-allowlist.sh.j2"
SCRIPT = SCRIPT_PATH.read_text()

SOURCED = ("decide_final_push", "sync_entry")


def _function(name: str) -> str:
    """The body of one shell function, `name() {` through its closing brace in column one."""
    import re

    match = re.search(rf"^{name}\(\) \{{[^\n]*\}}$", SCRIPT, re.M) or re.search(
        # `[^\n]*` after the opening brace tolerates this file's own style of trailing
        # `# arg names` comment on the same line (`push() { # up|down msg`, `sync_entry() { #
        # stored current state_file label`) — without it the multi-line branch never matches
        # either of them.
        rf"^{name}\(\) \{{[^\n]*\n.*?^\}}$",
        SCRIPT,
        re.M | re.S,
    )
    assert match, (
        f"{name}() is gone from crowdsec-update-home-allowlist.sh.j2 — sourced by name"
    )
    body = match.group(0)
    assert "{{" not in body, (
        f"{name}() gained a Jinja expression; it can no longer be sourced"
    )
    return body


PRELUDE = "\n".join(_function(name) for name in SOURCED)


def _bash(script: str, cwd: Path, calls_log: Path) -> subprocess.CompletedProcess:
    """Run `script` with the production functions in scope, plus fakes `sync_entry` calls into.

    `push`, `logger`, `cscli_lapi` and `fail` are stubbed here rather than sourced: they are the
    I/O boundary (Kuma, syslog, the LAPI exec) this test replaces, not the logic under test.
    """
    fakes = f"""
ALLOWLIST="home-ips"
push() {{ printf 'push\t%s\t%s\n' "$1" "$2" >> {shlex.quote(str(calls_log))}; }}
logger() {{ :; }}
cscli_lapi() {{ printf 'cscli\t%s\n' "$*" >> {shlex.quote(str(calls_log))}; }}
fail() {{ push down "$1"; exit 1; }}
"""
    return subprocess.run(
        ["bash", "-uo", "pipefail", "-c", f"{fakes}\n{PRELUDE}\n{script}"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


# --- sync_entry: the empty-vs-fallback distinction the fix depends on ---


def test_a_v6_value_equal_to_stored_leaves_the_entry_and_state_untouched(tmp_path):
    """ACCEPT: this is what the fix's fallback (`CURRENT_PREFIX6=$STORED_PREFIX6`) produces.

    `sync_entry`'s own equality check short-circuits before touching cscli or the state file —
    proving the fallback is enough to make a v6 fetch failure inert to the stored entry.
    """
    state_file = tmp_path / "ipv6"
    state_file.write_text("2001:db8:1::/64\n")
    calls_log = tmp_path / "calls.log"
    run = _bash(
        'sync_entry "2001:db8:1::/64" "2001:db8:1::/64" '
        f"{shlex.quote(str(state_file))} IPv6",
        tmp_path,
        calls_log,
    )
    assert run.returncode == 0, run.stderr
    assert state_file.read_text() == "2001:db8:1::/64\n"
    assert _calls(tmp_path) == []


def test_an_empty_v6_value_removes_the_stored_entry_and_truncates_state(tmp_path):
    """REJECT: what would happen if the fix's fallback were skipped and `current` stayed empty.

    This is `sync_entry`'s existing, correct behaviour for a host that genuinely lost its v6
    address — the fix works by never reaching this branch on a mere fetch failure, not by
    changing what the branch does. Without this half, "leaves state untouched" above would pass
    on a function that always no-ops.
    """
    state_file = tmp_path / "ipv6"
    state_file.write_text("2001:db8:1::/64\n")
    calls_log = tmp_path / "calls.log"
    run = _bash(
        f'sync_entry "2001:db8:1::/64" "" {shlex.quote(str(state_file))} IPv6',
        tmp_path,
        calls_log,
    )
    assert run.returncode == 0, run.stderr
    assert state_file.read_text() == ""
    assert any(c.startswith("cscli\tallowlists remove") for c in _calls(tmp_path)), (
        _calls(tmp_path)
    )


# --- decide_final_push: the down-overrides-clean-sync decision ---


def test_a_v6_failure_reports_down_with_its_reason_even_with_no_ipv4_change(tmp_path):
    """ACCEPT: the exact shape of the 2026-09-05 incident — IPv4 unchanged, v6 unreachable."""
    calls_log = tmp_path / "calls.log"
    run = _bash(
        'decide_final_push down "host has global IPv6 but failed to resolve public IPv6 '
        'from ipify" ""',
        tmp_path,
        calls_log,
    )
    assert run.returncode == 0, run.stderr
    assert _calls(tmp_path) == [
        "push\tdown\thost has global IPv6 but failed to resolve public IPv6 from ipify"
    ]


def test_a_v6_failure_still_reports_down_when_ipv4_also_rotated(tmp_path):
    """ACCEPT: an IPv4 change must not paper over a concurrent v6 failure with `up`."""
    calls_log = tmp_path / "calls.log"
    run = _bash(
        'decide_final_push down "v6 broke" "IPv4 1.2.3.4 -> 5.6.7.8"',
        tmp_path,
        calls_log,
    )
    assert run.returncode == 0, run.stderr
    assert _calls(tmp_path) == ["push\tdown\tv6 broke; IPv4 1.2.3.4 -> 5.6.7.8"]


def test_a_clean_run_still_reports_up_with_the_changes(tmp_path):
    """REJECT-shaped pair: a `down` status must not be the only one `decide_final_push` can
    produce, or the function would be indistinguishable from one hardcoded to `down`.
    """
    calls_log = tmp_path / "calls.log"
    run = _bash(
        'decide_final_push up "" "IPv4 1.2.3.4 -> 5.6.7.8"', tmp_path, calls_log
    )
    assert run.returncode == 0, run.stderr
    assert _calls(tmp_path) == [
        "push\tup\thome allowlist updated: IPv4 1.2.3.4 -> 5.6.7.8"
    ]


def test_the_pre_fix_unconditional_up_would_have_hidden_a_v6_failure(tmp_path):
    """REJECT: the pre-#1248 body, minimised, against the same down-with-no-changes case.

    Without this half, `test_a_v6_failure_reports_down_...` above would still pass against a
    function that never learned about V6_STATUS at all.
    """
    calls_log = tmp_path / "calls.log"
    run = _bash(
        'push() { printf "push\\t%s\\t%s\\n" "$1" "$2" >> '
        + shlex.quote(str(calls_log))
        + "; }\n"
        'push up "home allowlist updated: "',
        tmp_path,
        calls_log,
    )
    assert run.returncode == 0, run.stderr
    assert _calls(tmp_path) == ["push\tup\thome allowlist updated: "]


# --- structural: the fast path and the v6 block's freedom from early exits ---


def test_the_fast_path_requires_v6_status_up():
    """ACCEPT: without this guard, a v6 fetch failure with unchanged IPv4 takes the fast path
    and reports `up` — the fallback that keeps `sync_entry` inert (see above) makes
    CURRENT_PREFIX6 == STORED_PREFIX6 true in exactly that case.
    """
    assert (
        'if [ "$V6_STATUS" = up ] && [ "$CURRENT_IP" = "$STORED_IP" ] '
        '&& [ "$CURRENT_PREFIX6" = "$STORED_PREFIX6" ]; then' in SCRIPT
    )


def test_the_fast_path_without_the_v6_guard_is_flagged():
    """REJECT: the pre-fix condition, so the check above can be shown to fail on it."""
    naive = (
        'if [ "$CURRENT_IP" = "$STORED_IP" ] && '
        '[ "$CURRENT_PREFIX6" = "$STORED_PREFIX6" ]; then'
    )
    assert naive not in SCRIPT


def test_the_v6_block_never_exits_early():
    """ACCEPT: the v6 fetch/validate block sets V6_STATUS/V6_MSG and falls through — it must
    not call `fail` or `exit`, either of which would take the IPv4 sync down with it again.
    """
    start = SCRIPT.index("if ip -6 addr show scope global")
    # The block's own closing `fi`, before STORED_IP is read back out.
    end = SCRIPT.index("STORED_IP=$(cat", start)
    block = SCRIPT[start:end]
    assert "fail " not in block, block
    assert "exit" not in block, block


def test_a_block_that_calls_fail_on_v6_failure_is_flagged():
    """REJECT: the pre-#1248 body — `fail` on the v6 fetch — against the same style of check."""
    old_block = (
        "if ip -6 addr show scope global 2>/dev/null | grep -q inet6; then\n"
        "  if ! CURRENT_IP6=$(curl -sf https://api6.ipify.org); then\n"
        '    fail "host has global IPv6 but failed to resolve public IPv6 from ipify"\n'
        "  fi\n"
        "fi\n"
    )
    assert "fail " in old_block


def test_a_v6_fetch_failure_falls_back_to_the_stored_prefix():
    """ACCEPT: the line that makes the empty-current branch (tested above) unreachable on a
    mere fetch failure — `sync_entry` never sees an empty `current` unless v6 really is gone.
    """
    assert '[ "$V6_STATUS" = up ] || CURRENT_PREFIX6="$STORED_PREFIX6"' in SCRIPT
