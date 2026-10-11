---
id: "0020"
title: Red-authored code runs only in a per-phase sandbox with its own transient uid
status: Proposed
date: 2026-10-11
governs: []
---

# ADR-0020: Red-authored code runs only in a per-phase sandbox with its own transient uid

## Status

Proposed. It answers [#3865](https://github.com/DanielH2018/server/issues/3865) and
[#4508](https://github.com/DanielH2018/server/issues/4508), and it replaces the third-UNIX-user
plan the operator accepted on 2026-10-10. Nothing in it is built yet.

## Context

A `red-green` batch splits test authoring from implementation. A headless session, the red
author, writes failing tests. The red gate checks that they fail at the base commit. The
implementer then makes them pass, and the green gate checks the result. The split is worth
something only if the red author's influence on the batch ends with its commit.

`fanout_lib/review/processes.py:reaping` kills every process the red phase leaves in the
pipeline's process tree, orphans included (#3852). A process the red author starts outside that
tree survives. `systemd-run --user`, a user timer and a crontab entry are the examples. #3865
asked for a fix. The 2026-10-10 comments rejected detection by unit name, by cwd and by
per-phase scope. The operator then accepted the gap until a third UNIX user could run the red
phase.

### What was measured on daniel-box, 2026-10-11

- **No unprivileged sandbox runs here.** `unshare -U -r true` fails with
  `write failed /proc/self/uid_map: Operation not permitted`. Ubuntu sets
  `kernel.apparmor_restrict_unprivileged_userns=1`, so `bubblewrap` needs a host-wide AppArmor
  exception before it can start.
- **The user manager ignores sandbox properties and does not say so.** A
  `systemd-run --user -p InaccessiblePaths=/run/user/1000/bus` unit started. Inside it,
  `systemd-run --user true` exited 0. A per-phase `--user --scope` cannot close #3865.
- **`NoNewPrivileges` stops cron.** Under `setpriv --no-new-privs`, `crontab -` fails with
  `/var/spool/cron/: mkstemp: Permission denied`.
- **The red phase runs in production.** `fanout-4480.service` ran
  `fanout.py review --red-green`. The 2026-10-10 acceptance assumed it had not run yet.
- **Delegation starts at `user@<uid>.service`.** Every `cgroup.procs` file above it, including
  `user.slice` and `user-<uid>.slice`, is root-owned.
- **The agent's toolchain is not readable by another uid.** `login.defs` sets `UMASK 027`.
  The agent's home and its uv-managed interpreters are group- or owner-only.

### Red code runs again after the red phase

The red author's tests execute in three more places, all as the pipeline user and outside any
reap:

- **The implementer runs them**, because it has to make them pass.
- **The reviewers run them.** `review/review_system_prompt.md:7` tells each reviewer to "run the
  tests that cover the change".
- **The green gate runs them** (`review/review.py`, the `self.gates.green` call). #4508 records
  this.

A test can start `systemd-run --user` from module scope as easily as the red session can. So a
boundary around the red phase alone does not end the red author's influence. The third-user plan
has the same hole: it isolates the session that writes the tests, not the code it wrote.

### Options considered

| Option | Why not chosen |
|---|---|
| Detect and stop what the red phase created | Rejected on #3865. The red author chooses the unit name, the cwd and the environment. |
| A third UNIX account | Needs an account, a home, worktree ACLs and its own user manager per host. A persistent home carries state from one batch to the next. It still leaves red code running in later phases. |
| A system unit with `User=` the pipeline user | Closes every route out of the cgroup except one: the red code keeps the pipeline's uid, so it can still `kill()` a concurrent batch or a landing. systemd 255 has no `PrivatePIDs=`. This is the fallback. |
| `bubblewrap`, or Claude Code's sandbox runtime | Needs an AppArmor profile that re-enables user namespaces for `bwrap` on the whole host. Claude Code's built-in sandbox covers only Bash, not file tools or hooks. |
| `Landlock` | Pathname-socket `connect()` control (`LANDLOCK_ACCESS_FS_RESOLVE_UNIX`, ABI 9) is not in any kernel this host can install, so it cannot stop a connect to the user bus. Signal scoping needs ABI 6, which kernel 6.8 lacks. |
| A Kubernetes Job | Agents have no write path to the cluster. Ansible is the only one. Egress NetworkPolicies are not enforced on daniel-box (ADR-0009), and the Job would need its own image pipeline. |

## Decision

Every step of a `red-green` batch that executes red-authored code runs in a root-defined
systemd system unit with `DynamicUser=yes`. Those steps are the red author, the red gate, the
green gate, and in the last slice the implementer and the reviewers. Each run gets a uid that
systemd allocates at start and releases at stop. The pipeline is the only part of the batch
outside the sandbox. It stages each run's inputs, reads its outputs as hostile data, and
pushes.

`DynamicUser=` is a different uid, not an account. It has no `passwd` entry, no home and no
persistent state, so nothing has to be provisioned for it. The different uid is what makes
`kill()`, `ptrace` and file ownership attributable by the kernel. If the operator rules out any
uid other than the pipeline's, the same templates with `User=<pipeline user>` keep every other
property.

### Units and who may start them

Ansible renders two templates for each pipeline user:

- **`fanout-gate-<user>@.service`** runs tests. It has `PrivateNetwork=yes` and no credential.
- **`fanout-agent-<user>@.service`** runs a Claude session. It can reach only the Anthropic API,
  and it loads one credential.

A polkit rule shaped like `50-claude-land.rules` lets each pipeline user `start` and `stop` only
its own two templates. The instance name is `<batch>-<phase>-<run id>`, limited to lowercase
letters, digits and dashes. Both the rule and the unit refuse anything else. Each template sets
`Slice=user-<uid>-fanout.slice`. That slice nests under `user-<uid>.slice`, so `placement.py`'s
fleet and login-plane readings both count the sandbox. Moving a process out of the slice needs
write access to a root-owned `cgroup.procs`, so every process a run starts stays attributable to
it.

### What a run can see

| Path | Access |
|---|---|
| `/var/lib/fanout-sandbox/<user>/<instance>/in` | Read-only, at the same path. The pipeline stages it with a world-readable mode. |
| `/var/lib/fanout-sandbox/<user>/<instance>/out` | Writable. This is the run's only output. |
| `/tmp` | Private, and systemd deletes it at stop. The run's working clone and its `HOME` live here. |
| `/opt/fanout-sandbox` | Read-only. A root-owned toolchain that Ansible pins: the interpreter, `uv` and `claude`. |
| `/usr`, `/etc` | Read-only. |
| `/home`, the agent home, `/var/lib/fanout-sandbox` | An empty tmpfs, except the run's own `in` and `out`. |
| `/run/user`, the system bus, systemd's private socket | Inaccessible. |

The run gets no route out of its cgroup. Each class of route has a property that closes it:

| Route | Closed by |
|---|---|
| The user manager: units, timers, the agent's other sockets | No user manager exists for a dynamic uid, and `/run/user` is inaccessible |
| polkit grants on the system bus | The bus socket and systemd's private socket are inaccessible, and polkit grants nothing to a dynamic uid |
| cron, at, sudo, `setuid` binaries | `NoNewPrivileges=`, which `DynamicUser=` implies |
| ssh, cached git credentials, loopback listeners | The homes are hidden, and the gate template has no network |
| Signals and `ptrace` on another batch | A different uid, `ProtectProc=invisible`, and `SystemCallFilter=~@debug` |
| Files another session executes later | `ProtectSystem=strict`, with `ProtectHome=` and the tmpfs masks above |
| Namespaces, `setuid` files, kernel tunables | `RestrictNamespaces=`, `RestrictSUIDSGID=`, the `Protect*` set and an empty capability set |

`systemd-analyze security --offline=yes` scores a draft of the gate template at 0.9 (SAFE).

### Inputs and outputs

The pipeline stages a shallow clone at the commit the run starts from. Alongside it go a venv
built from that commit's `uv.lock` against the `/opt` interpreter, the prompt and brief, and a
read-only copy of the Stop hook. For a gate run it also stages the gate driver from the
pipeline's own checkout, never from the batch tree.

Inside the run, a fixed launcher clones the staged tree into `/tmp` and runs the phase. It then
writes three things to `out/`: a git bundle of `<start>..HEAD`, the session's JSON report or the
gate's outcomes, and, for a session, the transcript a later `--resume` needs.

The pipeline treats every file in `out/` as hostile. It opens each one without following
symlinks and caps its size. It fetches the bundle with `transfer.fsckObjects` under
`hardened_runs.hardened`. It then applies the checks the red gate applies today: descent from
the base, and `is_test_path` on every changed file. The red author never sees the batch
worktree, so whatever it plants on disk stays in its `/tmp`.

### Credentials and egress

The agent template loads a `claude setup-token` token through `LoadCredential=`. That token is a
one-year subscription token that can only make model requests. The operator mints it, and it is
encrypted in SOPS and registered for rotation. No run ever reads the agent's refreshing
`.credentials.json`. A refresh inside the sandbox would rotate the token every other session on
that user depends on.

Egress starts as `IPAddressDeny=any`, plus `IPAddressAllow=` for the inbound ranges Anthropic
publishes and a pinned hosts file, so no DNS is needed. A later slice replaces this with
`PrivateNetwork=yes` and a pass-through proxy on a pathname socket. That proxy identifies the run
from `SO_PEERCRED` and the peer's cgroup. It allows only the model endpoints, refuses a bearer
token whose hash does not match the minted one, and logs each run's token use outside the run's
reach. Having the proxy inject the credential, so the run never holds it, is an optional last
step. Anthropic documents a gateway that forwards a subscription token. It does not document a
gateway that injects one, so that step waits on a probe the operator runs.

### Lifecycle

Each template is a oneshot with `TimeoutStartSec=`. `RuntimeMaxSec=` has no effect on a oneshot.
When the launcher exits, systemd kills the cgroup, removes the run's IPC objects and private
`/tmp`, and releases the uid. The pipeline stops its instances on every exit path. The
pipeline's own unit stops them again in `ExecStopPost=`. `place launch` stops any instance whose
batch has no live pipeline.

## Consequences

**What it closes:**

- The #3865 Verify-by, by construction. `systemd-run --user` has no manager to reach, and every
  process the run started is gone when the unit stops.
- #4508, once the green gate runs in the gate template.
- The writes into the worktree that #3864, #3879 and #3884 detect and repair. Those checks stay
  as tripwires.
- Spend and activity attribution, once the proxy lands. The record can carry each run's
  invocation id, uid, CPU, memory and token use, measured outside the run.

**What it costs:**

- Root-provisioned pieces in the `claude_code` role, on both hosts: two templates per pipeline
  user, a polkit rule, the `/opt` toolchain and the credential.
- Ansible pins the sandbox's `claude`, so it lags the agent's auto-updated copy until Renovate
  bumps it.
- Every staged tree copies a venv, because the agent's uv cache is not readable by another uid.
- The implementer and the reviewers of a `red-green` batch lose live diagnostics, `ssh` and
  `gh`. The pipeline opens the PR instead of the session. That limits the label to code-and-test
  issues.
- CI cannot start a system unit. CI tests the rendered unit's properties, the polkit instance
  table and the output handling. The escape payload runs after deploy, once per host.
- The operator mints the token by hand, once a year.

**What it leaves open:**

- Until the proxy slice, the agent template shares the host network namespace, so abstract UNIX
  sockets stay reachable. The ones on daniel-box belong to root daemons that take no work from
  any other user.
- Red-authored text still reaches later sessions as prompt input. That is the same class as any
  issue body or PR comment.
- Until the injection step, the token sits inside the agent run. The bearer pin limits its use
  to model requests on the operator's own account.

**Reversing it:** each slice is gated on a variable, as `docs/claude-agent-user.md` does for
its slices. Turning the variable off returns that step to the pipeline's own process tree.

### Slices

Each slice can be exercised on its own.

1. **The sandbox exists and the escape payload proves it.** Templates, polkit rule, `/opt`
   toolchain, and a launcher with a `probe` mode that tries every route above. The operator starts
   one probe instance per host and reads `out/probe.json`. The pipeline does not change.
2. **The red author and the red gate run in it.** This slice closes #3865.
3. **The green gate runs in it.** This slice closes #4508.
4. **The agent template moves to `PrivateNetwork=yes` behind the pass-through proxy.** This adds
   the per-run ledger and the bearer pin.
5. **The implementer and the reviewers of a `red-green` batch run in it.** The pipeline pushes
   and opens the PR.
6. **Optional: the proxy injects the credential**, if the operator's probe shows the API accepts
   it.

## Governs

Nothing yet. The slices that build this add `# DECIDED: … (ADR-0020)` markers to the unit
templates, the polkit rule and `review.py`, and list them in `governs:`.
