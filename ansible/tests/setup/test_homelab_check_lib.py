"""homelab-check-lib.sh: the shared host-check library M6's batch B0 installs on every host.

The plan is docs/target-architecture/monitoring/m6-retire-kuma.md, *B0, B5, B7 and B8*. The
library carries `write_check_textfile` (M3's atomic write) and the three helpers moved from
kuma-push-lib.sh. Consumers render under other roles' tags, so two things have to hold or a
tag-scoped run leaves a host without the file: the copy task and its stamp are `tags: [always]`,
and every consumer sources the library with a failing guard instead of skipping silently.
"""

import re

import pytest
from _helpers import ANSIBLE, load_tasks, task_named
from _shell_render import render_shell_script
from lib.proc_testing import run

LIB = ANSIBLE / "roles/setup/initial_setup/files/homelab-check-lib.sh"
CRONS = ANSIBLE / "roles/setup/initial_setup/tasks/crons.yml"
LIB_PATH = "/usr/local/lib/homelab-check-lib.sh"
SOURCE_LINE = re.compile(
    r"^\s*(?:source|\.)\s+" + re.escape(LIB_PATH) + r"(?P<rest>.*)$"
)


def _bash(tmp_path, body, textfile_dir=None):
    """Run `body` with the library sourced and TEXTFILE_DIR pointed at a temp directory."""
    textfile_dir = textfile_dir or tmp_path / "textfile"
    textfile_dir.mkdir(exist_ok=True)
    return run(
        ["bash", "-c", f"source {LIB}\n{body}"],
        env={"TEXTFILE_DIR": str(textfile_dir)},
    )


def test_write_check_textfile_writes_the_four_series_and_detail_lines_atomically(
    tmp_path,
):
    result = _bash(
        tmp_path,
        "write_check_textfile demo 1 1760000000 900 'detail{check=\"demo\"} 7'",
    )
    assert result.returncode == 0, result.stderr
    out = tmp_path / "textfile"
    assert sorted(p.name for p in out.iterdir()) == ["demo.prom"]
    prom = out / "demo.prom"
    assert prom.stat().st_mode & 0o777 == 0o644
    lines = prom.read_text().splitlines()
    assert 'homelab_check_success{check="demo"} 1' in lines
    assert 'homelab_check_last_run_timestamp_seconds{check="demo"} 1760000000' in lines
    assert 'homelab_check_deadline_seconds{check="demo"} 900' in lines
    assert any(
        line.startswith('homelab_check_duration_seconds{check="demo"} ')
        for line in lines
    )
    assert 'detail{check="demo"} 7' in lines


def _duration(tmp_path, body):
    result = _bash(tmp_path, body)
    assert result.returncode == 0, result.stderr
    prefix = 'homelab_check_duration_seconds{check="demo"} '
    [line] = [
        line
        for line in (tmp_path / "textfile" / "demo.prom").read_text().splitlines()
        if line.startswith(prefix)
    ]
    return int(line.removeprefix(prefix))


def test_write_check_textfile_records_the_scripts_run_time_with_no_caller_change(
    tmp_path,
):
    # Assigning SECONDS sets bash's run-time counter, so this stands in for a 40s scan.
    assert (
        40 <= _duration(tmp_path, "SECONDS=40\nwrite_check_textfile demo 1 1 900") < 45
    )


def test_write_check_textfile_takes_the_callers_duration_when_it_sets_one(tmp_path):
    body = "SECONDS=40\nCHECK_DURATION_S=7 write_check_textfile demo 1 1 900"
    assert _duration(tmp_path, body) == 7


def test_write_check_textfile_failure_returns_non_zero_and_leaves_no_file(tmp_path):
    missing = tmp_path / "no-such-dir"
    result = run(
        ["bash", "-c", f"source {LIB}\nwrite_check_textfile demo 1 1 1"],
        env={"TEXTFILE_DIR": str(missing)},
    )
    assert result.returncode != 0
    assert not missing.exists()


def test_reachout_verdict_pages_when_the_wan_answers_and_skips_when_it_does_not(
    tmp_path,
):
    # ACCEPT and REJECT halves: a stub curl that succeeds, then one that fails.
    probe = 'logger() { :; }\nreachout_verdict t https://a.example\necho "$REACHOUT_STATUS|$REACHOUT_NOTE"'
    answers = _bash(tmp_path, f"curl() {{ return 0; }}\n{probe}")
    refuses = _bash(tmp_path, f"curl() {{ return 7; }}\n{probe}")
    assert answers.stdout == "down|\n"
    assert refuses.stdout == "up|skipped: WAN unreachable — \n"


def _wan_probe(tmp_path, urls, rcs):
    """Run `wan_reachable` with `curl` exiting `rcs` in sequence; return (rc, curl calls, logs).

    The stub records each call in a file, because a cron can call the gate from a command
    substitution, and `return`s because `exit` inside a shell function ends the whole shell.
    """
    calls = tmp_path / "calls"
    calls.write_text("")
    logs = tmp_path / "logs"
    logs.write_text("")
    result = _bash(
        tmp_path,
        f"""RCS=({" ".join(str(rc) for rc in rcs)})
curl() {{
  idx=$(wc -l < "{calls}")
  echo x >> "{calls}"
  return "${{RCS[$idx]}}"
}}
logger() {{ shift; echo "$*" >> "{logs}"; }}
wan_reachable test-tag {" ".join(urls)}
echo "rc=$?"
""",
    )
    assert result.returncode == 0, result.stderr
    return (
        result.stdout.strip(),
        len(calls.read_text().splitlines()),
        logs.read_text().splitlines(),
    )


