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
tile's exit-code and token plumbing, how to exercise the wrapper without arming anything, the
modules `files/` ships, the lander, and the denylist marker's own history are in
`docs/renovate-agent-bounds-and-digest.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "renovate_agent"` when `inventory_hostname ==
  renovate_agent_host`
- **Timer:** `renovate-agent.timer` (`OnCalendar=*-*-* 06:00:00 America/Chicago`)
<!-- /generated_from -->

## Arming it

The role installs the scripts, config, prompt and units on every run.
`renovate_agent_enabled` alone decides whether the timer is enabled and started, and setting
it back to `false` stops **and** disables the timer. That is the rollback, and
`ansible/tests/setup/test_renovate_agent_unit.py` pins that both directions stay wired.

It ships `false`. Arming it is a decision with a spend attached and a blast radius — the
lander deploys what the session picks — not a sign the role is unfinished. The role refuses to
arm without the session's own GitHub token and Claude credential;
`docs/renovate-agent-bounds-and-digest.md` has the checklist.

**The session can neither push nor merge.** It runs as `renovate_agent_user`, with a token that
has no contents write, in its own clone. It lands a PR only by starting
`renovate-agent-land@<n>.service`, which polkit lets it start and which runs `land.sh` as
`sys_user` on a PR the lander re-checks itself.

**`--check` fails at "Enable and start the timer", and that is not a bug in the role.** Check
mode writes no unit file, so systemd reports `Could not find the requested service` for a
`renovate-agent.timer` that does not exist. Every task before it reports correctly, which is
what a check run of this role is good for.

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
  `renovate-prs` skill and landed through the lander. **Never** a PR by another author
  (ENFORCED twice: the lander's own author check, and its unit's
  `LAND_REQUIRE_AUTHOR=app/renovate`, which `land.sh --arm-merge` reads —
  `scripts/deploy_tools/land_lib/merge.py:_require_author`;
  `ansible/tests/setup/test_renovate_agent_unit.py::test_the_lander_pins_land_sh_to_renovates_prs`
  pins both to the wrapper's census). **Never** a push or merge by the session: its token has no
  contents write, so a `manual —` work order ends as a hand-off in the digest
  (`ansible/tests/setup/test_renovate_agent_identity.py` pins the user and the token's route).
  **Never** a session in the primary
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
- **Mode (explicit + reversible):** `renovate_agent_enabled`, which ships `false` (see *Arming
  it* above; `test_renovate_agent_unit.py` pins both directions).
- **Authoritative sources:** the open PRs authored by `app/renovate` before and
  after the session, the lander's verdict files, CI's own verdict through `land.sh`, and the
  health gate `land.sh` runs.
  Never the session's closing paragraph — it reads confident whatever happened.
- **Abort valves:** `renovate_agent_max_prs`, `renovate_agent_run_timeout_s`, the systemd
  `renovate_agent_unit_timeout` backstop, and `renovate_agent_budget_usd` as a runaway catch.
  `docs/renovate-agent-bounds-and-digest.md` has why each is the var it is and why the budget
  must never be the one that binds.
- **Required evidence:** a Discord digest whose headline is the before/after PR delta, plus the
  `Renovate Agent — Alive` push tile, beaten only on exit 0. `docs/renovate-agent-bounds-and-digest.md`
  has the full delta vocabulary, the crash path, and why a `permission denials:` line is the one
  to act on.
  A digest the host could not deliver waits in `<STATE_DIR>/discord-spool/` and goes out,
  marked as delayed, with the next tick's post (#3905).
- **Next-run review:** before raising a cap or widening the prompt, read the last week's
  digests for what the sessions actually resolved and what they timed out on.
