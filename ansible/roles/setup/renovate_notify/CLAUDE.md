# `setup/renovate_notify` — the Renovate reporting half

A daily systemd timer that queries the GitHub REST API for open Renovate PRs and the
Dependency Dashboard issue, then posts a Discord digest **only when what needs a human
changes**. It is the reporting half of the pair whose acting half is `setup/renovate_agent`:
this role says what is open and what needs manual work, that one does it.

Runs on exactly one host, `renovate_notify_host` (`inventory/group_vars/all.yml`, daniel-box):
the query is fleet-wide, so a second host duplicates every notification.

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
`renovate_agent` has no such handler — its run costs money and changes the fleet.

The working-out behind the rules below is `docs/renovate-notify-internals.md`: the sandbox's
two overrides, the dashboard parsers, the module layout and what a dry run does not prove.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "renovate_notify"` when `inventory_hostname ==
  renovate_notify_host`
- **Timer:** `renovate-notify.timer` (`OnCalendar=*-*-* 13:00:00`)
<!-- /generated_from -->

## What it watches, and why each arm exists

Three signals, read from the same repo, each blind to what the others see:

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
outlives its `minimumReleaseAge` plus `PENDING_GRACE_DAYS` (issue #886). It measures dwell, so
it needs state: `/var/lib/renovate-notify/pending_seen.json` holds two first-seen epochs per
item, written every run. That state is the arm's single point of failure, failing silently two
ways: **a renamed marker empties the parse**, and **a lost `pending_seen.json` restarts every
clock at zero** and reads exactly like the intended bootstrap. `pending_state_lost` tells the
two apart using `last_run`.

**Each item carries two clocks**, because a mutable tag's branch outlives any one digest, and
**a grouped row takes the digest soak**, because it carries no update type of its own.

`docs/renovate-notify-internals.md` has both failure shapes in full — the runtime checks that
catch a renamed marker, the two-clock mechanism, the digest-soak exceptions — and every
measurement behind this section: the dwell that over-reported 18.8 days, the grouped row four
days late, and why a reset posts twice.

## Notification is fingerprint-gated, not state-gated

`fingerprint()` is the whole dedupe: the digest posts when the fingerprint changes and stays
silent when it does not, so **a standing problem goes quiet after one page** and a new problem
beside it re-pages. Two qualifications: `stuck` PRs carry an age dimension, so one broken for
weeks re-pages at 1/3/7/14 days, and the fingerprint persists only on confirmed delivery, so a
failed Discord post retries next run.

## Liveness, and the sandbox

`ExecStartPost` beats a Kuma push monitor ("Renovate Notifier — Alive", token
`monitor_bridge_renovate_alive_push_token`) and runs only when `ExecStart` succeeded, so a
crash pages twice — `OnFailure=renovate-notify-alert.service` and the missed 36h beat.
**That monitor watches the notifier, not Renovate**, and greens whatever Discord did.

The unit is sandboxed (`ProtectSystem=strict` + `ProtectHome=read-only`), and two overrides
are load-bearing: `UV_CACHE_DIR=/tmp/uv-cache`, without which the unit dies with exit 2 before
Python starts on every tick, and the Kuma push URL reaching curl on stdin (`-K -`) rather than
in argv, where any local user could read the token. `docs/renovate-notify-internals.md` has
both, and the gaps the alive monitor leaves.

## Working on it

Two pure modules, `files/notify_logic.py` and `files/pending_logic.py`, with
`files/renovate_notify.py` as the fetch/persist/post shell. **A new module here needs BOTH
ship-list sites in `tasks/code.yml`**: the copy loop and the `stamp_deployed_pairs` drift
list. `host_lib.py` is copied in from `roles/setup/common/files/` — edit it there, not here.

```bash
uv run pytest ansible/roles/setup/renovate_notify -q -n0
uv run python ansible/roles/setup/renovate_notify/files/renovate_notify.py --dry-run
```

**A dry run proves only the paths today's dashboard exercises,** and `--check` fails at
"Enable and start the timer", which is not a bug. Both, plus the captured-fixture rule a new
parsing arm meets, are in `docs/renovate-notify-internals.md`.

## Verifying a deploy took effect

The role installs to `/opt/renovate-notify/`, its state to `/var/lib/renovate-notify/`.
`last_run` is written **only on clean completion**, so its mtime is the honest signal the
deployed code ran end-to-end:

```bash
systemctl show renovate-notify.service -p ExecMainStatus -p ExecMainStartTimestamp
ls -l /var/lib/renovate-notify/          # last_run mtime; last_notified holds the fingerprint
```

An empty `last_notified` means nothing needs a human — not that the notifier is broken.
