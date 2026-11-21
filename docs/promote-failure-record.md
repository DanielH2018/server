# promote failure record: 2026-11-06 to 2026-11-20

This page classifies every failed run of the `promote` workflow (`.github/workflows/promote.yml`,
workflow id 382614144) created from 2026-11-06 to 2026-11-20. It names the step each run failed
in and the cause the run's log gives. It answers
[#9624](https://github.com/DanielH2018/server/issues/9624). A cancelled run carries no verdict
(`docs/landing.md`), so this page does not count one.

## How the runs were read

The list of runs comes from the workflow runs API, with `status=failure` and
`created=2026-11-06..2026-11-20`. The query returns 40 runs. The issue counted 38 because it was
filed at 20:32 UTC on 2026-11-20, and two runs failed after that, at 22:07 and 22:52. Of the 40
runs, 34 are `workflow_run` and 6 are `workflow_dispatch`. The issue's own split was 31 and 7;
this page cannot reproduce that split.

The failing job and step come from each run's jobs API. The cause comes from
`ghread run failures <run>`, or from the step's log where that prints no error line. A `Verify`
step ends with a `verdict=failed failed=<check>` line, and the check named there is the cause.
Every `Verify` log also prints `digest-mismatch: error`. That line is an input of
`actions/upload-artifact`, not a failure.

## Failures by step

| Job / step | Runs |
|---|---|
| `promote` / Decide | 16 |
| `verify` / Verify | 9 |
| `render` / Build the bundle | 3 |
| `render` / Run ./.github/actions/setup | 2 |
| `publish` / Push and sign the bundle | 2 |
| `publish` / Run `sigstore/cosign-installer` | 1 |
| `tag` / Run fluxcd/flux2/action | 1 |
| `images` / Push each image whose content tag GHCR does not serve | 1 |
| `render` / Check the bundle | 1 |
| `promote` / Log in to GHCR read-only | 1 |
| `refuse-starter` / Refuse a start by an account off the list | 1 |
| no failed job | 2 |
| **Total** | **40** |

## Failures by cause

| Cause | Runs | Disposition |
|---|---|---|
| `Decide` reads a newer bundle that another run has pushed but not yet signed | 16 | Filed as [#9753](https://github.com/DanielH2018/server/issues/9753). |
| `Verify` reported a failed check | 8 | Expected: the verify gate did its job. No single check failed more than three times. |
| The `actionlint` binary download from GitHub releases failed | 4 | Fixed by [b5d6e5eca](https://github.com/DanielH2018/server/commit/b5d6e5eca) on 2026-11-08. |
| GitHub, GHCR or Sigstore failed for one request | 6 | Transient: six different steps, one run each. |
| GHCR denied the signature push | 1 | One run; no later recurrence. |
| 3005a0e7's `promote.yml` asked uv to skip a `lint` group that the checked-out commit lacks | 2 | Self-clearing: only runs triggered by a commit older than 3005a0e7 could hit it. |
| A dispatch by an account off the allow list | 1 | Expected: the guard refused the start. |
| GitHub ended the run failed with no job failed | 2 | Not established. |

### `Decide` and the unsigned newer bundle (16)

Each of these runs fails in `Decide` with `cosign verify sha256:… exited 10: Error: no signatures
found`. In all 16, another promote run was inside its `Push and sign the bundle` step while
`Decide` ran. `flux push` creates the `sha-<commit>` tag before cosign signs the digest.
`promote.py decide` takes the newest tagged commit with a passing CI verdict and verifies its
signature, and an unsigned tag makes that verification raise. No `verify_drill` push ran near any
of the 16.

The failed run promotes nothing, and the concurrent run promotes the newer commit. In 12 of the
16 cases the concurrent run tagged `prod` itself. In the other 3 the concurrent run was
cancelled, and a later run promoted. The issue for the fix is
[#9753](https://github.com/DanielH2018/server/issues/9753).

### `Verify` verdicts (8)

| Check named by `failed=` | Runs |
|---|---|
| `soak` | 3 |
| `slskd` | 1 |
| `navidrome` | 1 |
| `crowdsec-verify` | 1 |
| `homelab` | 1 |
| `speedtest` | 1 |

A `Verify` failure is the gate's designed verdict: the moved `prod` did not come up healthy, and
`reconcile` acts on the status it records. These runs are not pipeline faults. A check that fails
more than three times in a later window is a workload issue for that role.

### The `actionlint` download (4)

Three `render` runs and one `verify` run failed while uv built `actionlint-py`, whose build
downloads the `actionlint` binary from a GitHub release. The download timed out twice and returned
504 twice, all between 09:05 and 11:00 UTC on 2026-11-08.
[b5d6e5eca](https://github.com/DanielH2018/server/commit/b5d6e5eca) landed at 11:21 that day.
`actionlint-py` was in the `dev` group then, and the commit set `UV_NO_DEV` for every job in
`promote.yml`. [3005a0e7](https://github.com/DanielH2018/server/commit/3005a0e71) moved the lint
tools into a `lint` group at 18:49 and added `UV_NO_GROUP: lint` beside `UV_NO_DEV`. No promote
run failed on that download after 11:21.

### One-off failures (7)

- [42149207603](https://github.com/DanielH2018/server/actions/runs/42149207603): GHCR's login
  endpoint returned `EOF`.
- [42381848496](https://github.com/DanielH2018/server/actions/runs/42381848496): the Sigstore
  transparency log (`rekor.sigstore.dev`) returned 500 twice while cosign signed the bundle.
- [42567486754](https://github.com/DanielH2018/server/actions/runs/42567486754) and
  [42568756378](https://github.com/DanielH2018/server/actions/runs/42568756378): GitHub releases
  returned 504 for the cosign and flux downloads.
  [d7b5a6f5d](https://github.com/DanielH2018/server/commit/d7b5a6f5d) and
  [2934ae023](https://github.com/DanielH2018/server/commit/2934ae023) cached both downloads on
  2026-11-09.
- [43275583271](https://github.com/DanielH2018/server/actions/runs/43275583271): the GHCR read of
  `prod` timed out in `Check the bundle`.
- [44033822941](https://github.com/DanielH2018/server/actions/runs/44033822941): a GHCR read of an
  image tag timed out in `images`.
- [42710237024](https://github.com/DanielH2018/server/actions/runs/42710237024): GHCR answered
  the signature push with `DENIED: permission_denied: write_package`, although the bundle push
  before it succeeded with the same token.

### The `lint` group at 3005a0e7 (2)

`uv sync` in the setup action stopped with ``Group `lint` is not defined in the project's
`dependency-groups` table``. A `workflow_run` run reads its workflow file from master's tip, and
the tip was [3005a0e7](https://github.com/DanielH2018/server/commit/3005a0e71). That commit added
`UV_NO_GROUP: lint` to `promote.yml` and the `lint` group to `pyproject.toml`. The `render` job
checks out the commit whose CI triggered the run, though. Run 42513247395 checked out
[37b2cd08](https://github.com/DanielH2018/server/commit/37b2cd08f), and run 42513592265 checked
out [0bddab21](https://github.com/DanielH2018/server/commit/0bddab217), 3005a0e7's parent.
Neither commit's `pyproject.toml` defines a `lint` group. uv refuses to skip a group the project
does not define, so the workflow and the tree it built disagreed. Only a run triggered by a commit
older than 3005a0e7 could fail this way, so the cause cleared itself once those runs ended.

The *Workflow commit* column in the table below is the run's `head_sha`, the commit the workflow
file came from. These two rows show that it can differ from the tree the run built.

### No failed job (2)

- [43094872595](https://github.com/DanielH2018/server/actions/runs/43094872595): the `publish`
  job stayed queued and never received a runner, and GitHub ended the run failed.
- [42670003984](https://github.com/DanielH2018/server/actions/runs/42670003984): every job that
  ran succeeded. GitHub did not create `verdict-status`, `reconcile` or `settle`. The comment on
  `verdict-status` in `promote.yml` describes a run GitHub creates without its `settle`.

## Every run

| Created (UTC) | Run | Event | Workflow commit | Job / step | Cause |
|---|---|---|---|---|---|
| 2026-11-06 02:15 | [42149207603](https://github.com/DanielH2018/server/actions/runs/42149207603) | `workflow_run` | [`8af998dd`](https://github.com/DanielH2018/server/commit/8af998dd7815c542913ba202af67df97e6591aec) | `promote` / Log in to GHCR read-only | GHCR login EOF |
| 2026-11-07 14:29 | [42381848496](https://github.com/DanielH2018/server/actions/runs/42381848496) | `workflow_run` | [`c66fb784`](https://github.com/DanielH2018/server/commit/c66fb7841d793c80c64ffe0b2325e0d5e26a1733) | `publish` / Push and sign the bundle | the Sigstore transparency log (`rekor.sigstore.dev`) returned 500 |
| 2026-11-07 15:30 | [42386659480](https://github.com/DanielH2018/server/actions/runs/42386659480) | `workflow_run` | [`6176e7f6`](https://github.com/DanielH2018/server/commit/6176e7f6a974aaf93b8e2df9854b4ef6dfd82ea5) | `verify` / Verify | Verify verdict: `soak` |
| 2026-11-08 09:05 | [42465704725](https://github.com/DanielH2018/server/actions/runs/42465704725) | `workflow_dispatch` | [`3c9d1c07`](https://github.com/DanielH2018/server/commit/3c9d1c0740ec80ce92493c7747b4143f2cd0c3e5) | `render` / Build the bundle | `actionlint` download, GitHub 504 |
| 2026-11-08 09:35 | [42467818957](https://github.com/DanielH2018/server/actions/runs/42467818957) | `workflow_dispatch` | [`3c9d1c07`](https://github.com/DanielH2018/server/commit/3c9d1c0740ec80ce92493c7747b4143f2cd0c3e5) | `render` / Build the bundle | `actionlint` download, GitHub 504 |
| 2026-11-08 10:55 | [42473501232](https://github.com/DanielH2018/server/actions/runs/42473501232) | `workflow_dispatch` | [`3c9d1c07`](https://github.com/DanielH2018/server/commit/3c9d1c0740ec80ce92493c7747b4143f2cd0c3e5) | `render` / Build the bundle | `actionlint` download timed out |
| 2026-11-08 11:00 | [42473786677](https://github.com/DanielH2018/server/actions/runs/42473786677) | `workflow_run` | [`164e6824`](https://github.com/DanielH2018/server/commit/164e6824330807653ef621f461aba031c1b19e1f) | `verify` / Verify | `actionlint` download timed out |
| 2026-11-08 14:18 | [42489197013](https://github.com/DanielH2018/server/actions/runs/42489197013) | `workflow_run` | [`2a2090cc`](https://github.com/DanielH2018/server/commit/2a2090cc469130cf5c0754dc77c2676dbba6401c) | `verify` / Verify | Verify verdict: `speedtest` |
| 2026-11-08 19:05 | [42513247395](https://github.com/DanielH2018/server/actions/runs/42513247395) | `workflow_run` | [`3005a0e7`](https://github.com/DanielH2018/server/commit/3005a0e715562d5acfc8026c54eb8c3a46a0147c) | `render` / Run ./.github/actions/setup | `lint` group missing from the checked-out commit |
| 2026-11-08 19:09 | [42513592265](https://github.com/DanielH2018/server/actions/runs/42513592265) | `workflow_run` | [`3005a0e7`](https://github.com/DanielH2018/server/commit/3005a0e715562d5acfc8026c54eb8c3a46a0147c) | `render` / Run ./.github/actions/setup | `lint` group missing from the checked-out commit |
| 2026-11-09 02:35 | [42551826199](https://github.com/DanielH2018/server/actions/runs/42551826199) | `workflow_run` | [`55b3b995`](https://github.com/DanielH2018/server/commit/55b3b995dfd07dcf7fa2ba651da99551d9034aad) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-09 05:28 | [42567486754](https://github.com/DanielH2018/server/actions/runs/42567486754) | `workflow_run` | [`7125b3d9`](https://github.com/DanielH2018/server/commit/7125b3d9c470456c2f9409d9847c84679a1d2faa) | `publish` / Run `sigstore/cosign-installer` | GitHub 504 on the cosign download |
| 2026-11-09 05:41 | [42568756378](https://github.com/DanielH2018/server/actions/runs/42568756378) | `workflow_run` | [`bc342bd2`](https://github.com/DanielH2018/server/commit/bc342bd2eda1c443d64307d52f74ab0313eb9cd2) | `tag` / Run fluxcd/flux2/action | GitHub 504 on the flux download |
| 2026-11-09 17:59 | [42670003984](https://github.com/DanielH2018/server/actions/runs/42670003984) | `workflow_run` | [`4f1faa6e`](https://github.com/DanielH2018/server/commit/4f1faa6e2199b0b2ba73c239c0399926511ead14) | `none` / none | no failed job |
| 2026-11-09 21:43 | [42705183856](https://github.com/DanielH2018/server/actions/runs/42705183856) | `workflow_dispatch` | [`d28d4a9a`](https://github.com/DanielH2018/server/commit/d28d4a9adeb33d324b0797c4abe64358f3e8ce27) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-09 22:15 | [42710237024](https://github.com/DanielH2018/server/actions/runs/42710237024) | `workflow_run` | [`f6c1e7e5`](https://github.com/DanielH2018/server/commit/f6c1e7e5a6e44a6b5e3ad398896464c3815a7bea) | `publish` / Push and sign the bundle | GHCR `write_package` denied on the signature push |
| 2026-11-11 20:15 | [43094872595](https://github.com/DanielH2018/server/actions/runs/43094872595) | `workflow_run` | [`a6621055`](https://github.com/DanielH2018/server/commit/a66210554c15cc614875b3180ef55031289af3b4) | `none` / none | no failed job |
| 2026-11-12 01:52 | [43137478709](https://github.com/DanielH2018/server/actions/runs/43137478709) | `workflow_run` | [`7b4eeb6f`](https://github.com/DanielH2018/server/commit/7b4eeb6f2008002010b6ba472df415c2ab911e06) | `verify` / Verify | Verify verdict: `homelab` |
| 2026-11-12 12:21 | [43217198401](https://github.com/DanielH2018/server/actions/runs/43217198401) | `workflow_run` | [`b3f209e7`](https://github.com/DanielH2018/server/commit/b3f209e7c300b60ba9e32fa44c34291a010dd066) | `verify` / Verify | Verify verdict: `crowdsec-verify` |
| 2026-11-12 14:07 | [43233954904](https://github.com/DanielH2018/server/actions/runs/43233954904) | `workflow_run` | [`439ec637`](https://github.com/DanielH2018/server/commit/439ec63716f9c84b562850ddff807eca80af6122) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-12 18:01 | [43275583271](https://github.com/DanielH2018/server/actions/runs/43275583271) | `workflow_run` | [`3febc5ec`](https://github.com/DanielH2018/server/commit/3febc5ec5e29fdc21134219cb2da196c3776c8f5) | `render` / Check the bundle | GHCR read of `prod` timed out |
| 2026-11-12 23:03 | [43325432411](https://github.com/DanielH2018/server/actions/runs/43325432411) | `workflow_dispatch` | [`aafaeb70`](https://github.com/DanielH2018/server/commit/aafaeb709dc7bdf07af6de81a33494503deea7a9) | `verify` / Verify | Verify verdict: `navidrome` |
| 2026-11-13 16:57 | [43467975070](https://github.com/DanielH2018/server/actions/runs/43467975070) | `workflow_run` | [`dbcd9e26`](https://github.com/DanielH2018/server/commit/dbcd9e26af658f53dcfea30f54b91908df1116a0) | `verify` / Verify | Verify verdict: `slskd` |
| 2026-11-13 20:45 | [43504535147](https://github.com/DanielH2018/server/actions/runs/43504535147) | `workflow_dispatch` | [`a0bd22f8`](https://github.com/DanielH2018/server/commit/a0bd22f8e0d8c4e6bb3ef6ba81243bb20700709c) | `refuse-starter` / Refuse a start by an account off the list | dispatch by an account off the list |
| 2026-11-14 01:28 | [43540764631](https://github.com/DanielH2018/server/actions/runs/43540764631) | `workflow_run` | [`ba09a490`](https://github.com/DanielH2018/server/commit/ba09a49093babb0058cb73a293e74e7d463344ab) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-15 20:09 | [43756415162](https://github.com/DanielH2018/server/actions/runs/43756415162) | `workflow_run` | [`b0b6165a`](https://github.com/DanielH2018/server/commit/b0b6165a3b38d8d0200933e20990dbf0fd33d5ff) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-15 21:16 | [43762756700](https://github.com/DanielH2018/server/actions/runs/43762756700) | `workflow_run` | [`9a75cd94`](https://github.com/DanielH2018/server/commit/9a75cd94b071b0b24dbf7535e774e8f495ce3167) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-15 22:40 | [43770563182](https://github.com/DanielH2018/server/actions/runs/43770563182) | `workflow_run` | [`c0f9e729`](https://github.com/DanielH2018/server/commit/c0f9e729b415fe356cf263581eb73bde008709e1) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-17 06:47 | [44033822941](https://github.com/DanielH2018/server/actions/runs/44033822941) | `workflow_run` | [`96e0793b`](https://github.com/DanielH2018/server/commit/96e0793b2ce8c88fbeac41df69bfc7e740752805) | `images` / Push each image whose content tag GHCR does not serve | GHCR read timed out |
| 2026-11-18 21:09 | [44398484665](https://github.com/DanielH2018/server/actions/runs/44398484665) | `workflow_run` | [`bc945e3f`](https://github.com/DanielH2018/server/commit/bc945e3ff9ae17200948005cfcf11a468dc91253) | `verify` / Verify | Verify verdict: `soak` |
| 2026-11-18 23:45 | [44423473239](https://github.com/DanielH2018/server/actions/runs/44423473239) | `workflow_run` | [`e7bf8c51`](https://github.com/DanielH2018/server/commit/e7bf8c51afbd10641f2b5f256cfbadea01d2056e) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-18 23:46 | [44423577507](https://github.com/DanielH2018/server/actions/runs/44423577507) | `workflow_run` | [`f1e19814`](https://github.com/DanielH2018/server/commit/f1e19814ff7bf7341eccfd92ff2372463d348a4d) | `verify` / Verify | Verify verdict: `soak` |
| 2026-11-19 13:01 | [44529379700](https://github.com/DanielH2018/server/actions/runs/44529379700) | `workflow_run` | [`e39644d9`](https://github.com/DanielH2018/server/commit/e39644d9822e04ac32e2660e676df2f93f190b76) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-19 14:22 | [44542945822](https://github.com/DanielH2018/server/actions/runs/44542945822) | `workflow_run` | [`f1da6574`](https://github.com/DanielH2018/server/commit/f1da6574efb834325fcccef73d4a3e29b86fe909) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-19 15:00 | [44549824740](https://github.com/DanielH2018/server/actions/runs/44549824740) | `workflow_run` | [`2c27fb4c`](https://github.com/DanielH2018/server/commit/2c27fb4c4ec820a5d73060f4830ff34983cd5d59) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-19 19:55 | [44603344627](https://github.com/DanielH2018/server/actions/runs/44603344627) | `workflow_run` | [`239a67d7`](https://github.com/DanielH2018/server/commit/239a67d71b00e8cca7fe4cb2e3ed6c57134f7dcc) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-20 01:35 | [44654742971](https://github.com/DanielH2018/server/actions/runs/44654742971) | `workflow_run` | [`8661640a`](https://github.com/DanielH2018/server/commit/8661640ace1abc82e303d27abdc92582c5528e4b) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-20 15:19 | [44762172858](https://github.com/DanielH2018/server/actions/runs/44762172858) | `workflow_run` | [`7336e006`](https://github.com/DanielH2018/server/commit/7336e0068a045b72ba60b56d2857ccf0e547ef43) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-20 22:07 | [44826225369](https://github.com/DanielH2018/server/actions/runs/44826225369) | `workflow_run` | [`3a7c5c53`](https://github.com/DanielH2018/server/commit/3a7c5c53b14955769312c164f68630bd4bee5104) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
| 2026-11-20 22:52 | [44832479084](https://github.com/DanielH2018/server/actions/runs/44832479084) | `workflow_run` | [`d05d68f6`](https://github.com/DanielH2018/server/commit/d05d68f66a3f9b46228140a90637af84c134aa9c) | `promote` / Decide | unsigned newer bundle ([#9753](https://github.com/DanielH2018/server/issues/9753)) |
