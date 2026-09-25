"""The crons that print on a successful run send that output to the journal, not to mail.

cron mails whatever a job writes to /var/mail/ubuntu, and nothing opens that spool. A per-run
success line there is not a record — it is one more message a real failure has to be found
among. #2418 silenced the infra-map cron's, #2444 drained the spool, and #2466 is the six that
still mailed: four crons, plus `secret-rotation-audit` and `live-drift`, which the kuma-check
timer migration had already moved to the journal by passing `kuma_check_cron_name`.

Each of the four routes BOTH streams through `logger -t <tag>`. Both, not stdout alone: the
cert-expiry line this exists to silence comes from `logging.basicConfig`, which writes to
stderr. None of the four loses an alert, because none of them alerted by mail — the reasoning
per cron is at its own task.

Those five are instances. The class check (#2487) reads every `ansible.builtin.cron` task that
installs a job, on both planes. Each job must route both streams of every stage, or appear in
`MAILS_ONLY_ON_FAILURE` with the reason its healthy run prints nothing. The allowlist fails in
both directions: an entry whose cron is gone, or whose job is now routed, is stale.

The judge splits a job at top-level `;`, `&&` and `||`, so an unbraced chain is judged stage
by stage rather than by its last pipe. It does not judge the stderr of a pipeline's earlier
stages, which prints only on failure. It cannot see inside a script, so an allowlist reason is
a claim about the script that a person checked. The Kuma and Healthchecks crons print on a
push that fails once and then succeeds on retry (#2511).

Run: uv run pytest ansible/tests/setup/test_cron_output_goes_to_the_journal.py
"""

import re

import pytest
from _helpers import ROLES, load_tasks, walk_tasks

# The four crons of #2466 that mail a per-run success line, by `ansible.builtin.cron` name, and
# the `logger` tag each must route to. Named rather than globbed: a census that finds its
# subject by pattern reads empty the day a file moves, and every job then passes by default.
# A rename fails `test_every_named_cron_still_exists` below rather than going quiet.
JOURNAL_ROUTED = {
    "Weekly secret rotation (auto tier)": "secret-rotate",
    "TLS cert-expiry watch": "cert-expiry",
    "Refresh generated docs": "docs-refresh",
    "Longhorn filesystem trim": "longhorn-trim-cron",
    # Not one of the four — added with the branch sweep (#2430), which made a previously
    # near-silent `--gc` job print a line per removed worktree and deleted branch.
    "Weekly git object-store repair": "worktree-sweep",
}


# `2>&1 | logger -t <tag>`, allowing the shell's own spacing. Matched as written because the
# redirect is the whole fix: `| logger` without `2>&1` leaves stderr mailing, which is exactly
# the cert-expiry case.
def _routing(tag: str) -> re.Pattern[str]:
    return re.compile(r"2>&1\s*\|\s*logger\s+-t\s+" + re.escape(tag) + r"\b")


def _cron_job_list() -> list[tuple[str, str]]:
    """(cron name, job string) for every `ansible.builtin.cron` task that installs a job."""
    jobs = []
    for path in sorted(ROLES.glob("*/*/tasks/**/*.yml")):
        for task in walk_tasks(load_tasks(path)):
            cron = task.get("ansible.builtin.cron")
            if isinstance(cron, dict) and cron.get("job"):
                jobs.append((str(cron["name"]), str(cron["job"])))
    return jobs


def _cron_jobs() -> dict[str, str]:
    return dict(_cron_job_list())


def test_every_named_cron_still_exists():
    """The census names four crons; all four are installed by the task files it reads."""
    assert set(JOURNAL_ROUTED) <= set(_cron_jobs())


@pytest.mark.parametrize(("name", "tag"), sorted(JOURNAL_ROUTED.items()))
def test_the_cron_sends_both_streams_to_the_journal(name, tag):
    job = _cron_jobs()[name]
    assert _routing(tag).search(job), f"{name!r} still mails its output: {job}"


def test_a_stdout_only_redirect_does_not_satisfy_the_check():
    """The RED half: `| logger` without `2>&1` is the bug, not the fix."""
    assert not _routing("cert-expiry").search(
        "uv run python scripts/watchers/cert_expiry.py | logger -t cert-expiry"
    )


