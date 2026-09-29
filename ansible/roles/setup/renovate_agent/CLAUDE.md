# `setup/renovate_agent` — the unattended Renovate agent

A daily systemd timer that spends one headless Claude Code session
(`claude -p "/renovate-prs"`) on the repo's open Renovate PRs, then posts a Discord digest.
It is the acting half of the pair whose reporting half is `setup/renovate_notify`: that role
says what is open and what needs manual work, this one does it.

Runs on `renovate_agent_host` (`inventory/group_vars/all.yml`, daniel-box). Invoked from
`initial_setup.yml`, **not** `deploy.yml` — the role is not in `containers_list`, so
`./scripts/deploy.sh --tags renovate_agent` exits 2 on an unmatched tag. Deploy it with:

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags renovate_agent
```

The four bounds on a run and the worktree rules, what the Discord digest measures, the alive
tile's exit-code and token plumbing, how to exercise the wrapper without arming anything, and the
denylist marker's own history are in `docs/renovate-agent-bounds-and-digest.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "renovate_agent"` when `inventory_hostname ==
  renovate_agent_host`
- **Timer:** `renovate-agent.timer` (`OnCalendar=*-*-* 06:00:00 America/Chicago`)
<!-- /generated_from -->

## Arming it

The role installs the script, config, prompt and units on every run.
`renovate_agent_enabled` alone decides whether the timer is enabled and started, and setting
it back to `false` stops **and** disables the timer. That is the rollback, and
`ansible/tests/setup/test_renovate_agent_unit.py` pins that both directions stay wired.

It ships `false`. Arming it is a decision with a spend attached and a blast radius — the session
merges PRs and lands them through `land.sh`, which deploys — not a sign the role is unfinished.

**The merge itself goes through `land.sh --arm-merge`, not a bare `gh pr merge`.** A bare
`gh pr merge` sits on the ask list (`Bash(gh pr merge:*)` in `~/.claude/settings.json`), and
auto mode suspends the allow list — an unattended session has nobody to answer that prompt,
so it times out as a denial (three attempts, three denials, on 2026-09-03, issue #979).
`--arm-merge` runs the same `gh pr merge --squash --auto` call inside `land.sh` instead, where the
session's own invocation text is just the one script call the worktree-containment check already
accepts. The `renovate-prs` skill's landing step names the flag.

**`--check` fails at "Enable and start the timer", and that is not a bug in the role.** Check
mode writes no unit file, so systemd reports `Could not find the requested service` for a
`renovate-agent.timer` that does not exist. Every task before it reports correctly, which is what
a check run of this role is good for. The sibling `renovate_notify` role behaves the same way.

There is deliberately **no run-once handler**, where `renovate_notify` has one. Its run is a
read-only API query; this one costs money and changes the fleet, so a config edit must not
kick a session as a side effect. Start one by hand:

```bash
sudo systemctl start renovate-agent && journalctl -u renovate-agent -f
```

## Autonomous-role contract (it merges and deploys with no human in the loop)

The acting half of the Renovate pair. Its authority is written down so an edit to the prompt,
the caps or the schedule cannot quietly widen it.

- **Scope / exclusions:** the repo's open PRs authored by `app/renovate`, worked through the
  `renovate-prs` skill: finish the manual half of a grouped bump, merge through
  `land.sh --arm-merge`, land and verify. **Never** a PR by another author (ENFORCED: the
  unit sets `LAND_REQUIRE_AUTHOR=app/renovate`, and `land.sh --arm-merge` refuses any other
  author before its first merge call — `scripts/deploy_tools/land_lib/merge.py:_require_author`;
  `ansible/tests/setup/test_renovate_agent_unit.py::test_the_unit_pins_land_sh_to_renovates_prs`
  pins the value to the wrapper's census). That includes the superseding PR the session opens
  itself for a finished `manual —` bump: the operator chose to hand it off rather than let the
  timer land it (#2746). The session leaves it open, never passes `--any-author`, and files a
  hand-off finding carrying its `land.sh` command, as the `renovate-prs` skill's §4 says;
  `ansible/tests/setup/test_renovate_agent_unit.py::test_the_prompt_hands_off_its_own_superseding_pr`
  pins the prompt to it. **Never** a bare `gh pr merge`, **never** a session in the primary
  checkout, **never** a worktree that still holds unlanded work (the tick skips, posts the path
  and exits non-zero), and **never a PR whose title OR BRANCH carries `k8s_autodeploy: false`**
  (#1939). That phrase is renovate.json's denylist marker, not a work order: the roles behind it
  (authelia, traefik, crowdsec, …) are denied because a failed deploy is one `probe.py health`
  cannot see, so the "look" the denial asks for is a person and not this session's gated land.
  The prompt leaves such a PR open and puts its `land.sh` command in the digest, and reads
  `headRefName` beside the title because the marker can survive in the branch alone (#2641).
  `ansible/tests/setup/test_renovate_agent_unit.py` pins that the prompt names the same marker
  the rule's `groupName` carries, and
  `ansible/tests/deploy/test_renovate_automerge_follows_the_autodeploy_denylist.py` asserts the
  marker sits on exactly the per-package rules whose pin a denied role owns. Which rules carry
  it, and the three rounds that got the marker into a title, are on the docs page.
- **Mode (explicit + reversible):** `renovate_agent_enabled`, which ships `false`. It alone
  arms the timer, and setting it back stops AND disables the unit (*Arming it*;
  `test_renovate_agent_unit.py` pins both directions). There is deliberately no run-once
  handler: a config edit must not kick a paid session as a side effect.
- **Authoritative sources:** the open PRs authored by `app/renovate` before and
  after the session, the same census of the session account's own PRs on the run's branches
  (what it handed off), CI's own verdict through `land.sh`, and the health gate `land.sh` runs.
  Never the session's closing paragraph — it reads confident whatever happened.
- **Abort valves:** `renovate_agent_max_prs` (the bound that stops a normal run),
  `renovate_agent_run_timeout_s` and the systemd `renovate_agent_unit_timeout` backstop, and
  `renovate_agent_budget_usd` as a runaway catch that must never be the binding constraint.
- **Required evidence:** a Discord digest whose headline is the before/after PR delta
  (`resolved`, `ran and no Renovate PR changed state`, or `FAILED — <reason>`), plus the
  `Renovate Agent — Alive` push tile, beaten only on exit 0; a crash pushes its own `down`
  with the exception text. `permission denials:` on a digest line is the signal the design
  rests on having gone missing.
- **Next-run review:** before raising a cap or widening the prompt, read the last week's
  digests for what the sessions actually resolved and what they timed out on.
