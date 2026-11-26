# `hosts/components/renovate-notify` — the Renovate reporting half

A daily systemd timer that queries the GitHub REST API for open Renovate PRs and the
Dependency Dashboard issue, then posts a Discord digest **only when what needs a human
changes**. It is the reporting half of the pair whose acting half is
`hosts/components/renovate-agent/`: this component says what is open and what needs manual
work, that one does it.

`homelab-daniel-box` ships it, and only that package lists it: the query is fleet-wide, so a second
host duplicates every notification. A merged change reaches the host when the builder publishes the
next package version and the updater installs it.

The last section holds the contract. The working-out behind the rules below is
`docs/renovate-notify-internals.md`: the sandbox's two overrides, the dashboard parsers, the
module layout and what a dry run does not prove.

## What it watches, and why each arm exists

Four signals, read from the same repo, each blind to what the others see:

| Arm | Reads | The failure it exists to catch |
|---|---|---|
| PR digest | open PRs (`actionable`) | a PR that will never merge on its own — automerge off, CI failing, conflicting |
| Dashboard staleness | the dashboard issue's `updated_at` | Renovate itself stopped. With no PRs the digest reads as a cleared backlog, so this is the only fail-loud arm |
| Dashboard problems | the dashboard issue's **body** | a dependency that silently stops receiving updates while everything reads green |
| Autoclosed | recently closed PRs | a PR Renovate closed unmerged while the dashboard still lists its update (#4630) |

**The third arm parses two independently rendered blocks**, and missing either is invisible:
`find_dashboard_problems` unions a parser for each. `docs/renovate-notify-internals.md` has
both blocks and why a lookup failure leaves the other two arms quiet.

## The stuck-pending clock, and the two ways it goes inert

A fifth arm reads the dashboard's `## Pending Status Checks` section and pages when an item
outlives its `minimumReleaseAge` plus `PENDING_GRACE_DAYS` (issue #886). It measures dwell, so
it needs state: `/var/lib/renovate-notify/pending_seen.json` holds two first-seen epochs per
item, written on every run that finds the dashboard. A run that misses it keeps the file,
because a rewrite from zero parsed items would restart every clock (#9437). That state is the arm's single point of failure, failing silently two
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

`fingerprint()` (`hosts/components/renovate-notify/files/notify_logic.py:fingerprint`) is the
whole dedupe: the digest posts when the fingerprint changes and stays
silent when it does not
(`hosts/components/renovate-notify/tests/test_notify_logic.py::test_should_notify_unchanged_is_silent`),
so **a standing problem goes quiet after one page** and a new problem
beside it re-pages
(`hosts/components/renovate-notify/tests/test_notify_logic.py::test_should_notify_changed_backlog_is_digest`).
Two qualifications: `stuck` PRs carry an age dimension, so one broken for
weeks re-pages at 1/3/7/14 days
(`hosts/components/renovate-notify/tests/test_notify_logic.py::test_should_notify_repages_when_stuck_pr_crosses_age_threshold`),
and the fingerprint persists only on confirmed delivery, so a
failed Discord post retries next run (`hosts/components/renovate-notify/files/renovate_notify.py:main`).

## Liveness, and the sandbox

`ExecStartPost` writes the `renovate-notify` textfile to node_exporter's textfile directory (M6 batch
B4) and runs only when `ExecStart` succeeded, so a crash pages twice:
`OnFailure=homelab-alert@%n.service`, and `RenovateNotifyNotReporting` once the file is
older than its 36h deadline. **That verdict watches the notifier, not Renovate**, and greens
whatever Discord did. The `+` prefix runs that one line outside the sandbox, because
`ProtectSystem=strict` leaves the directory read-only.

The unit is sandboxed (`ProtectSystem=strict` + `ProtectHome=read-only`), and one override is
load-bearing: `UV_CACHE_DIR=/tmp/uv-cache`, without which the unit dies with exit 2 before
Python starts on every tick. `docs/renovate-notify-internals.md` has it, and the gaps the alive
verdict leaves. `ReadWritePaths=-/var/lib/gh-quota` is the one writable path besides the state
directory: without it `github_rest.github_get` fails open and the notifier's reads count as
spend beyond the gh-quota ledger (`docs/github-quota.md`).

## Working on it

Two pure modules, `files/notify_logic.py` and `files/pending_logic.py`, with
`files/renovate_notify.py` as the fetch/persist/post shell. **A new module here needs an entry
in `component.yml`'s `files:`**, or it never reaches the host. `host_lib.py`, `renovate_identity.py`,
`ci_verdict.py` and `builder_ci_grade.py` are `lib:` entries from `hosts/lib/` — edit them
there, not here.

```bash
uv run pytest hosts/components/renovate-notify -q -n0
PYTHONPATH=hosts/lib uv run python hosts/components/renovate-notify/files/renovate_notify.py --dry-run
```

The dry run needs `PYTHONPATH=hosts/lib` because the repo holds no `host_lib.py` beside the
script; the package installs one into `/usr/lib/homelab/renovate-notify/`. The script also
reads the hard-coded `/etc/renovate-notify/config.env`, which exists only on
daniel-box and is mode `0600` for `sys_user`. Run the dry run there as
that user; anywhere else it stops at `FileNotFoundError` once the imports resolve.

**A dry run proves only the paths today's dashboard exercises.** That, plus the
captured-fixture rule a new parsing arm meets, is in `docs/renovate-notify-internals.md`.

## Verifying a deploy took effect

The package installs to `/usr/lib/homelab/renovate-notify/`, its state to `/var/lib/renovate-notify/`.
`last_run` is written **only on clean completion**
(`hosts/components/renovate-notify/files/renovate_notify.py:main`), so its mtime is the
honest signal the deployed code ran end-to-end:

```bash
systemctl show homelab-renovate-notify.service -p ExecMainStatus -p ExecMainStartTimestamp
ls -l /var/lib/renovate-notify/          # last_run mtime; last_notified holds the fingerprint
```

An empty `last_notified` means nothing needs a human — not that the notifier is broken.

## Autonomous-role contract (the timer posts a digest to Discord with no human in the loop)

- **Scope / exclusions:** reads the repo's open Renovate PRs and the dependency dashboard, and posts a
  Discord digest when what needs a human changes. It writes /var/lib/renovate-notify, its gh-quota ledger under /var/lib/gh-quota and the textfile below.
- **Mode (explicit and reversible):** the timer's enable link in this package arms it. Dropping the
  link from a package version, or reverting this component, stops the timer on the install that
  drops it.
- **Authoritative sources:** GitHub's REST API, as github_rest.github_get reads it.
- **Abort valves:** the fingerprint gate, which keeps a standing problem to one page, and the updater's
  hold (docs/host-packages.md).
- **Required evidence, every run:** renovate-notify.prom in node_exporter's textfile directory, which
  RenovateNotifyNotReporting reads, and a page from homelab-alert@ on a crash.
- **Next-run review:** `journalctl -u homelab-renovate-notify` before you change a threshold.

ENFORCED: this component's tests/ and hosts/lib/tests/host_baselines/test_h737_box_components.py.