def test_the_braced_chain_pipes_the_whole_cert_expiry_job():
    """`|` binds tighter than `&&`, so an unbraced chain would pipe only its last stage.

    The cert-expiry job is the only one of the four that is a chain rather than one command,
    and it is the one where getting this wrong reads as fixed while the `cd` and the
    credentials-file stage keep mailing.
    """
    job = _cron_jobs()["TLS cert-expiry watch"]
    assert job.startswith("{ cd "), job
    assert "; } 2>&1 | logger -t cert-expiry" in job, job


# ── every cron (#2487) ───────────────────────────────────────────────────────────────

_KUMA = "prints nothing on a healthy run; its verdict is a Kuma push"

# Crons whose job routes nowhere, each with the reason its healthy run prints nothing. Every
# reason was checked against the script when #2487 wrote this list. A cron that prints a
# success line does not belong here: route it instead.
MAILS_ONLY_ON_FAILURE = {
    "Sync peer Claude artifacts": (
        "rsync -a without -v and ssh in BatchMode print nothing on success, and a failure goes "
        "to logger -t sync-artifacts; only the run that reaches "
        "artifacts_sync_alert_after_failures consecutive failures writes to stderr, so a peer "
        "that stays down mails once per outage rather than every 5 minutes (#2467); its "
        "verdict is a Kuma push (#2516)"
    ),
    "Claude Code telemetry health": _KUMA,
    "configarr sync health": _KUMA,
    "CrowdSec remote allowlist": _KUMA,
    "CrowdSec AppSec verify": _KUMA,
    "janitorr error health": _KUMA,
    "qbittorrent prefs drift check": (
        "captures the reader's output and reports drift through logger alone"
    ),
    "Registry garbage collection": _KUMA + ", plus a Healthchecks /fail ping",
    "fake-remux health": _KUMA,
    "Longhorn backup health": _KUMA + ", plus a Healthchecks /fail ping",
    "Longhorn restore drill": (
        "its PASS line goes to logger alone; a failure prints to stderr, and "
        "longhorn-backup-health alerts on the drill's stale stamp"
    ),
    "daniel-box disk health": _KUMA + ", plus a Healthchecks /fail ping",
    "Release staleness drift check": _KUMA,
    "Off-box etcd snapshot": _KUMA + ", plus a Healthchecks /fail ping",
    "UPS secondary watchdog": _KUMA,
    "Pi SD-card health heartbeat": _KUMA + ", plus its own file log",
    "Pi container-recovery heartbeat": _KUMA + ", plus its own file log",
    "Homelab eval sweep": (
        "writes its output to log files; the EXIT trap reports to logger and Kuma"
    ),
    "Refresh homelab infrastructure map": (
        "stdout (`Wrote ...`) is discarded and stderr carries only a failure, for which the "
        "mail is the only alert (the job pings no Healthchecks check)"
    ),
    "Clear ansible log file": (
        "truncate prints nothing unless it fails, and then the mail is the only alert"
    ),
}

# Named members from both planes and from the files the census used to read, so a glob that
# stops matching fails here rather than passing over an empty census.
KNOWN_CRONS = frozenset(
    {
        "Sync peer Claude artifacts",  # roles/k8s
        "TLS cert-expiry watch",  # setup/initial_setup/tasks/crons.yml
        "Longhorn filesystem trim",  # setup/k3s/tasks/health-crons.yml
        "UPS secondary watchdog",  # setup/nut_host
        "Weekly AIDE file integrity check",  # setup/initial_setup/tasks/integrity.yml
    }
)
_CENSUS_FLOOR = 30

_JINJA = re.compile(r"\{\{.*?\}\}")

# A stage that ends by sending both streams somewhere other than cron: the journal, /dev/null,
# or a log file it appends to.
_ROUTED_STAGE = re.compile(
    r"(?:2>&1\s*\|\s*(?:\S*/)?logger\s+-t\s+\S+"
    r"|>\s*/dev/null\s+2>&1"
    r"|>>\s*\S+\s+2>&1)\s*$"
)

# Commands that print nothing unless they fail. `logger` covers the fallback stage of
# `... >/dev/null 2>&1 || logger -t tag 'msg'`.
_SILENT_COMMANDS = frozenset({"cd", "logger"})


