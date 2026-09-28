# Self-hosted GitHub Actions runner: the measurements and the verdict

**Verdict: no-go while the repository is public, and conditional after that.** A self-hosted
runner saves nothing on a public repository, because GitHub bills Actions minutes only on
private ones. Running one on a public repository executes pull-request workflows from forks on
a host that holds the SOPS age key and the cluster kubeconfig, which GitHub's own guidance
tells you never to do. The spike therefore measures a configuration that the operator must
first re-enable by making the repository private again.

The numbers below say that configuration works but costs latency. `daniel-server` carries the
whole suite in 148s on an idle host against the hosted runner's 105-109s, and in 294.5s while
two fan-out agents run their own `pytest` — which is the condition that holds on a landing day.
It also says the 4-shard matrix is the wrong shape on one host, that one runner instance runs
one job at a time, and that the runner needs its own systemd slice.

Filed as #2245. Nothing here changes CI.

## What was measured, and how

Every self-hosted number comes from `daniel-server` on 2026-09-22, against `origin/master` at
`52826dd8`, with warm `uv`, prek and Galaxy caches. The suite was 11,992 passed and 112
skipped.

Each command ran inside its own transient cgroup scope:

```bash
systemd-run --user --scope -q -p MemoryAccounting=yes <command>
# then, from inside the scope, read /sys/fs/cgroup<scope>/memory.peak
```

`memory.peak` is the right instrument for the cap question, because `MemoryHigh` enforces
against the same accounting — page cache charged to the cgroup included.

**Read every number beside its load average.** `daniel-server` runs the fan-out agents that
produce the landings CI then has to verify, so an idle measurement describes a condition that
does not hold when CI matters. Two other fan-out agents were running their own `pytest` during
this session, at a 1-minute load of 14.3 on 8 cores.

### On `daniel-server`

| Job, as CI invokes it | Wall | Peak | 1-min load before |
|---|---|---|---|
| `ansible-galaxy collection install`, cold cache | 44.9s | 152 MiB | 0.52 |
| `pytest`, whole suite, one job | 110.4s | 1,635 MiB | 0.42 |
| `pytest`, shard 1 of 4, alone | 30.3s | 546 MiB | 3.84 |
| `pytest`, all four shards at once | 89.2s | 2,539 MiB | 2.85 |
| `hooks` (the prek sweep plus `mkdocs --strict`) | 36.9s | 486 MiB | 10.65 |
| `ansible-lint` | 62.8s | 480 MiB | 8.35 |
| One `pytest` job beside `hooks` and `ansible-lint`, idle host | 148.4s | 1,641 MiB | 5.44 |
| The same three jobs, with two fan-out agents also running `pytest` | 294.5s | 1,501 MiB | 15.29 |

The last two rows are the proposed self-hosted shape, measured twice. The second is the
condition that actually holds on a landing day, and it doubles the wall clock.

The `hooks` and `ansible-lint` jobs exited non-zero in that second sample, on
`files were modified by this hook` rather than on any lint finding — this session was editing
the tree while the sample ran. The wall-clock and memory figures stand, because a few text
writes cost no CPU, but do not read those two exit codes as a result.

### On GitHub's hosted runners

Two pushes to master the same day, from `gh api .../actions/runs/<id>/jobs`:

| Job | Run `35725608472` | Run `35719761837` |
|---|---|---|
| `hooks` | 59s | 67s |
| `ansible-lint` | 91s | 101s |
| `pytest`, slowest of four shards | 80s | 82s |
| `prek` gate | 4s | 2s |
| **Run wall clock** | **105s** | **109s** |
| **Billed minutes** | **12** | **13** |

## Wall clock per landing

The hosted runner finishes a push run in 105-109s. `daniel-server` needs 148s on an idle host
and 294.5s while two fan-out agents run their own `pytest` — 1.4x and 2.8x the hosted figure.

The reason is that GitHub gives every job its own machine. Each of the four `pytest` shards
gets eight cores of its own, so a run costs the slowest shard. One host gives every job the
same eight cores.

### One runner instance runs one job at a time

A self-hosted runner executes a single job and then takes the next, so `hooks`, `ansible-lint`
and `pytest` do not overlap on one runner. Serially they cost the sum of their solo
measurements: 110.4 + 62.8 + 36.9 = 210.1s, against the hosted 105-109s.

