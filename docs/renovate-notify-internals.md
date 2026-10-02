# renovate_notify internals — the sandbox, the parsers and the module layout

`ansible/roles/setup/renovate_notify/CLAUDE.md` is the role doc, and it keeps the operating
rules: the four arms, the fingerprint gate, the two ways the stuck-pending clock goes inert.
This page is the working-out behind them — why the unit's sandbox needs two overrides, how the
dashboard parsers were measured, where the logic lives and what a dry run does not prove. A
session reads it when it edits that code, not on every touch of the role (#2989).

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

## The dashboard-problems arm parses two independently rendered blocks

Renovate renders the `## Repository Problems` section from `appendRepoProblems`, and
per-dependency lookup failures from `getDepWarningsDashboard` — a blockquote callout appended
after the branch lists, outside that section entirely. `find_dashboard_problems` unions
`parse_repository_problems` with `parse_dependency_lookup_failures` for exactly that reason,
and missing either block is invisible.

Two occurrences, one per block: karakeep's gcr.io image (2026-08) and
`registry.k8s.io/kube-state-metrics` (2026-09-02, finding #887), where the dependency dropped
out of "Pending Status Checks" between two reads with nothing but the callout to say why.

**A lookup failure does not touch the other two arms.** The dashboard still updates on
schedule, so staleness stays quiet, and a dependency Renovate cannot look up raises no PR, so
the digest stays quiet too.

## What each stuck-pending failure shape cost

The role doc names both shapes; these are the measurements behind them.

- **A renamed marker empties the parse.** Renovate renamed the item marker `approvePr-branch=`
  to `unpend-branch=` in September 2026, and the old name alone read 26 live items as zero
  (#1472). `pending_section_unreadable` and `dashboard_headers_unrecognized` are the runtime
  non-vacuity checks that catch an empty parse before it reads as a cleared backlog.
- **A lost `pending_seen.json` restarts every clock at zero.** The arm then finds nothing for
  soak plus grace: 14 days for a version bump, 10 for a digest one, with every run reporting
  healthy. A package in `pending_logic.py:FAST_DIGEST_SOAK_DAYS` waits 8 rather than 10: the two
  nginx alpine pins soak 1 day in `renovate.json` since #2886 —
  `tests/test_pending_soak.py::test_soak_constants_match_renovate_json` fails when the two
  disagree. The churn arm is blind for its
  own, longer window — a loss restarts the branch clocks too, so it can find nothing for
  `PENDING_CHURN_MULTIPLIER` times soak plus grace, 42 days for a version row (#3076).
  `pending_state_lost` tells a wiped state file from a bootstrap using `last_run` as
  the witness that this host has completed a run before, and the digest names all three dates
  the clocks become usable again (#1526).
- **A branch-keyed dwell over-reported a mutable tag.** Renovate reuses one branch across
  every re-push of a mutable tag, so a dwell keyed on the branch measured how long some digest
  had been pending: this arm reported the freshrss cache digest stuck 18.8 days when no
  single digest had been pending more than about 3 (#2886, #3076). Keying on the description
  instead would have under-reported to zero — nine of the 22 items live on 2026-09-02 were
  mutable-tag digest bumps whose description changes on every re-push — so the fix keeps both
  clocks: `content_key` keys the digest clock `stale_pending` times, and the branch key stays
  for `churning_pending`. The digest clock alone cannot page for `nginx:alpine`, re-pushed about
  every 3.6 days against a 1-day soak and 7-day grace, which is why the churn arm exists:
  `PENDING_CHURN_MULTIPLIER` times soak plus grace, so three consecutive allowances pass with
  the item never once leaving the section before it pages. A legacy state file's branch-only
  entry seeds its digest clock from the branch clock rather than from now, so the upgrade run
  does not blind the arm for 14 days the way a lost file does.
- **A grouped row took the wrong soak.** `groupSingleUpdates: true` (#2646) titles a
  single-dependency update with its GROUP name, so the row reads `Update k8s image
  ghcr.io/haveagitgat/tdarr (manual — ...)` where an ungrouped digest row says `Docker digest
  to df221db`. Those rows took the 7-day version soak and alerted four days late — five sat
  10.3 days on 2026-09-28 against their 10-day threshold, unnamed by the digest (#2885).
  `pending_logic.py:GROUPED_TITLE_MARKER` is the parenthetical `item_soak_days` matches on; the
  `DECIDED:` comment beside it accepts a grouped VERSION bump paging four days early as the cost.

**A reset posts once, then posts again the next day.** The reset rides the same fingerprint as
the other three arms; the following run rewrites the state file, so the component drops and the
fingerprint moves a second time. The follow-up digest (or `CLEARED_MSG`) is the cost of one
dedupe for all four arms.

## Where the logic lives

Two pure modules, no I/O in either: `files/notify_logic.py` for the PR digest and the
dashboard-level arms, `files/pending_logic.py` for everything that parses, times or renders a
Pending-Status-Checks item. `notify_logic` imports `PENDING_HEADER` from `pending_logic` and
nothing goes the other way, so the pair cannot cycle. The fetch/persist/post shell is
`files/renovate_notify.py`, which imports from both. `host_lib.py` is copied in from
`roles/setup/common/files/` — edit it there, not here.

## What a dry run does not prove

`--dry-run` logs what it would post and persists neither the fingerprint nor the liveness
marker, so it is the safe way to exercise the real query path. It proves only the paths
today's dashboard exercises. When the dashboard carries no problems, it exercises the empty
case of both parsers and says nothing about whether they fire — the unit tests on captured
dashboard bodies are what prove that. Any new parsing arm needs a captured body committed as a
fixture, verbatim, plus the paired `..._is_flagged` / `..._is_clean` tests: a hand-approximated
body only proves you parse your own approximation.

**`--check` fails at the "Enable and start the timer" task, and that is not a bug in the
role.** Check mode writes no unit file, so systemd is asked about a `renovate-notify.timer`
that does not exist and reports `Could not find the requested service`. Every task before it
reports correctly. The sibling `renovate_agent` role behaves the same way.

## What the liveness monitor does not cover

`ExecStartPost` beats a Kuma push monitor ("Renovate Notifier — Alive") and runs only when
`ExecStart` succeeded, so a crash pages twice: `OnFailure=renovate-notify-alert.service` hits
Discord immediately, and the missed beat reds the monitor at its 36h window.

That monitor watches the notifier, not Renovate. A green "Alive" monitor says this unit ran; it
says nothing about whether Renovate is still producing updates, which is the staleness arm's
job. The marker also greens regardless of Discord *delivery*, which is why monitor-bridge
verifies the GitOps/Renovate webhook separately (`checks/notify.py`).
