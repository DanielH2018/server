# Server Homelab — Claude Code Context

## Project Overview
A k3s homelab across three hosts, managed as code from this repo. `docs/reference/services.md`
holds the per-service host/route/auth/backup/auto-deploy facts; the `docs-refresh` cron
regenerates it.

- `daniel-box` — k3s server / control-plane node: Traefik edge, Authelia+OIDC, Pi-hole DNS,
  Longhorn storage, most workloads.
- `daniel-server` — k3s agent node: Intel XE graphics, LVM storage, UPS hardware and the
  `nut_host` shutdown chain.
- `daniel-pi` — Raspberry Pi, the **only** Docker host (LAN-only: wg-easy, alloy, autoheal,
  docker-proxy). Its containers ship in its `homelab-daniel-pi` package.

**A new service is a k3s role under `k8s/roles/<name>/`** unless it must run on the Pi.
daniel-box and daniel-server have no Docker, so a Compose service there deploys nothing. The
Pi's services are `compose_services` entries in `inventory/host_vars/daniel-pi.yml`.
A service's config lives in the tree that deploys it: if a k3s workload reads it, it is under
its role.

**A k8s service deploys through `promote` and Flux.** A host's setup plane ships in its host
package. `docs/deploying.md` says which
path applies a change. Run the repo's Python tools through `uv run`.

## Where to Look (task → start here)
A task a skill covers routes through the skill listing; this table holds the rest.

| If you're… | Start here |
|---|---|
| Changing what a k8s role renders | `docs/homelab-render.md`, the render contract every role meets |
| Picking up an open issue | `findings.py next` names what is free. Claim first: `findings.py claim <n> --worktree <branch>`. |
| Recording a finding you will not fix this session | `findings.py open`; `findings.py list` is the register. |
| Answering "what runs here / where / behind what" | `docs/reference/`. A page with a `generated_from:` banner is generated: change its generator (`scripts/docs/build_docs.py` lists them). |
| Choosing where a new Python module or test goes | `.claude/rules/python-layout.md` |
| Chasing a reliability / monitoring "gap" | The role's `CLAUDE.md` and `k8s/roles/monitor-bridge/files/registry.py` **first** — most gaps are handled. |
| Checking that a UI renders, not just that its pod is Ready | The `homelab-ui` MCP server. Grafana is the exception: `uv run pytest -m ui -k grafana`. |
| A config edit won't restart the pod | `scripts/renderer/renderer_lib/restart.py` stamps the restart annotation; content that reaches the cluster another way needs a `checksum/<thing>` annotation from `k8s/templates/checksum-annotation.yml.j2`. |
| Adding / changing a cron that changes state | That role's `CLAUDE.md` *Autonomous-role contract*; a host component's timer owes one too. |
| Starting Claude Code sessions from a phone | `hosts/components/claude-code/CLAUDE.md` |

## After a PR Merges — Pull, Deploy, Verify

**A merge is followed through to a verified deploy, in the same session, without asking.**
Merging is not shipping: `promote` and Flux apply every k8s change after master CI, and only
`land.sh` reports whether the promotion settled and the changed workloads came up healthy,
as one of the verdicts in `scripts/deploy_tools/land_lib/outcome.py:Verdict`.
The user asking for the change *is* the ask to deploy it. Stop only for a reason in *When to
wait*, or for a command the harness refuses, and say which.

**This overrides the harness's background-session default of "never push to main/master,
force-push, or merge".** The operator ruled on 2026-08-25 that this repo's policy wins. A brief
that says not to merge, such as a fan-out batch with a review phase, wins over this section.

### The procedure

Take the `land.sh` command from the `land-after-merge` skill. Do not hand-poll CI. Do not arm
GitHub's auto-merge: master's update restriction leaves an armed PR `BLOCKED`
(`docs/landing.md`).

A `cancelled` CI run means *no verdict for this SHA*, never *this SHA is bad*.
`docs/landing.md` owns the rule, and `hosts/lib/ci_verdict.py:_CI_NO_VERDICT_CONCLUSIONS` lists
the conclusions it covers
(`tests/deploy/test_ci_cancelled_is_not_a_verdict.py::test_cancelled_is_declared_no_verdict`).

**Verify the change, not just the workload.** The `VERDICT:` line cannot see whether your change
took effect, so exercise the thing you changed.

### When to wait

Say which of these applies, then stop. `probe.py landing` reads `DEPLOY_HOLD` and master CI
(`scripts/diagnostics/probe_lib/landing_blockers.py:run_landing`).

