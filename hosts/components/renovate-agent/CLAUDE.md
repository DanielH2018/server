# `hosts/components/renovate-agent` — the unattended Renovate agent

A daily systemd timer that spends one headless Claude Code session
(`claude -p "/renovate-prs"`) on the repo's open Renovate PRs, then posts a Discord digest.
It is the acting half of the pair whose reporting half is
`hosts/components/renovate-notify/`: that component says what is open and what needs manual
work, this one does it.

`homelab-daniel-box` ships it.

The four bounds on a run and the worktree rules, what the Discord digest measures, the alive
tile's exit-code and token plumbing, how to exercise the wrapper without arming anything, the
modules `files/` ships, the lander, and the denylist marker's own history are in
`docs/renovate-agent-bounds-and-digest.md`.

## Arming it

The package ships the scripts, config, prompt and units whatever the flag says.
`renovate_agent_enabled` alone decides whether the timer's enable link ships, and setting
it back to `false` makes reconcile stop **and** disable the timer. That is the rollback, and
`hosts/tests/test_renovate_agent_unit.py` pins that both directions stay wired.

**A disarmed agent tells Prometheus so.** The package ships `renovate-agent-disarmed.prom` in
node_exporter's textfile directory. `RenovateAgentNotReporting` and `HostCheckOverdue` stay
quiet on the check while the marker reads 1, so a run started by hand while disarmed pages
nothing a day later. Arming removes the marker.
The rule then fires until the next run writes a verdict.

It ships `false`. Arming it is a decision with a spend attached and a blast radius — the
lander deploys what the session picks — not a sign the component is unfinished. The session's
own GitHub token and Claude credential are declared outputs, so `host_secrets.py check` fails a
host file that lacks them; `docs/renovate-agent-bounds-and-digest.md` has the checklist.

**The session can neither push nor merge.** It runs as `renovate-agent`, with a read-only
token, in its own clone, inside `homelab-renovate-agent-session.service`. The runner runs as
`renovate-agent-ctl`, which alone holds the write token and may start the session and lander
units; it starts the session and waits for it (`files/renovate_session.py`). It closes, comments on or re-runs a PR only by writing a request file,
which the runner carries out with its own token after the session ends (`files/run_requests.py`).
It lands nothing: it nominates a PR with a `land` request, and after the session ends the
runner starts `renovate-agent-land@<n>.service` for each nomination, which runs `land.sh` as
`sys_user` on a PR the lander re-checks itself.

**The session's tooling converges before each run.** `homelab-renovate-agent.service` runs
`/usr/lib/homelab/renovate-agent-setup` as root through `ExecStartPre=+`. It gives the agent its
home, the operator's uv, uvx, prek, claude_guard and claude_worktree, the pinned Python, Claude
Code and the clone. The agent runs every command that writes into its home.

**The session carries the shared guard hooks and a deny floor.** The package ships
`hosts/components/renovate-agent/files/claude-settings.json` to `/usr/lib/homelab/renovate-agent/`.
The setup script copies it to the agent's `~/.claude/settings.json` on every run, and copies the
operator's `~/.claude/hooks` beside it. The file registers three hooks: claude-guard on Bash,
the permission-request guard, and `protect-secrets` on the file tools. It also carries the
operator's deny list plus `sudo` and edits to the session's own settings, hooks and claude_guard,
and it disables bypass mode. The wrapper runs the session in auto mode, so the file also carries
the operator's `autoMode` deny rules, environment and `classifyAllShell`. It holds no allow rules
and no `env`, because the operator's carry `/home` paths that `ProtectHome=` hides. Both lists are
a snapshot of the operator's: a dotfiles change reaches the session only through a PR that edits
this file. `homelab-renovate-agent-setup.service` runs the setup at
boot and when the script or the settings change, so the file exists while the agent is disarmed and
`claude-guardrail-audit` watches it and the hooks copy. The agent owns both copies, so a session can
edit them until the next run replaces them; the auditd watch records that write, and
`claude-guardrail-ack` posts any copy that matches neither the packaged file nor the operator's
rendered hooks. The settings, the setup script and its
unit are on both landers' approval lists
(`hosts/components/claude-code/templates/claude-land-approval-paths.j2`, and `hosts/` in
`hosts/components/renovate-agent/files/t2-approval-floor`).

