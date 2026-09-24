# watchers

Generic external-watcher scaffold: fetch something outside this repo, check whether it
changed state, notify Discord on a transition, and ping a healthchecks.io-style monitor.
The shared loop lives in `scripts/lib/watcher.py`; this directory holds the watchers built
on it.

| Watcher | Watches |
| --- | --- |
| `cert_expiry.py` | TLS leaf-cert expiry on every publicly-routed hostname |

A watcher here notifies only when its state *changes*: a cert crossing the expiry threshold
is one event, not one alert per day it stays crossed.

## Adding a watcher

1. Write a `fetch() -> T` that returns this run's state — the shape is yours; JSON-
   serializable, since it round-trips through `lib.watcher.save_state`/`load_state`.
2. Write a `check(previous: T | None, current: T) -> str | None` that returns a message
   only when something worth alerting on changed, `None` otherwise. Keep this pure —
   inject `now` (or whatever clock/threshold input you need) rather than reading it inside
   `check`, so the transition rule is testable with plain dicts and no network.
3. Wire them into a `lib.watcher.Watcher` and call `lib.watcher.run_watcher(watcher)` from
   `main()`. Give the script a `--dry-run` flag that fetches and prints without notifying
   or touching state — see `cert_expiry.py`'s `main()`.
4. Bootstrap `sys.path` the way every cross-directory script in this repo does (see the
   repo-root `CLAUDE.md`, "Directory Structure") — a directly-invoked script gets only its
   own directory on `sys.path`.
5. State persists under `~/.local/state/homelab-watchers/<name>.json` by default; pass an
   explicit `state_path` to `Watcher` if a watcher needs a different location.
6. Tests go in `scripts/watchers/tests/`, importing as `from watchers.<name> import ...`
   (matching `diagnostics.probe_lib` / `validate.*` — `scripts/watchers` is deliberately
   absent from `pyproject.toml`'s pytest `pythonpath`, reached through `scripts` alone).
   Ship the red-proof pair for `check`'s transition rule (unchanged state notifies nothing,
   a real change notifies) and for any threshold `check` depends on.
7. Schedule it in `ansible/roles/setup/initial_setup/tasks/crons.yml`, tagged `[crons]`,
   alongside the other `daniel-box` crons. Reuse an existing SOPS secret for the Discord
   webhook where one already fits (`monitor_discord_webhook_url` is the general ops-alert
   channel) rather than adding a new one.