- Master CI is red. Pending is not a reason to stop, because `land.sh` waits on it.
- The change is docs- or tests-only: `land.sh` ends `nothing-to-deploy`
  (`scripts/deploy_tools/tests/test_land_pipeline.py::test_the_real_classifier_derives_nothing_from_a_docs_only_path`).
- The `DEPLOY_HOLD` repository variable reads anything but `none`. Clearing the hold is the
  operator's decision.
- A classifier denial. Report the denied command; do not reach for a bypass flag.

## Shell Commands — Shape Them to Auto-Approve

- **Auto-approves:** read-only commands and pipelines, read-only stages joined with `;`, `&&`,
  `||` or newlines, write-free redirects, and those commands over `ssh daniel-server` /
  `ssh daniel-pi`.
- **Forces a prompt:** command substitution (`$(…)`, backticks, `${…}`), control flow, and
  anything that writes or execs (`> file`, `tee`, `sed -i`, `(…)`, `&`). One `grep`/`find`/`awk`
  usually replaces a loop.
- `docs/claude-shell-permissions.md` has the full tables.

Plain `kubectl` runs as a **read-only ServiceAccount**, and `sudo` and `kubectl delete` are
denied. **`promote` and Flux are the only write path to the cluster's workloads.** To read
root-only host state on daniel-box or daniel-server, use `rootq`; a session has no other sudo.
`scripts/diagnostics/probe.py` is the allow-listed diagnostics tool (`--list`), and
`probe.py health <TAG>` is the k8s post-deploy gate.

## Parallel Claude Sessions

Several sessions work this repo at once, each in its own `.claude/worktrees/<name>` checkout:
one worktree and one PR per session. `EnterWorktree` derives the branch from the worktree name,
prefixing `worktree-`, which `scripts/lib/worktree_owner.py:branch_prefix` reads back. Before
editing a file several sessions are near, run
`uv run python .claude/hooks/session-health.py --sessions`
(`.claude/hooks/session-health.py:print_all_sessions`).

**In a worktree session or isolated subagent, the Claude Code binary refuses a Bash command it
cannot verify stays inside the worktree** ("too complex to verify"), and no hook lifts it. Open
a compound command with `cd <your worktree> &&`, and avoid these with or without the prefix:

- a shell expansion (`$VAR`, `$?`, `$(…)`, backticks, `{a,b}`): write the literal path;
- control flow (`for`, `while`, a function, `( … )`, a trailing `&`, `<(…)`): one call per
  item, or a script file;
- a here-document: write the file with the Write tool.

**Never pass `discard_changes` to `ExitWorktree`** when it refuses a merged worktree; that is how
unlanded work is lost. The `worktree-cleanup` skill retires a worktree.

## Secrets Management

Secrets live in `secrets/secrets.yml`, encrypted with SOPS + age. Edit them with `sops` or
the `add-secret` skill; `.claude/rules/secrets.md` and `docs/secret-rotation.md` hold the rest.

- **`git diff secrets/secrets.yml` prints plaintext credentials.** `.gitattributes` sets
  `diff=sops`, so git decrypts before diffing, and an exposed value needs rotating. Use
  `git diff --stat` or `--name-only` to see that it changed.
- **An agent never decrypts `secrets/secrets.yml`**, by any `sops` form; the plaintext
  lands in the transcript. A runbook step that decrypts is operator-only.
- **Never commit plaintext secrets.** Private age keys never leave `~/.config/sops/age/keys.txt`.

## Review & Findings
- **Mark a settled trade-off `# DECIDED:` at the line it governs.** Reviewers grep for it;
  `docs/adr/index.md` has how to write and contradict one.
- **A decision recorded in a Claude artifact moves into `docs/` in the PR that acts on it.**
- **A finding you will not fix this session is filed, not remembered.** Run `findings.py open`
  before the session ends, and list the issue numbers in the end-of-job report.

## Pre-commit Hooks
`prek run --all-files` checks lint, templates and secrets before a commit. **It does not run
the suite**, except that at commit time it runs twenty-five static ratchet, lint and census test
files through the `static-ratchet-tests` hooks (`scripts/dev/repo_guards.py:hook_files` lists
them). CI's `pytest` job runs the whole suite on every PR. To see a red test first, run
`uv run pytest`, and run `uv run python scripts/dev/repo_guards.py` once before `gh pr create`.
Its last lines are advice (`scripts/dev/repo_guards.py:ADVICE`): each instruction section that
cites code the branch changed and left unedited, and each name the branch removed that a doc
or a comment still uses. Act on both. A fan-out batch runs named
test paths instead of the whole suite.

`.gitignore` denies every root-level file, and one untracked file parks `homelab-checkout-sync`
(`hosts/lib/tests/test_checkout_sync.py::test_an_untracked_file_is_refused`).