_TWO_URLS = ["https://a.example", "https://b.example"]


def test_one_endpoint_answering_makes_the_wan_reachable(tmp_path):
    # The second provider is never asked: a healthy path costs one request.
    assert _wan_probe(tmp_path, _TWO_URLS, [0, 0]) == ("rc=0", 1, [])


def test_the_second_endpoint_still_answers_for_the_first_providers_own_outage(tmp_path):
    # Requiring both would turn one provider's outage into a skip for a real fault.
    assert _wan_probe(tmp_path, _TWO_URLS, [6, 0]) == ("rc=0", 2, [])


def test_every_endpoint_failing_is_the_only_no_wan_verdict(tmp_path):
    out, probes, logs = _wan_probe(tmp_path, _TWO_URLS, [6, 28])
    assert (out, probes) == ("rc=1", 2)
    assert any("no endpoint answered" in line for line in logs), logs


def test_no_endpoints_at_all_disables_the_gate(tmp_path):
    # An empty `wan_probe_urls` must suppress nothing, the direction boot_grace_active fails in.
    assert _wan_probe(tmp_path, [], []) == ("rc=0", 0, [])


# The host crons that fetch from the internet. A frozen list rather than a glob: a cron renamed or
# a call silently dropped is the regression this names, and the 2026-09-18 WAN outage paged each
# of them beside the WAN alert.
WAN_GATED_CRONS = [
    ("components", "github-checks", "github-ruleset-drift.sh.j2"),
    ("components", "docs-refresh", "docs-refresh.sh.j2"),
]
SENTINEL_URL = "https://sentinel.example"


def _probes_only_the_shared_list(script):
    """True when `script` calls `reachout_verdict`, and every call ends with SENTINEL_URL."""
    # A `command -v reachout_verdict` guard is not a call.
    calls = [
        line.strip()
        for line in script.splitlines()
        if line.lstrip().startswith("reachout_verdict ")
    ]
    return bool(calls) and all(line.endswith(f" {SENTINEL_URL}") for line in calls)


@pytest.mark.parametrize("cron", WAN_GATED_CRONS, ids=lambda cron: cron[2])
def test_every_internet_fetching_cron_probes_the_shared_endpoint_list(cron):
    # The URLs are arguments, so a cron that hardcoded its own pair would probe something the
    # bridge's WAN check never agreed to. The sentinel shows the call renders `wan_probe_urls`.
    script = render_shell_script(*cron, {"wan_probe_urls": [SENTINEL_URL]})
    assert _probes_only_the_shared_list(script)


def test_a_cron_with_no_call_or_with_its_own_urls_is_flagged():
    assert _probes_only_the_shared_list(f"reachout_verdict t {SENTINEL_URL}\n")
    assert not _probes_only_the_shared_list(f"# reachout_verdict t {SENTINEL_URL}\n")
    assert not _probes_only_the_shared_list("reachout_verdict t https://a.example\n")


def test_boot_grace_active_fails_open_on_a_host_past_the_grace(tmp_path):
    # A grace of 0 can never exceed uptime, so the guard must let the check run.
    result = _bash(tmp_path, "logger() { :; }\nboot_grace_active 0 t")
    assert result.returncode == 1


def test_the_library_copy_task_and_its_stamp_are_tagged_always():
    tasks = load_tasks(CRONS)
    copy = task_named(tasks, "Deploy the shared host-check library")
    stamp = task_named(tasks, "Record the deployed shared host-check library")
    assert copy["tags"] == ["always"]
    assert stamp["tags"] == ["always"]
    assert copy["ansible.builtin.copy"]["dest"] == LIB_PATH


def _unguarded_sources(text):
    """Line numbers where the library is sourced without a failing `||` guard."""
    lines = text.splitlines()
    bad = []
    for i, line in enumerate(lines):
        m = SOURCE_LINE.match(line)
        if not m:
            continue
        guarded = "||" in m["rest"] and "exit 1" in "\n".join(lines[i : i + 5])
        if not guarded:
            bad.append(i + 1)
    return bad


def test_a_source_without_a_failing_guard_is_caught_and_a_guarded_one_is_not():
    unguarded = f"source {LIB_PATH}\n"
    guarded = f"source {LIB_PATH} || {{\n  logger -t x missing\n  exit 1\n}}\n"
    assert _unguarded_sources(unguarded) == [1]
    assert _unguarded_sources(guarded) == []


def test_every_rendered_script_sources_the_library_with_a_failing_guard():
    offenders = []
    for path in sorted((ANSIBLE / "roles").glob("**/*")):
        if path.suffix not in {".j2", ".sh"} or path == LIB:
            continue
        for lineno in _unguarded_sources(path.read_text()):
            offenders.append(f"{path.relative_to(ANSIBLE)}:{lineno}")
    assert not offenders, f"sourced without `|| exit 1`: {offenders}"
