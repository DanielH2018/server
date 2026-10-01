# `setup/renovate_notify` — the Renovate reporting half

A daily systemd timer that queries the GitHub REST API for open Renovate PRs and the
Dependency Dashboard issue, then posts a Discord digest **only when what needs a human
changes**. It is the reporting half of the pair whose acting half is `setup/renovate_agent`:
this role says what is open and what needs manual work, that one does it.

Runs on exactly one host, `renovate_notify_host` (`inventory/group_vars/all.yml`, daniel-box):
the query is fleet-wide, so a second host would duplicate every notification.

Invoked from `initial_setup.yml`, **not** `deploy.yml` — the role is not in
`containers_list`, so `./scripts/deploy.sh --tags renovate_notify` exits 2 on an unmatched
tag. Deploy it with:

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags renovate_notify
```

The GitOps deployer runs that command itself — `ansible/roles/setup/` is in
`_BROAD_SETUP_PREFIXES` (`roles/setup/gitops_deploy/files/deploy_changes.py`) — so a merged
change here applies on the next tick without a hand deploy.

**Every task notifies `Run renovate-notify once`,** so a deploy of this role is never silent:
it kicks a real GitHub query and, if the fingerprint moved, a real Discord post. The sibling
`renovate_agent` has no run-once handler because its run costs money and changes the fleet.

The working-out behind the rules below is `docs/renovate-notify-internals.md`: the sandbox's
two overrides, the dashboard parsers, the module layout and what a dry run does not prove.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "renovate_notify"` when `inventory_hostname ==
  renovate_notify_host`
- **Timer:** `renovate-notify.timer` (`OnCalendar=*-*-* 13:00:00`)
<!-- /generated_from -->

## What it watches, and why each arm exists

Three signals, all read from the same repo, each blind to what the others see:

| Arm | Reads | The failure it exists to catch |
|---|---|---|
| PR digest | open PRs (`actionable`) | a PR that will never merge on its own — automerge off, CI failing, conflicting |
| Dashboard staleness | the dashboard issue's `updated_at` | Renovate itself stopped. With no PRs the digest reads as a cleared backlog, so this is the only fail-loud arm |
| Dashboard problems | the dashboard issue's **body** | a dependency that silently stops receiving updates while everything reads green |

**The third arm parses two independently rendered blocks**, and missing either is invisible:
`find_dashboard_problems` unions a parser for each. `docs/renovate-notify-internals.md` has
both blocks and why a lookup failure leaves the other two arms quiet.

## The stuck-pending clock, and the two ways it goes inert

A fourth arm reads the dashboard's `## Pending Status Checks` section and pages when an item
outlives its `minimumReleaseAge` plus `PENDING_GRACE_DAYS` (issue #886 — promtail sat 111 days
against a 7-day soak). It measures dwell, so it needs state:
`/var/lib/renovate-notify/pending_seen.json` holds each pending branch's first-seen epoch, and
`write_pending_seen` writes it on every run regardless of what was posted. That state is the
arm's single point of failure, and it fails silently in two shapes:

- **A renamed marker empties the parse** (#1472). `pending_section_unreadable` and
  `dashboard_headers_unrecognized` are the runtime non-vacuity checks for that.
- **A lost `pending_seen.json` restarts every clock at zero.** `stale_pending` treats an item
  with no entry as first seen now, so a wiped state file looks like a bootstrap;
  `pending_state_lost` tells the two apart using `last_run`, and the digest names the date the
  clocks become usable again (#1526).

**Two packages soak less than the digest default.** `renovate.json` soaks the nginx alpine
digests 1 day rather than 3, because upstream re-pushes those tags about every 3.6 days (#2886),
and `files/pending_logic.py:FAST_DIGEST_SOAK_DAYS` is this arm's copy of that exception —
`tests/test_pending_soak.py::test_soak_constants_match_renovate_json` fails when the two
disagree.

**A grouped row carries no update type, so it takes the digest soak.**
`ansible/roles/setup/renovate_notify/files/pending_logic.py:GROUPED_TITLE_MARKER` is the
parenthetical `item_soak_days` matches; the cost, weighed in the `DECIDED:` comment there, is a
grouped VERSION bump paging four days early.

Both failure shapes with their measurements, the grouped row that alerted four days late
(#2885) and why a reset posts twice are in `docs/renovate-notify-internals.md`.

## Notification is fingerprint-gated, not state-gated

`fingerprint()` is the whole dedupe: the digest posts when the fingerprint changes and stays
silent when it does not, so **a standing problem goes quiet after one page** and a new problem
beside it re-pages. Two deliberate qualifications: `stuck` PRs carry a coarse age dimension so
one broken for weeks re-pages at 1/3/7/14 days, and the fingerprint persists only on confirmed
delivery, so a failed Discord post retries on the next run rather than being lost.

## Liveness, and the sandbox

`ExecStartPost` beats a Kuma push monitor ("Renovate Notifier — Alive", token
`monitor_bridge_renovate_alive_push_token`) and runs only when `ExecStart` succeeded, so a
crash pages twice — `OnFailure=renovate-notify-alert.service` and the missed 36h beat.
**That monitor watches the notifier, not Renovate**, and it greens regardless of Discord
delivery.

The unit is sandboxed (`ProtectSystem=strict` + `ProtectHome=read-only`), and two overrides
are load-bearing: `UV_CACHE_DIR=/tmp/uv-cache`, without which the unit dies with exit 2 before
Python starts on every tick, and the Kuma push URL reaching curl on stdin (`-K -`) rather than
in argv, where any local user could read the token. `docs/renovate-notify-internals.md` has
both, and what covers the gaps the alive monitor leaves.

## Working on it

Two pure modules, `files/notify_logic.py` and `files/pending_logic.py`, with
`files/renovate_notify.py` as the fetch/persist/post shell. **A new module here needs BOTH
ship-list sites in `tasks/main.yml`**: the copy loop and the `stamp_deployed_pairs` drift
list. `host_lib.py` is copied in from `roles/setup/common/files/` — edit it there, not here.

```bash
uv run pytest ansible/roles/setup/renovate_notify -q -n0
uv run python ansible/roles/setup/renovate_notify/files/renovate_notify.py --dry-run
```

**A dry run proves only the paths today's dashboard exercises,** and `--check` fails at
"Enable and start the timer" without that being a bug in the role. Both, plus the
captured-fixture rule a new parsing arm meets, are in `docs/renovate-notify-internals.md`.

## Verifying a deploy took effect

The role installs to `/opt/renovate-notify/`, its state to `/var/lib/renovate-notify/`.
`last_run` is written **only on clean completion**, so its mtime is the honest signal that the
deployed code ran end-to-end:

```bash
systemctl show renovate-notify.service -p ExecMainStatus -p ExecMainStartTimestamp
ls -l /var/lib/renovate-notify/          # last_run mtime; last_notified holds the fingerprint
```

An empty `last_notified` means nothing needs a human — not that the notifier is broken.
