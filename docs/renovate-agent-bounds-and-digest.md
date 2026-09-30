# `renovate_agent` — what bounds a run, and what its digest and tile measure

Working-out moved off `ansible/roles/setup/renovate_agent/CLAUDE.md` (#2995), which a session
reads on every touch of the unattended agent. The role doc keeps the arming procedure and the
autonomous-role contract; this page keeps the four bounds and why each is the var it is, the
worktree rules, what the Discord digest measures, and the alive tile's exit-code and token
plumbing. `docs/renovate-notify-internals.md` is the sibling page for the reporting half.

## Exercising the wrapper without arming anything

`RENOVATE_AGENT_CONFIG` overrides the config path, so the I/O shell can run end-to-end against a
throwaway config before the timer exists. Point `REPO` at the real repo with the backlog empty and
the gate returns before it spends a session, which makes it a free pass through config parsing,
the `gh` census and the return path:

```bash
RENOVATE_AGENT_CONFIG=/tmp/agent-test.env \
  uv run python ansible/roles/setup/renovate_agent/files/renovate_agent.py
```

The override exists for exactly this. Without it the first armed tick would be the first time this
code ever ran.

## What bounds the run

The unit is deliberately **not** sandboxed, unlike the sibling `renovate-notify.service`. This
session drives git, gh, uv, ansible and kubectl and needs `~/.claude` for its own credentials, so
`ProtectHome`/`ProtectSystem` would have to be widened until they meant nothing. Four other things
bound it instead, and each is a var in `defaults/main.yml`:

| Bound | Var | Why that one |
|---|---|---|
| PRs per tick | `renovate_agent_max_prs` (3) | Each landing is a CI wait plus a tick plus a deploy plus a health gate. Three fits the wall clock; the rest wait for tomorrow. |
| Wall clock | `renovate_agent_run_timeout_s` (5400) | The wrapper's own kill, which still posts a digest naming the timeout. |
| Wall clock, backstop | `renovate_agent_unit_timeout` (100min) | systemd's. It kills the whole cgroup and posts only the `OnFailure` alert, so it must never trip first. |
| Spend | `renovate_agent_budget_usd` (25) | A runaway backstop, not a planned stop — see below. |

**The budget must not be the binding constraint.** Stopping a session mid-landing leaves a
merged-but-undeployed change, which is exactly what the root `CLAUDE.md`'s post-merge section
forbids. The PR cap is what bounds a normal run; the budget only catches a loop.

**The session never runs in the primary checkout.** One untracked file in `/home/<user>/server`
parks the GitOps deployer silently, and a session that edits, renders and tests is guaranteed to
leave some. Each tick recreates `.claude/worktrees/renovate-auto` at `origin/master` and runs
there. `land.sh` changes to the primary checkout itself, so the landing steps still deploy the
right tree.

**A worktree holding work is not thrown away.** If the previous tick left uncommitted changes or
commits whose content is not on `origin/master`, the tick skips, posts the path and exits
`EXIT_WORKTREE_BLOCKED` (2). Removing the tree is how unlanded work is lost.

Content, not ancestry. The run branch is fixed (`worktree-renovate-auto`) and nothing resets it
after a landing, and a squash merge keeps a branch's content while discarding the commits that
carried it — so `rev-list origin/master..<branch>` counts a landed branch's commits forever. That
refused the tree every day from 2026-09-14 while its two commits sat on master as PR #1812
(#2014). `branch_content_is_on_master` settles it the way `scripts/dev/prune_worktrees.py` does:
`git merge-tree --write-tree origin/master <branch>` producing master's own tree means the branch
has nothing master lacks. A revert-only branch is still refused (merging it changes master's
tree), and no verdict — a conflict with master's drift, empty output — reads as not contained by
`merge-tree`. That conflict case is the pruner's fourth layer and is ported too
(`branch_tip_was_merged`): `gh pr list --state merged --head <branch> --json headRefOid` must name
the branch's exact tip. Ported rather than left to the page because the very tree #2014 found was
already past `merge-tree` — master had drifted into a conflict on `n8n/base-pin-history.tsv` — so
without it the fix would have paged daily and still needed the operator's `reset --hard`. The
match is on the head SHA, never the branch name: the name is reused every tick.

## The digest measures effect, not completion

`is_error: false` plus `terminal_reason: completed` means the process ended cleanly, not that any
PR moved — a session that achieved nothing still writes a confident closing paragraph. So the
wrapper censuses the open PRs authored by `app/renovate` **before and after**, and the digest's
headline is that delta:

- `resolved #a, #b` — those PR numbers left the open set and GitHub reports them `MERGED`.
- `ran and merged no Renovate PR` — PRs left the open set, but none merged. Each one is listed as
  `closed without merging` or `state unreadable`.
- `ran and no Renovate PR changed state` — the failure that would otherwise read green.
- `FAILED — <reason>` — timeout, non-zero exit, or `is_error`.

Leaving the open set is not landing. The `renovate-prs` skill finishes a `manual —` bump by
closing the Renovate PR in favour of a superseding PR, and that PR stays open for a person
(#2746). So the wrapper runs `gh pr view <n> --json state` for every PR that left the open set, and
only a `MERGED` one counts as resolved (#2755). A failed lookup lands in `state unreadable`, never
in `resolved`.

The superseding PR is authored by the session's account, not `app/renovate`, so a second census
finds it: the open PRs authored by the login `gh api user` names, before and after, kept to the
PRs whose head branch is the run branch or `<run branch>-<n>` (#2769). The prompt pins that name.
The branch filter is required because interactive sessions open PRs as the same account on
`worktree-renovate-<slug>` branches all day. A PR new to the after-census appears as `handed off,
open for a person to land: #n`, and a failed census as `hand-off census unreadable`, never as an
empty list.

**Both censuses filter the author locally, never with `gh pr list --author`** (#2772). With
`--author`, gh runs a GraphQL `search(` query, and GitHub's search index is eventually consistent.
The after-census runs seconds after the session exits, so the index can omit a superseding PR
opened near the end, or still list a Renovate PR just merged as open. Without the flag, gh reads
`repository.pullRequests`, which is current. `_open_pr_listing` in `files/renovate_agent.py` is the
one listing both censuses read, and `test_agent_logic.py::TestOpenPrs` refuses the flag.

`permission denials:` on a digest line is the one to act on. Headless auto mode approving the
session's writes is the assumption the whole design rests on; it was measured against Claude Code
2.1.258 (a bash file create, a `sed -i`, and an `Edit` tool call all landed with
`permission_denials: []`), but a Claude Code upgrade can change it, and the failure mode is a
session that reads green and does nothing.

## The run record

Every tick appends one JSON line to `runs.jsonl` in `STATE_DIR` (`/var/lib/renovate-agent`), so
whether the agent is worth its cost is a count over one file rather than a read of 30 digests
(#2864). `run_record` in `files/agent_logic.py` builds the line, and each exit path writes one:

- `skipped` — the gate spent no session. `reason` says why, including the quiet empty-backlog
  skip that posts nothing to Discord.
- `blocked` — the run worktree still holds unlanded work.
- `ran` or `failed` — a session ran. The line carries `merged`, `closed`, `handed_off`,
  `unread`, `left_open` and `opened` from the measured delta, plus `cost_usd`, `turns` and the
  denial count. `handed_off` is `null` when its census failed.
- `crashed` — the wrapper threw. `report_crash` writes this line before its Kuma push.

The PR fields come from the census, not from the session's summary, so a triage that only
comments on a PR leaves no trace in them. `left_open` is the PRs the session skipped, whether
by the `k8s_autodeploy: false` denylist or by the PR cap. A 30-day count:

```bash
jq -s --argjson since "$(date -d '-30 days' +%s)" '[.[] | select(.ts >= $since)]
  | {ticks: length, sessions: map(select(.result == "ran" or .result == "failed")) | length,
     merged: map(.merged // [] | length) | add, closed: map(.closed // [] | length) | add,
     handed_off: map(.handed_off // [] | length) | add, cost_usd: map(.cost_usd // 0) | add}' \
  /var/lib/renovate-agent/runs.jsonl
```

The file gains one line a day and is never rotated.

## The alive monitor

`renovate_agent_kuma_push_token` (SOPS, tier auto) is the one token behind two halves: the unit's
`ExecStartPost` beat, and the `Renovate Agent — Alive` push tile in
`roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2`. The beat fires only when the wrapper
exited 0, so the tile reports silence and the `OnFailure` alert reports failure.

**A worktree-blocked skip exits non-zero, so it does not beat.** The beat is `ExecStartPost`,
which runs after ANY exit 0 — a `down` pushed from inside the wrapper on a `return 0` path is
overwritten by the `up` that follows it. So that skip returns `EXIT_WORKTREE_BLOCKED` instead: no
beat, `OnFailure` pages, and the tile expires by deadman if nobody clears the tree. Until
2026-09-18 that path returned 0, and the tile stayed green through five daily skips (#2014). The
two other skips still exit 0: the quiet no-open-PRs skip is the healthy steady state, and the
GitOps-hold skip is alarmed by the deployer's own `GitOps Status` tile, which this one need not
duplicate. `test_agent_logic.py::TestSkipExitCodes` pins the blocked and the quiet case.

**A crash also pushes its own `down`, carrying the exception text** (`report_crash`, called from
the `__main__` guard). Silence plus an `OnFailure` page was not enough: the tile went down 28 hours
later by deadman expiry with no reason attached, which is what a host that is simply off looks
like. For two days from 2026-09-08 that hid a one-line `prepare_worktree` failure — a directory at
the run worktree's path that `git worktree list` had no record of (#1477). The deadman stays as the
backstop for a host that is genuinely off; it must not be the only signal for a run that started
and threw. The report is best effort and never re-raises, so a failed report cannot replace the
traceback that says what broke.

**An unregistered directory at the run worktree's path is reclaimed, not a crash.** That is what a
killed session leaves behind. It matters twice over, because **git searches UPWARD for a
repository**: `git -C <orphan dir> status` resolves to the PRIMARY CHECKOUT and answers about that
tree, so `worktree_is_reusable` was reading the wrong repo — a dirty primary would have read as
this run tree holding uncommitted changes. `is_registered_worktree` decides which case it is, and it
fails CLOSED — an unreadable `worktree list` reads as a directory git owns — so nothing removes a
tree that might be registered. Every case that holds real work is still refused by `worktree_is_reusable` before
this runs.

**The push URL lives in `/etc/renovate-agent/config.env` (0600), not in the unit.** A unit line is
public: `systemctl show <unit> -p ExecStartPost` serves it over the system bus to any local user,
and `/proc` here has no `hidepid`. The unit inlined the whole URL until 2026-09-09, which put a
rotation-tracked token in clear text behind a command the harness guard recommended as the SAFE
alternative to `systemctl cat` (issue #1489). `ExecStartPost` now sources `config.env` and passes
`$KUMA_PUSH_URL` to `curl` on stdin (`-K -`), the form `renovate-notify.service.j2` already used.
ENFORCED: `ansible/tests/setup/test_renovate_agent_unit.py`.

The tile's deadline is 28h, not the 25h the other daily tiles use, because the beat lands at the
end of the run: a fast run followed by one that draws the full jitter and runs to the 100-minute
unit timeout spaces two beats 25h50m apart. `test_renovate_agent_unit.py` pins the deadline
between that gap and two periods.

Rotating the token moves both halves, on two deploy paths: `deploy.sh --tags uptime-kuma` for the
tile and `initial_setup.yml --tags renovate_agent` for the unit. That is why it is in
`CROSS_HOST_PUSH_TOKENS` in `secret_rotation.py` and the unattended rotation skips it.

## The denylist marker's own history

The contract's rule is that the session never touches a PR whose title or branch carries
`k8s_autodeploy: false`. How the marker comes to be there took several rounds:

- The denylist rule in `renovate.json` leads its parenthetical with the marker. A per-package rule
  whose pin a denied role owns ends its own with it (the CrowdSec bouncer plugin in Traefik,
  Meilisearch and the time-tagger dependencies in Karakeep, n8n's Dockerfile pins, #1963), because
  those rules override the denylist rule's `groupName`.
- **Read the branch as well as the title** (#2641). Renovate titles a single-dependency group
  `Update <dep> …` and drops the group name, so the marker can survive in the branch alone,
  slugified as `k8s_autodeploy-false` — #2620 was `Update klutchell/unbound Docker tag to v1.26.1`
  against Pi-hole's defaults. The prompt therefore reads `headRefName` beside the title, and
  `test_the_prompt_leaves_a_denylisted_pr_to_a_person` pins both tells.
- Every rule carrying the marker sets `groupSingleUpdates: true`, which applies the group's
  `commitMessageTopic` to a one-dependency branch too and so puts the marker in their titles: the
  denylist and base-image rules since #2646, the per-package manual rules since #2654. A PR raised
  before that flag landed still arrives bare-titled.
- A third rule carries it for a denied role's base image — the `FROM` in
  `templates/Dockerfile*.j2`, which Renovate's built-in dockerfile manager finds and the denylist
  rule's `custom.regex` scope never reaches (#2117: nut's Debian digest bump #2115 arrived with a
  bare title). Its file list is the denylist restricted to the roles that carry a Dockerfile, and
  the same guard derives it.