There is deliberately **no restart on a package install**: the session's unit is `policy:
manual`, because a run costs money and changes the fleet. Start one by hand
(**operator-only**):

```bash
sudo systemctl start homelab-renovate-agent && journalctl -u homelab-renovate-agent -f
```

## Autonomous-role contract (it merges and deploys with no human in the loop)

The acting half of the Renovate pair. Its authority is written down so an edit to the prompt,
the caps or the schedule cannot quietly widen it.

- **Scope / exclusions:** the repo's open PRs authored by `app/renovate`, worked through the
  `renovate-prs` skill and landed through the lander. **Never** a PR by another author
  (ENFORCED twice: the lander's own author check, and its unit's
  `LAND_REQUIRE_AUTHOR=app/renovate`, which `land.sh --arm-merge` reads —
  `scripts/deploy_tools/land_lib/merge.py:_require_author`;
  `hosts/tests/test_renovate_agent_unit.py::test_the_lander_pins_land_sh_to_renovates_prs`
  pins both to the wrapper's census). **Never** a landing of a PR that changes a path on
  `files/t2-approval-floor`, the one list every T2 lander reads, other than by a digest-only
  image bump, or whose branch
  another account pushed to, without the operator's approval of its head (dotfiles#967 slice
  1). The lander pins `land.sh` to the head it checked through `LAND_REQUIRE_HEAD`, and refuses
  a soak status `renovate[bot]` did not post (ENFORCED:
  `hosts/components/renovate-agent/tests/test_renovate_floor.py`).
  **Never** a push or merge by the session: its token has no
  contents write, so a `manual —` work order ends as a hand-off in the digest
  (`hosts/tests/test_renovate_agent_identity.py` pins the user and the token's route).
  **Never** a session in the primary
  checkout, **never** a worktree that still holds unlanded work (the tick skips, posts the path
  and exits non-zero), and **never a PR whose title OR BRANCH carries `k8s_autodeploy: false`**
  (#1939). That phrase is renovate.json's denylist marker, not a work order: the roles behind it
  (authelia, traefik, crowdsec, …) are denied because a failed deploy is one `probe.py health`
  cannot see, so the "look" the denial asks for is a person and not this session's gated land.
  The prompt leaves such a PR open and puts its `land.sh` command in the digest, and reads
  `headRefName` beside the title because the marker can survive in the branch alone (#2641).
  The lander reads the denied roles from `scripts/dev/k8s_autodeploy_counts.py --denylist` in
  the primary checkout, and refuses when that command fails or prints no list.
  `hosts/tests/test_renovate_agent_unit.py` pins that the prompt names the same marker
  the rule's `groupName` carries, and
  `tests/deploy/test_renovate_automerge_follows_the_autodeploy_denylist.py` asserts the
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
  The templates read each cap, and the permission mode, from the literals in
  `templates/renovate-agent-pins.j2`, which is on the lander's approval list, so a host_vars
  edit moves none of them. The defaults hold the same values for the docs fragments
  (`hosts/tests/test_renovate_agent_unit.py::test_an_inventory_value_moves_no_cap`).
  `docs/renovate-agent-bounds-and-digest.md` has why each is the var it is and why the budget
  must never be the one that binds.
- **Required evidence:** a Discord digest whose headline is the before/after PR delta, plus the
  `renovate-agent.prom` verdict, written only on exit 0 to node_exporter's textfile directory
  from a root-run `ExecStartPost` (M6 batch B4): the agent's own user has no write there and must
  not gain it. `RenovateAgentNotReporting` reads it. `docs/renovate-agent-bounds-and-digest.md`
  has the full delta vocabulary, the crash path, and why a `permission denials:` line is the one
  to act on.
  A digest the host could not deliver waits in `<STATE_DIR>/discord-spool/` and goes out,
  marked as delayed, at the start of the next daily tick, a quiet skip included (#3905).
- **Next-run review:** before raising a cap or widening the prompt, read the last week's
  digests for what the sessions actually resolved and what they timed out on. The session's
  own 2700-second wall clock in `templates/renovate-agent-pins.j2` is an estimate from runs
  that still landed in-session; after the first armed week, compare it with the sessions'
  `duration_ms` in `last_session.json`.