def _stages(job: str) -> list[str]:
    """`job` split at every `;`, `&&` and `||` that sits outside quotes and `{ }` groups."""
    job = _JINJA.sub("X", job)
    stages, depth, quote, start, i = [], 0, None, 0, 0
    while i < len(job):
        char = job[i]
        if quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif depth == 0 and (char == ";" or job.startswith(("&&", "||"), i)):
            stages.append(job[start:i])
            i += 1 if char == ";" else 2
            start = i
            continue
        i += 1
    stages.append(job[start:])
    return [stage.strip() for stage in stages if stage.strip()]


def unrouted_stages(job: str) -> list[str]:
    """The stages of `job` whose output, on success, would reach cron and so the mail spool."""
    unrouted = []
    for stage in _stages(job):
        words = [w for w in stage.split() if not re.match(r"[A-Z_]+=", w)]
        command = words[0].rsplit("/", 1)[-1] if words else ""
        if command in _SILENT_COMMANDS or _ROUTED_STAGE.search(stage):
            continue
        unrouted.append(stage)
    return unrouted


def test_the_census_reads_both_planes():
    names = [name for name, _ in _cron_job_list()]
    assert len(names) >= _CENSUS_FLOOR, len(names)
    assert KNOWN_CRONS <= set(names), sorted(KNOWN_CRONS - set(names))


def test_no_two_installed_crons_share_a_name():
    """The census is keyed by name, so a duplicate would hide one job from every check."""
    names = [name for name, _ in _cron_job_list()]
    assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)


def test_every_cron_routes_its_output_or_says_why_it_mails():
    offenders = [
        f"{name!r} mails whatever these stages print: {stages}"
        for name, job in sorted(_cron_jobs().items())
        if (stages := unrouted_stages(job)) and name not in MAILS_ONLY_ON_FAILURE
    ]
    assert not offenders, (
        "Route each to `2>&1 | logger -t <tag>`, or add it to MAILS_ONLY_ON_FAILURE with the "
        "reason its healthy run prints nothing:\n" + "\n".join(offenders)
    )


def test_every_allowlist_entry_is_an_installed_cron_that_still_mails():
    jobs = _cron_jobs()
    stale = [
        name
        for name in MAILS_ONLY_ON_FAILURE
        if name not in jobs or not unrouted_stages(jobs[name])
    ]
    assert not stale, f"gone, or routed since and no longer needing an entry: {stale}"


@pytest.mark.parametrize(
    "job",
    [
        # The braced chain: one pipe covers every stage.
        "{ cd /srv && uv run python watch.py; } 2>&1 | logger -t cert-expiry",
        # Each stage routed on its own, without braces (fwupd).
        "fwupdmgr refresh >/dev/null 2>&1 ; fwupdmgr update 2>&1 | /usr/bin/logger -t fwupd",
        # The fallback stage is logger itself (fake-remux).
        "flock -w 600 /l scan.py >/dev/null 2>&1 || logger -t scan 'scan wrapper failed'",
        # A `cd` prints only on failure (the B2 crons).
        "cd /srv && PATH=/bin uv run probe.py b2-budget 2>&1 | logger -t b2-budget",
        # Appended to its own log (the hypervisor drill).
        "{{ runner }} >>{{ log_dir }}/cron.log 2>&1",
    ],
)
def test_a_job_routing_every_stage_is_clean(job):
    assert unrouted_stages(job) == []


@pytest.mark.parametrize(
    ("job", "stage"),
    [
        ("/usr/local/bin/health.sh", "/usr/local/bin/health.sh"),
        # Only the last stage of an unbraced chain is piped.
        ("prune -a && curl -fsS ping 2>&1 | logger -t prune", "prune -a"),
        # stderr still mails (the infra-map shape).
        (
            "uv run gen_map.py -o map.html >/dev/null",
            "uv run gen_map.py -o map.html >/dev/null",
        ),
        # A `;` inside a quoted argument is not a stage boundary, and the job is still bare.
        ("run.sh 'a;b'", "run.sh 'a;b'"),
    ],
)
def test_a_job_leaving_a_stage_unrouted_is_flagged(job, stage):
    assert unrouted_stages(job) == [stage]
