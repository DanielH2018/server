# `hosts/components/board-sync`

## Autonomous-role contract (the timer writes the Project board with no human in the loop)

- **Scope / exclusions:** one GitHub object, the Server Issue Tracking board. It adds an open issue
  the board lacks and writes its Status field. It changes no issue, issue field, label or
  repository setting, and writes nothing under the checkout.
- **Mode (explicit and reversible):** the timer's enable link in this package arms it. Dropping the
  link from a package version, or reverting this component, stops the timer on the install that
  drops it, and `systemctl stop homelab-board-sync.timer` stops it until the next install.
- **Authoritative sources:** each issue's state and `Claim:` comments, and its open closing
  pull requests, as GitHub's GraphQL API returns them. The board is only ever written, never read as truth.
- **Abort valves:** a run that finds nothing to change sends one GraphQL read per 25 open issues and
  no mutation. `MUTATION_PAUSE_S` in board.py paces a catch-up under GitHub's secondary write
  limit, and the updater's hold holds a package that would change the unit.
- **Required evidence, every run:** the count line `<n> issues: <x> updated, <y> unchanged, <z>
  failed` in the journal under `board-sync`, then one line per item it changed or could not
  change. A failed run adds `status=degraded <n> consecutive failed runs`. The third failed run
  in a row exits 1, and OnFailure= pages Discord through homelab-alert, then every 24th after
  it: a failure that lasts pages once a day, and one the next run clears pages never.
- **Next-run review:** a page names homelab-board-sync.service. Read `journalctl -t board-sync`.
  A `board has no "Status" field` or `has no option` line means the board's schema moved, and
  recurs every hour until the board or board.py changes. Any other reason is gh or GitHub.

ENFORCED: hosts/lib/tests/test_board_sync_component.py, which renders the package and holds the
unit, the timer, its enable link and this contract, and hosts/tests/test_board_sync_paging.py,
which runs the rendered script through a failure streak.
