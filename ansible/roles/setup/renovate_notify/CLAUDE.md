# `setup/renovate_notify` — the Renovate reporting half

A daily systemd timer that queries the GitHub REST API for open Renovate PRs and the
Dependency Dashboard issue, then posts a Discord digest **only when what needs a human
changes**. It is the reporting half of the pair whose acting half is `setup/renovate_agent`:
this role says what is open and what needs manual work, that one does it.

Runs on `renovate_notify_host` (`inventory/group_vars/all.yml`, daniel-box) — exactly one
host, because the query is fleet-wide (it reads the GitHub repo, nothing host-local) and a
second host would duplicate every notification.

Invoked from `initial_setup.yml`, **not** `deploy.yml` — the role is not in
`containers_list`, so `./scripts/deploy.sh --tags renovate_notify` exits 2 on an unmatched
tag. Deploy it with:

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags renovate_notify
```

The GitOps deployer runs that command itself: `ansible/roles/setup/` is in
`_BROAD_SETUP_PREFIXES` (`roles/setup/gitops_deploy/files/deploy_changes.py`) and only the
three bring-up playbooks are manual, so a merged change here applies on the next tick without
a hand deploy.

**Every task notifies `Run renovate-notify once`.** Installing the scripts, the config, the
units or the alert unit kicks a real run — a real GitHub query and, if the fingerprint moved,
a real Discord post. That is deliberate: activation is fully IaC and posts the current backlog
once. It also means a deploy of this role is never silent, unlike its sibling, which has no
run-once handler because its run costs money and changes the fleet.

## What it watches, and why each arm exists

Three signals, all read from the same repo, each blind to what the others see:

| Arm | Reads | The failure it exists to catch |
|---|---|---|
| PR digest | open PRs (`actionable`) | a PR that will never merge on its own — automerge off, CI failing, conflicting, or conflicting against a deleted path |
| Dashboard staleness | the dashboard issue's `updated_at` | Renovate itself stopped. With no PRs the digest reads as a healthy cleared backlog, so this is the only fail-loud arm |
| Dashboard problems | the dashboard issue's **body** | a dependency that silently stops receiving updates while everything else reads green |

**The third arm parses two independently rendered blocks**, and missing either is invisible.
Renovate renders the `## Repository Problems` section from `appendRepoProblems`, and
per-dependency lookup failures from `getDepWarningsDashboard` — a blockquote callout appended
after the branch lists, outside that section entirely. `find_dashboard_problems` unions
`parse_repository_problems` with `parse_dependency_lookup_failures` for exactly that reason.
Two occurrences, one per block: karakeep's gcr.io image (2026-08) and
`registry.k8s.io/kube-state-metrics` (2026-09-02, finding #887), where the dependency dropped
out of "Pending Status Checks" between two reads with nothing but the callout to say why.

**A lookup failure does not touch the other two arms.** The dashboard still updates on
schedule, so staleness stays quiet, and a dependency Renovate cannot look up raises no PR, so
the digest stays quiet too.

## The stuck-pending clock, and the two ways it goes inert

A fourth arm reads the dashboard's `## Pending Status Checks` section and pages when an item
outlives its `minimumReleaseAge` plus `PENDING_GRACE_DAYS` (issue #886 — promtail sat 111 days
against a 7-day soak). It measures dwell, so it needs state: `/var/lib/renovate-notify/pending_seen.json`
holds each pending branch's first-seen epoch, and `write_pending_seen` writes it on every run
regardless of what was posted.

That state is the arm's single point of failure, and it fails silently in two shapes:

- **A renamed marker empties the parse.** Renovate renamed the item marker `approvePr-branch=`
  to `unpend-branch=` in September 2026, and the old name alone read 26 live items as zero
  (#1472). `pending_section_unreadable` and `dashboard_headers_unrecognized` are the runtime
  non-vacuity checks for that.
- **A lost `pending_seen.json` restarts every clock at zero.** `stale_pending` treats an item
  with no entry as first seen now — deliberately, so a first run cannot page for the whole
  section at once — which makes a wiped state file indistinguishable from a bootstrap. The arm
  then finds nothing for soak + grace: 14 days for a version bump, 10 for a digest one, with
  every run reporting healthy. `pending_state_lost` tells the two apart using `last_run` as the
  witness that this host has completed a run before, and the digest names the date the clocks
  become usable again (#1526).

**A reset posts once, then posts again the next day.** The reset rides the same fingerprint as
the other three arms; the following run rewrites the file, so the component drops and the
fingerprint moves a second time. The follow-up digest (or `CLEARED_MSG`) is the cost of one
dedupe for all four arms.

## Notification is fingerprint-gated, not state-gated

`fingerprint()` is the whole dedupe: the digest posts when the fingerprint changes and stays
silent when it does not. Three consequences worth knowing before you change anything here:

- **A standing problem goes quiet after one page.** That is intended. A *new* problem alongside
  an old one changes the string and re-pages.
- **`stuck` PRs carry a coarse age dimension** so a PR broken for weeks re-pages at 1/3/7/14
  days instead of paging once and ageing silently. `manual` PRs get none — they are waiting on
  a merge, not getting worse.
- **The fingerprint persists only on confirmed delivery.** A failed Discord post leaves the
  previous fingerprint in place, so the digest retries on the next run rather than being lost.

## Liveness, and what it does NOT cover

`ExecStartPost` beats a Kuma push monitor ("Renovate Notifier — Alive", token
`monitor_bridge_renovate_alive_push_token`) and runs only when `ExecStart` succeeded. A crash
therefore pages twice: `OnFailure=renovate-notify-alert.service` hits Discord immediately, and
the missed beat reds the monitor at its window (36h, per the unit's own comment).

**That monitor watches the notifier, not Renovate.** A green "Alive" monitor says this unit
ran; it says nothing about whether Renovate is still producing updates. The staleness arm is
what covers that, and it reaches you as a digest rather than as a monitor.

The alive marker also greens regardless of Discord *delivery*, which is why monitor-bridge
verifies the GitOps/Renovate webhook separately (`checks/notify.py`).

## The unit is sandboxed, and two things about that surprise people

Unlike `renovate-agent.service`, this one is confined — it queries HTTPS and writes only its
state dir, so `ProtectSystem=strict` + `ProtectHome=read-only` cost nothing. Two consequences
are load-bearing rather than incidental:

- **`UV_CACHE_DIR=/tmp/uv-cache` is required, not tidiness.** uv opens its cache for write at
  every startup, and the default lives under `$HOME`, which `ProtectHome=read-only` makes
  unwritable — without the override the unit dies with exit 2 before Python starts, every tick,
  and `OnFailure` pages for it. It points at `PrivateTmp` rather than `ReadWritePaths` because
  this unit parses attacker-influenceable API JSON, so a cache surviving between runs is state
  one compromised run could poison for the next.
- **The Kuma push URL reaches curl on stdin (`-K -`), never in argv.** `systemctl show -p
  ExecStartPost` publishes the unit line over the system bus and `/proc` here has no `hidepid`,
  so an argv-expanded token is readable by any local user. The 0600 `config.env` was never the
  leak; the shell expansion was. The same reasoning retired the embedded webhook from the alert
  unit (2026-08-23b review M5), which is why that unit is honestly 0644 now.

## Working on it

Logic lives in two pure modules, no I/O in either: `files/notify_logic.py` for the PR digest
and the dashboard-level arms, `files/pending_logic.py` for everything that parses, times or
renders a Pending-Status-Checks item. `notify_logic` imports `PENDING_HEADER` from
`pending_logic` and nothing goes the other way, so the pair cannot cycle. The
fetch/persist/post shell is `files/renovate_notify.py`, which imports from both. A new module
here needs BOTH ship-list sites in `tasks/main.yml`: the copy loop and the
`stamp_deployed_pairs` drift list. `host_lib.py` is copied in from `roles/setup/common/files/`
— edit it there, not here.

```bash
uv run pytest ansible/roles/setup/renovate_notify -q -n0
```

`--dry-run` logs what it would post and persists neither the fingerprint nor the liveness
marker, so it is the safe way to exercise the real query path:

```bash
uv run python ansible/roles/setup/renovate_notify/files/renovate_notify.py --dry-run
```

**A dry run proves only the paths today's dashboard exercises.** When the dashboard carries no
problems, it exercises the empty case of both parsers and says nothing about whether they fire
— the unit tests on captured dashboard bodies are what prove that. Any new parsing arm needs a
captured body committed as a fixture, verbatim, plus the paired `..._is_flagged` /
`..._is_clean` tests: a hand-approximated body only proves you parse your own approximation.

**`--check` fails at "Enable and start the timer", and that is not a bug in the role.** Check
mode writes no unit file, so systemd is asked about a `renovate-notify.timer` that does not
exist and reports `Could not find the requested service`. Every task before it reports
correctly. The sibling `renovate_agent` role behaves the same way.

## Verifying a deploy took effect

The role installs to `/opt/renovate-notify/` and its state to `/var/lib/renovate-notify/`.
`last_run` is written **only on clean completion**, so its mtime is the honest signal that the
deployed code ran end-to-end:

```bash
systemctl show renovate-notify.service -p ExecMainStatus -p ExecMainStartTimestamp
ls -l /var/lib/renovate-notify/          # last_run mtime; last_notified holds the fingerprint
```

An empty `last_notified` means nothing currently needs a human — not that the notifier is
broken.