Getting them to overlap means registering several runner instances on the one machine. Whether
those instances can share one work directory was not settled here: `hooks` and `ansible-lint`
both failed the contended sample with `files were modified by this hook`, but this session was
editing the tree while that sample ran, which is sufficient on its own to produce that failure.
A clean-tree rerun with a concurrent `uv run python -m pytest` passed both hooks, so no
shared-workspace collision was demonstrated. **Treat it as unmeasured**, and give each instance
its own work directory until someone measures it.

### The matrix is the wrong shape on one host

`addopts` in `pyproject.toml` already carries `-n auto`, so a single `pytest` job fans across
every core on its own. Four shards on one host is four `-n auto` runs contending for the same
eight cores — 32 xdist workers where there are 8 cores.

The measurement is decisive: four shards at once take 89.2s and peak at 2,539 MiB, against
110.4s and 1,635 MiB for a single job. Sharding buys 19% of wall clock for 55% more
memory and four times the job count. On a hosted runner the same matrix buys 3.3x
(267s as a single job against an 80s pole shard), which is why it exists.

**So `strategy.matrix` collapses to one job under `runs-on: self-hosted`.** Collapsing also
halves the peak, which is what makes the memory answer below comfortable.

## The minutes arithmetic

This is the part that explains the 2026-09-21 blowout better than any timing.

GitHub bills per job, rounded up to the minute. The four `pytest` shards each run 65-82s, so
each bills 2 minutes — 8 minutes for a job whose serial form bills 5. A push run bills 12-13
minutes; a pull-request run bills about the same, so a landing costs roughly 25 minutes.

At 25 landings a day that is 625 minutes a day against a 2,000-minute monthly free allowance
for private repositories. The allowance lasts 3.2 days. The issue records it lasting from 08:44
to 22:00.

Two levers follow, and they are independent of each other:

- **Collapsing the matrix cuts the bill by about a quarter** — 8 billed minutes become 5 — at a
  cost of roughly 3 extra minutes of wall clock per run, paid twice per landing. It needs no
  runner and no visibility change. It does not solve the quota problem: 19 minutes a landing
  still exhausts the allowance in four days.
- **Self-hosting removes the bill entirely.** Self-hosted runners are not billed on private
  repositories. Nothing short of that meets the stated goal.

## Toolchain drift

`daniel-server` is Ubuntu 24.04.5 LTS, which is the same image family as the pinned
`runs-on: ubuntu-24.04`. Each `setup-*` action resolves as follows:

- **`actions/setup-python` at 3.14.7.** The host has a uv-managed Python 3.14.6 and
  `requires-python = ">=3.14"`. Every job reaches Python through `uv run`, which resolves an
  interpreter itself, so the action becomes redundant rather than broken. Drop it and let `uv`
  pin the version.
- **`astral-sh/setup-uv`.** uv is already on the host. The action still works and its
  `enable-cache` writes to a runner-local directory that a self-hosted runner keeps between
  jobs anyway.
- **`actions/setup-node` at 24.21.0.** The host has no Node. The action downloads it into the
  runner's tool cache on first use and reuses it after that, so this self-resolves. It matters
  only for `renovate-config`, which carries `if: github.event_name != 'push'` and so runs on
  pull requests alone.
- **`actions/checkout` with `fetch-depth: 0`.** A self-hosted runner reuses its workspace, so
  every clone after the first is an incremental fetch. The measurements above used a warm
  worktree, which matches.
- **Go, for the `gitleaks` prek hook.** The hook is `language = "golang"` and the hosted image
  ships Go. `daniel-server` does not, so prek fetches a managed toolchain of about 270 MB and
  builds gitleaks from source — 220s, once, for a scan that takes 0.05s. A runner running as
  its own user pays that once against its own `~/.cache/prek`. `systemd-analyze`, `shellcheck`
  and `vale` are all already present.

### What the pinning test needs

`ansible/tests/repo/test_workflow_runners_are_pinned.py` rejects both self-hosted spellings:

- `_VERSIONED = re.compile(r"^[a-z]+-\d+(\.\d+)?(-[a-z]+)?$")` does not match the bare
  `self-hosted` label, which carries no version.
- `_RUNS_ON = re.compile(r"^\s*runs-on:\s*(.+?)\s*$")` captures the list form
  `[self-hosted, linux, x64]` as one string, which matches nothing either.

The edit is an explicit allowlist of self-hosted labels beside the versioned-image regex, not a
loosening of the regex. The test exists because GitHub rolls `ubuntu-latest` to a new image
with no commit in this repository, moving the toolchain the `language: system` hooks run under
a SHA already merged. That reasoning does not apply to a host this repository provisions
itself: the Ansible role is the pin, and it moves through a PR like any other role.

