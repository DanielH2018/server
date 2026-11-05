### PR B-7

PR B-7, the docs, skills, agents and evals that still pointed a reader at Kuma: the PR that
adds this record, [#7058](https://github.com/DanielH2018/server/pull/7058), for [#7052](https://github.com/DanielH2018/server/issues/7052). It deploys
nothing. Each item of *Phase B* item 8, and what changed:

- **The root `CLAUDE.md` *Where to Look* rows.** No change. `git grep -n -i kuma -- CLAUDE.md`
  returns nothing, because B-2 had already edited the *Claude tooling* bullet.
- **`.claude/skills/add-secret/SKILL.md`.** The 32-hex Kuma push-token bullet is replaced by a
  rule never to add a push token, pointing at ADR 0026's producer recipes. The `auto` tier
  example says the remaining push tokens are the only `auto` entries.
- **`.claude/skills/new-container/SKILL.md`.** No change. It does not name Kuma.
- **`.claude/skills/homelab-review/accepted-designs.md`.** The "down = no heartbeat" bullet now
  names the alert-plane deadmen (`<Check>NotReporting`, `HostCheckOverdue`,
  `BridgeCheckStale`). The Pi bullet names B8's `PiHostDown` and `PiPortDead` and A1's Pi rules
  instead of static Kuma labels, and drops `HOST_METRIC_ORIGIN_EXCLUDE`, which no file in the
  tree holds. `uptime-kuma-data` stays in the no-backup bullet, because the claim exists until
  C-2b deletes it.
- **The three reviewer agents.**
    - `homelab-backup-observability-reviewer.md` describes one alert plane, the producer
      recipes and the deadmen, and its method checks the rules directory before it reports a
      missing monitor.
    - `homelab-cicd-reviewer.md` names the secret-rotation audit timer and
      `SecretRotationAuditFailed` in place of the daily Kuma push.
    - `homelab-container-reviewer.md` is unchanged. Its one mention is the
      AutoKuma macro the Pi compose files inline, which the plan's census allowlist keeps.
      [#7210](https://github.com/DanielH2018/server/issues/7210) later deleted the macro and
      the mention.
- **`.claude/hooks/hooklib/service_lines.py`.** Two docstrings change: the release-staleness
  verdict sits beside a textfile metric, and `ReleaseStalenessCheckNotReporting` pages a dead
  cron. The `--kuma` flag the cron passes to `probe.py releases` is a live CLI flag, so it
  stays.
- **The eval cases.** `homelab-backup-observability-reviewer/001` grades a missing
  blackbox-ingress probe instead of a missing Kuma monitor; `kuma: false` stays its opt-out
  until C-4a renames the key. `002-kuma-push-no-retries` is replaced by `002-wan-gate-no-for`,
  which grades the deliberate absence of a `for:` on `WANUnreachable`. The two
  `homelab-cicd-reviewer` cases quote the alloy compose file's live label. `evals/history.json`
  keeps the old case id as history, because the eval-run cron owns that file.
- **`ansible/roles/k8s/homelab-mcp/CLAUDE.md`.** The `/health` router now exists for the
  `blackbox-extra` job's `http_body_ok` probe, not for a Kuma tile.
- **The ADR (C-0).** No change. ADR 0026 was written and accepted on 2026-10-24.

The same PR adds a *Token PR* line to each of the 15 batch records that
[#6949](https://github.com/DanielH2018/server/pull/6949) covered
([#6950](https://github.com/DanielH2018/server/issues/6950)). It also re-checked the comment
halves of [#6821](https://github.com/DanielH2018/server/issues/6821) and
[#6882](https://github.com/DanielH2018/server/issues/6882). Outside history, generated pages and
tests, no comment still names `probe.py kuma-drift` or a Loki-reading `probe.py alerts`, so
nothing changed there. Their C-1a halves, `format_kuma` and the `KUMA_QUERY` arm of
`scripts/diagnostics/probe_lib/monitors.py`, wait for C-1a.

**Files B-7 did not reword.** About 35 hand-written `docs/` pages still name Kuma, among them
`docs/monitor-bridge-checks.md`, `docs/k3s-node-plane-crons-and-incidents.md` and
`docs/healthchecks-io-deadman.md`. Item 8 does not list them, and no other M6 PR row does.
[#7056](https://github.com/DanielH2018/server/issues/7056) tracks the sweep, after C-1 and C-3a.

`ansible/roles/k8s/monitor-bridge/files/bridge/common.py` is one of the files #6821 names. Two of
its docstrings still say the bridges push to Kuma and that Kuma forwards to Discord. B-7 left
them, because the file ships in the monitor-bridge and autofix-bridge builds. Both roles are
flux-owned, so a docstring edit would roll two workloads in a PR that otherwise deploys nothing.
[#7060](https://github.com/DanielH2018/server/issues/7060) rewords them in the next real edit to
the file.