## Memory

A runner job on `daniel-server` lands inside a cgroup, and which one decides who pays for it.

The proposed shape — one `pytest` job beside `hooks` and `ansible-lint` — peaks at 1,641 MiB.
The current 4-shard shape peaks at 2,539 MiB. One shard alone peaks at 546 MiB.

`host_vars/daniel-server.yml` sets `claude_code_rc_memory_high: 8G` and
`claude_code_fleet_memory_high: 10G`, and its `# DECIDED:` block records that with the remote-control unit
disabled, `user-1000.slice` is the only plane and the 8G login cap is the effective bound.

**The runner gets its own slice, as a system unit under `system.slice`.** Left in
`user-1000.slice` it takes 1.6 GiB — a fifth of the login cap — from the fan-out agents, and
throttles whichever cgroup loses the reclaim race.

That placement answers the `fanout_place.py` question as well, in the direction that needs
work. `fanout_place.py read` scores a host on `memory.current` against `MemoryHigh` for
`user.slice` and `user-1000.slice` only, so a runner in `system.slice` is invisible to it. No
code change is needed there; the fleet budget absorbs the runner instead, and the derivation in
the `# DECIDED:` block has to be redone:

| Term | Today | With a runner |
|---|---|---|
| Footprint that cannot be reclaimed, measured | 10.3 GiB | 11.9 GiB |
| Budgeted at +40% | 14.4 GiB | 16.7 GiB |
| Available to the fleet | 13.1 GiB | 10.8 GiB |
| Unspent beside the 10G cap | ~3 GiB | ~0.8 GiB |

The 10G cap still fits. The margin that keeps the homelab plane from losing the reclaim race
falls from about 3 GiB to about 0.8 GiB, which is the cost to weigh — not the cap itself.

## Security

This is the finding that decides the verdict.

**The repository is public as of 2026-09-22.** A self-hosted runner on a public repository
executes workflows from fork pull requests. GitHub's documentation states plainly that you
should not do this. `daniel-server` holds the age key that decrypts `ansible/vars/secrets.yml`,
a `gh` token with `repo` and `workflow` scopes, and a node of the production cluster, so
arbitrary code there is a total compromise of the homelab.

The issue offers a second branch: keep `pull_request` jobs hosted and run only `push` and
`merge_group` jobs self-hosted. That branch does not work, for a reason the arithmetic above
makes concrete. Pull-request runs are about half the bill, so the split halves the burn rather
than removing it — and on a public repository there is no bill to halve in the first place.

The two branches therefore collapse into one configuration: **private repository, every job
self-hosted.** Private is also what removes the fork-execution risk, since only collaborators
can open a pull request. Any other combination is either unsafe or pointless.

## Shape, if the answer becomes go

A setup role, `github_runner`, applied by `initial_setup.yml`, in preference to a container on
`daniel-pi` — the Pi is a Zero 2 W with 512 MB of RAM and cannot hold a 1.6 GiB job at all, and
`daniel-box` is the control plane, Traefik edge and Authelia host, where running a fork's code is
worse than on `daniel-server`.

The role installs the runner package, a systemd unit in its own slice, and registers against a
repository-level token minted from `POST /repos/{owner}/{repo}/actions/runners/registration-token`.
GitHub documents that endpoint as needing the `repo` scope for a repository runner, which the
deployed `gh` token carries. **This was not verified by minting one:** a hook denies mutating
`gh api` calls from a session, so the scope claim rests on GitHub's documentation rather than on
an observation here. Mint one by hand before committing to the role.

Renovate tracks the runner package through a custom manager on the `github-releases` datasource,
the pattern eleven pins in `renovate.json` already use. It stays `automerge: false`: a bump
moves nothing until the role applies it, so the PR is the work order.

## What would settle this

The operator decides whether the repository goes private again. Nothing else in this document
is blocking:

- **Private, and the cost matters** — the follow-up role issue is worth opening. Accept 1.4x
  the CI wall clock on an idle host and 2.8x on a landing day, a collapsed `pytest` matrix, a
  runner in its own slice, and a fleet-cap margin of about 0.8 GiB.
- **Public stays** — close this. A runner costs security and buys nothing.

The matrix-collapse lever is worth considering on its own merits either way, and is a separate
question from the runner: it trades about 3 minutes of wall clock per run for about a quarter of
the billed minutes, and it is the only change here that is a one-line edit.
