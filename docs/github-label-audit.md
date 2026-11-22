# GitHub label audit and migration plan

This page lists every label on `DanielH2018/server` and `DanielH2018/dotfiles`, the code
that reads each one, and where each one belongs. It ends with the migration plan for the
`domain/*` family, which the operator asked to become a GitHub field on 2026-11-22. Nothing
on this page has been applied: each slice waits for the operator's ruling.

## Summary

- `domain/*` moves to a `Domain` **issue field**, single-select. Issue fields became
  generally available on personal-account repositories on 2026-11-11, and this account
  already uses two (`Severity`, `Not Before`). A Projects field is not needed.
- The move adds no GitHub API call per command. `findings.py` already reads every issue
  field in one aliased GraphQL query per 100 issues, and `open` already writes fields in one
  mutation.
- `severity/*`, `kind/*` and the 41 `not-before:*` labels were migrated on 2026-11-18 and
  2026-11-19. Only the label fallback and the labels themselves remain, which
  [#9120][i9120] deletes. The `domain/*` removal joins that slice.
- `escalated` becomes a value derived from the re-observation count, which every row
  already carries.
- `red-green`, `claimed` and the GitHub default labels have no reader and are deleted.
- `claude`, `manual`, `no-vetted-remediation`, `review-leftover`, `refuted`, `accepted`,
  `landing-batch`, `dependencies` and `security` stay labels.

[i9120]: https://github.com/DanielH2018/server/issues/9120

## Method

One GraphQL query per repository read every label with its open and closed issue counts,
its open and closed pull request counts, and the newest issue and pull request carrying it
(3 points of 5000). GitHub returns no time at which a label was applied without a timeline
read per issue. The *Last seen* column below is therefore the creation date of the newest
item that carries the label, a lower bound on its last use. A second query read the issue
fields, issue types and Projects fields both repositories see. Readers come from a grep of
the whole tracked tree of both repositories, tests excluded, and from the filters of the
seven saved views on the *Server Issue Tracking* project, read the same day.

## Inventory: DanielH2018/server

Counts read 2026-11-22. Issue counts are open/closed; PR counts are open/closed or merged.

| Label | Issues | PRs | Last seen | Created by |
|---|---|---|---|---|
| `claude` | 67/4025 | 0/1 | #9949, 2026-11-22 | `findings.py open` |
| `domain/cicd` | 27/2104 | 0 | #9949, 2026-11-22 | `findings.py open --domain` |
| `domain/backup-observability` | 15/620 | 0 | #9941, 2026-11-22 | `findings.py open --domain` |
| `domain/docs` | 4/548 | 0 | #9818, 2026-11-21 | `findings.py open --domain` |
| `domain/container` | 7/254 | 0 | #9882, 2026-11-22 | `findings.py open --domain` |
| `domain/security` | 10/203 | 0 | #9934, 2026-11-22 | `findings.py open --domain` |
| `domain/network` | 3/101 | 0 | #9933, 2026-11-22 | `findings.py open --domain` |
| `domain/home-assistant` | 1/44 | 0 | #9817, 2026-11-21 | `findings.py open --domain` |
| `severity/low` | 20/2390 | 0 | #9145, 2026-11-19 | pre-field `findings.py` |
| `severity/medium` | 3/1254 | 0 | #9126, 2026-11-19 | pre-field `findings.py` |
| `severity/high` | 0/101 | 0 | #8998, 2026-11-18 | pre-field `findings.py` |
| `kind/gap` | 15/1943 | 0 | #9181, 2026-11-19 | pre-type `findings.py` |
| `kind/improvement` | 6/1591 | 0 | #9196, 2026-11-19 | pre-type `findings.py` |
| `kind/addition` | 3/224 | 0 | #8929, 2026-11-15 | pre-type `findings.py` |
| `not-before:<date>` (37 labels) | 10/38 | 0 | #9120, 2026-11-19 | pre-field `findings.py defer` |
| `manual` | 15/151 | 0 | #9934, 2026-11-22 | `findings.py open --manual`, `manual` |
| `no-vetted-remediation` | 3/199 | 0 | #9941, 2026-11-22 | `findings.py open --no-vetted-remediation` |
| `review-leftover` | 0/119 | 0 | #9657, 2026-11-20 | `findings.py open --review-leftover` |
| `accepted` | 0/95 | 0 | #9601, 2026-11-20 | `findings.py close --accepted` |
| `refuted` | 0/42 | 0 | #9027, 2026-11-18 | `findings.py close --refuted` |
| `escalated` | 0/4 | 0 | #5654, 2026-10-18 | `findings.py touch`, third sighting |
| `red-green` | 0/415 | 0 | #9294, 2026-11-19 | retired fan-out red phase |
| `claimed` | 0/0 | 0 | never | retired claim label |
| `landing-batch` | 0 | 0/256 | #9947, 2026-11-22 | `land_lib/batch.py` |
| `dependencies` | 0 | 0/630 | #9862, 2026-11-22 | Renovate |
| `security` | 0 | 0/1 | #5803, 2026-10-21 | Renovate vulnerability PRs |
| 9 GitHub defaults | 0 | 0 | never | repository creation |

The GitHub defaults are `bug`, `documentation`, `duplicate`, `enhancement`,
`good first issue`, `help wanted`, `invalid`, `question` and `wontfix`. Of the 37
`not-before:` labels, 8 sit on no issue at all, and 9 sit on open issues. No `solo-only`
label exists: `findings_lib/solo_only.py` derives solo-only from the paths an issue cites.

## Inventory: DanielH2018/dotfiles

The dotfiles register is filed by the same `findings.py`, run with
`--repo DanielH2018/dotfiles`, so its labels have the same creators. Its only reader of its
own is `renovate.json:10`, which applies `dependencies`.

| Label | Issues | PRs | Last seen |
|---|---|---|---|
| `claude` | 3/321 | 0 | #1475, 2026-11-21 |
| `domain/security` | 1/104 | 0 | #1445, 2026-11-20 |
| `domain/cicd` | 1/50 | 0 | #1457, 2026-11-21 |
| `domain/docs` | 0/19 | 0 | #1448, 2026-11-20 |
| `domain/backup-observability` | 0/2 | 0 | #1452, 2026-11-20 |
| `domain/container` | 0/2 | 0 | #1090, 2026-10-31 |
| `domain/home-assistant`, `domain/network` | 0/0 | 0 | never |
| `severity/low`, `/medium`, `/high` | 0/200, 1/90, 0/2 | 0 | #1407, 2026-11-18 |
| `kind/gap`, `/improvement`, `/addition` | 0/182, 1/100, 0/13 | 0 | #1416, 2026-11-19 |
| `not-before:<date>` (4 labels) | 0/3 | 0 | #1068, 2026-10-29 |
| `manual` | 2/13 | 0 | #1451, 2026-11-20 |
| `no-vetted-remediation` | 0/8 | 0 | #1385, 2026-11-14 |
| `review-leftover` | 0/7 | 0 | #1309, 2026-11-14 |
| `accepted` | 0/21 | 0 | #1385, 2026-11-14 |
| `refuted` | 0/2 | 0 | #1052, 2026-10-25 |
| `claimed` | 0/30 | 0 | #1262, 2026-11-13 |
| `escalated` | 0/0 | 0 | never |
| `dependencies` | 0 | 0/32 | #1401, 2026-11-16 |
| `enhancement`, `bug`, `question` | 0/21, 0/1, 0/1 | 0 | #557, 2026-09-19 |
| 6 other GitHub defaults | 0 | 0 | never |

## What GitHub offers a personal account

Read from GitHub's documentation on 2026-11-22, and checked against this account's live
schema the same day.

- **Issue fields** are generally available on personal-account repositories since
  2026-11-11 ([changelog][cl-fields]). The account owns them, so one definition serves both
  repositories: both already show `Severity` (single-select) and `Not Before` (date).
  Types are single-select, text, number and date; the changelog adds multi-select, which
  the docs page does not list. An owner has at most 25 fields and a single-select field at
  most 100 options ([managing issue fields][d-manage]). Fields apply to issues only, never
  pull requests ([adding issue fields][d-add]). Values are read and written through GraphQL
  (`issueFieldValues`, `setIssueFieldValue`) or REST (`/repos/{o}/{r}/issues/{n}/issue-field-values`,
  [REST reference][d-rest-values]). REST creates field definitions only for organizations
  ([REST reference][d-rest-fields]), so on this account a definition is made in
  Settings > Planning > Issue fields. Issue search filters a value as
  `field.domain:cicd` ([search syntax][d-search]). `gh issue list --json` returns no field
  values, and `gh issue edit` sets none; the docs name no `gh` flag for either.
- **Issue types** are generally available on personal accounts on the same date
  ([managing issue types][d-types]), up to 25 per owner. Both repositories have Task, Bug,
  Feature, Gap and Improvement. `gh issue list --json issueType` returns the type, and
  search filters `type:"Gap"`. A type is one value per issue, which is why it holds
  `kind`, not `domain`.
- **Projects fields** (text, number, date, single-select, iteration) belong to one project,
  at most 50 fields and 50,000 items each ([about projects][d-projects]). An issue has a
  value only once it is an item: `addProjectV2ItemById` must run before
  `updateProjectV2ItemFieldValue`, in two calls ([projects API][d-projects-api]). The API is
  GraphQL only. The docs name no issue-search qualifier for a Projects field. The account's
  one project, *Server Issue Tracking* (3221 items), already shows `Severity` and
  `Not Before` as its own columns, because a project displays the owner's issue fields.
- **Milestones** hold one per issue and are read by `gh issue list --json milestone`. They
  already carry project membership (`findings.py milestone`), so a second use would collide.
- **Sub-issues** (100 per parent, 8 levels, [sub-issues][d-sub]) and **issue dependencies**
  (`gh issue edit --add-blocked-by`, [dependencies][d-deps]) are relationships, not
  attributes. `findings.py block` already records "waits on" as a dependency.

[cl-fields]: https://github.blog/changelog/2026-11-11-issue-types-and-fields-are-available-for-personal-accounts
[d-manage]: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/managing-issue-fields
[d-add]: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-and-managing-issue-fields
[d-rest-values]: https://docs.github.com/en/rest/issues/issue-field-values
[d-rest-fields]: https://docs.github.com/en/rest/orgs/issue-fields
[d-search]: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/filtering-and-searching-issues-and-pull-requests
[d-types]: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/managing-issue-types
[d-projects]: https://docs.github.com/en/issues/planning-and-tracking-with-projects/learning-about-projects/about-projects
[d-projects-api]: https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects
[d-sub]: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues
[d-deps]: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/creating-issue-dependencies

### API cost of a Domain field

| Operation | Label (today) | Issue field | Projects field |
|---|---|---|---|
| Read with `list`, `next`, `claim` | 0 extra: in `gh issue list --json labels` | 0 extra: `attach_fields` already reads every field, 1 point per 100 issues | 1+ GraphQL query over project items, and dotfiles issues are not items |
| Write on `open` | 0 extra: `gh issue create --label` | 0 extra: joins the `setIssueFieldValue` mutation `open` already sends | 2 mutations: add the item, then set the value |
| Filter in search | `label:domain/cicd` | `field.domain:cicd` | none documented |
| Closed issues | keep the label | keep the value | only items keep it |

`findings.py` filters by domain only in `list --domain`, which filters rows client-side
after the load (`scripts/dev/findings.py:668`). `next`, `claim` and fan-out never select by
domain: a batch groups issues by the paths they cite (`issue_model.py:523`). So the field
changes no selection, only where the value is stored.

## Recommendation per label family

### domain/* becomes the Domain issue field

Seven labels on each repository become one single-select issue field named `Domain`, with
one option per entry of `DOMAINS`, named exactly as the slug (`cicd`,
`backup-observability`). Matching names need no translation table, keep `--domain cicd`
and `field.domain:cicd` identical, and keep the generated backlog page unchanged.

- **Reason:** the operator's requirement, and the precedent `Severity` set. One account-level
  definition replaces 14 repository labels.
- **Readers that change:** `scripts/dev/findings_lib/issue_model.py:24`,
  `issue_model.py:75` (the loop that adds the labels), `issue_model.py:839` (the row's
  `domain`); `scripts/dev/findings.py:281` (writes the label on `open`),
  `findings.py:668` and `findings.py:699` (`list`); `scripts/dev/findings_lib/next_cli.py:89`;
  `scripts/dev/findings_lib/gh_calls.py:119` and `gh_calls.py:204` (`set_fields`,
  `fill_missing`); `scripts/docs/reference/backlog.py:102`, `backlog.py:172` and
  `backlog.py:211`; `scripts/docs/fragments_lib/fragments_deploy.py:180` (parses the
  `for _d in DOMAINS` loop and raises without it);
  `docs/assets/generated/fragments/findings-labels.md`; `docs/issue-claiming-and-fanout.md:44`
  and `:55`. `.claude/skills/homelab-review/SKILL.md:121` and `:331` pass `--domain` and need
  no edit, since the flag stays.
- **Lost:** the domain on closed issues the backfill does not reach, once the labels are
  deleted. No tool reads those.

A Projects field is the alternative only if issue fields were unavailable. It would require
every issue to be a project item (board-sync adds open server issues only), double the
writes on `open`, and lose search filtering. I do not recommend it.

### severity/*, kind/*, not-before:* are deleted under #9120

The fields and types already hold these values; the labels remain only as a read fallback
for sessions on a pre-field checkout. [#9120][i9120] deletes the fallback
(`issue_model.py:69`, `:204`, `:215`, `:228`; `plans.py:63`) and the labels. It is deferred
to 2026-12-03. Nothing new is decided here: the `domain/*` removal joins it, so one slice
retires every label fallback.

### escalated becomes derived from the re-observation count

`plans.py:231` adds `escalated` on the third sighting, and `issue_model.py:840` and `:939`
read it back. Every row already carries `reobservations` (`issue_model.py:855`), so
`escalated` is `reobservations >= 2`, computed where the row is built. The threshold is
exact: `plan_touch` counts the filing as the first sighting and adds the label while
posting the second `Re-observed` comment (`plans.py:219`). On 2026-11-22 no open issue had
more than one such comment, so the derivation reorders nothing in `next` today.

- **Readers that change:** `issue_model.py:53`, `:840`, `:939`; `plans.py:219`, `:231`;
  `scripts/dev/findings.py:380`, `:683`; `scripts/dev/findings_lib/cli.py:232`;
  `backlog.py:62`, `:89`.
- **Removes:** one label write per escalation, and a value that could disagree with the
  comments it summarises. **Lost:** the label as a search filter; 4 closed issues carry it.

### red-green, claimed and the GitHub defaults are deleted

- `red-green`: the fan-out red phase that wrote and read it was retired in 35eb56560. No
  code names it; 415 closed issues carry it.
- `claimed`: replaced by the `Claim:` comment and the board's Status (896f23374). It sits
  on 30 closed dotfiles issues and none on server. Two project views, *Open* and
  *Non-Manual*, still filter `-label:claimed`; that term matches every issue, so it is
  stale rather than broken, and the operator removes it when the label goes.
- The nine GitHub defaults on server carry nothing. On dotfiles, `enhancement` (21),
  `bug` (1) and `question` (1) sit on closed issues from September 2026, which no tool reads.
  Mapping `enhancement` to the Feature type first would cost 21 writes for history nobody
  queries; I recommend deleting without it.

### Labels that stay

Each of these is a flag that a gate or a server-side filter reads in the same call that lists
the issues, or a pull request label, which no field can hold.

- `claude` scopes the register. `gh issue list --label claude` is the server-side filter
  under every load (`gh_calls.py:290`, `:385`, `:432`; `export_cli.py:60`), and the trust
  gates refuse an issue without it (`plans.py:246`, `:310`, `:418`; `claim.py:196`;
  `history_cli.py:185`; `scripts/dev/fanout_lib/brief.py:37`). A field cannot narrow a
  `gh issue list` call.
- `manual` withholds an issue from sessions and has its reverse, `manual --clear`
  (`plans.py:157`, `:315`; `claim.py:196`; `scripts/dev/fanout_lib/operator_waits.py:233`;
  `issue_model.py:848`), and the project views *Manual*, *Non-Manual* and *No Milestone*
  filter on it. A one-option single-select field would add a definition and a two-call
  write for a boolean. No project view filters on `domain/*`, `escalated` or the other
  labels this page moves.
- `no-vetted-remediation` has its reverse in `remedy` (`plans.py:198`; `findings.py:342`;
  `.claude/skills/homelab-review/SKILL.md:332`, `:393`).
- `review-leftover` sets a batch's model floor (`scripts/dev/fanout_lib/sizing.py:56`;
  `.claude/skills/issue-fanout/SKILL.md:143`) and ranks last in `next`
  (`issue_model.py:849`).
- `refuted` and `accepted` are close outcomes GitHub records as one `NOT_PLANNED`; the
  settled-register search excludes them by label (`gh_calls.py:321`, `:348`;
  `issue_model.py:804`; `backlog.py:57`).
- `landing-batch` is a pull request label: `.github/workflows/ci.yml:145` gates a job on it
  and `scripts/deploy_tools/land_lib/batch.py:69` applies it.
- `dependencies` and `security` are Renovate's (`renovate.json:9`, `:37` on server,
  `renovate.json:10` on dotfiles), on pull requests only.

## Migration plan

Each slice is one PR the operator can exercise before the next. `domain` comes first.

### Slice 0: the operator defines the Domain field

The operator adds a single-select issue field named `Domain` under Settings > Planning >
Issue fields, with the seven options `backup-observability`, `cicd`, `container`, `docs`,
`network`, `security` and `home-assistant`. REST cannot create a field for a personal
account. GraphQL's `createIssueField` is documented without saying whether a user owner can
call it, so the UI is the dependable path.

- **Exercise:** open any issue; the sidebar shows an empty Domain field.
- **Complexity:** none in the repo.

### Slice 1: backfill Domain from the labels

The script `scripts/dev/fill_domain.py` sets `Domain` on every issue that carries a
`domain/` label and has no Domain value: every open issue, and the closed issues the
backlog page renders (`load_backlog_issues`). A re-run skips filled issues, so it is safe to
repeat. It paces mutations with the `MUTATION_PAUSE_S` board.py uses, under GitHub's
secondary write limit. On 2026-11-22 it found 205 server issues and 22 dotfiles issues.
Nothing reads the field yet; the labels stay authoritative.

- **Exercise:** search `is:issue field.domain:cicd is:open` returns the same 27 server issues
  as `label:domain/cicd is:open`.
- **Complexity:** adds one temporary script and its test; slice 3 deletes both.
- **Option:** backfill all ~4,000 closed issues too, so search history keeps the domain. At
  about 500 writes an hour that is an eight-hour paced run, for values nothing reads.

### Slice 2: open writes the field, and every reader moves to it

One PR. `open --domain` puts `Domain` into the `setIssueFieldValue` mutation it already sends,
and stops adding the label. The row's `domain` reads `issue.fields["Domain"]`, so `list`,
`next`, `show` and the backlog page move with it. The `for _d in DOMAINS` loop leaves
`LABELS`, so `sync-labels` and `open` stop creating the labels, and the fragment generator
stops parsing the loop. `docs/issue-claiming-and-fanout.md` and the generated label table
change in the same PR. After the merge, run `fill_domain.py` once more for issues filed in
between.

- **Why one PR:** writing the field before the readers move leaves new issues blank in every
  reader; moving the readers first leaves them blank the other way. Writing both would make
  two values authoritative.
- **No label fallback:** unlike severity and not-before dates, domain gates nothing. A
  session on a pre-merge checkout files an issue with only the label; its domain reads `-`
  until the slice 3 re-run fills it. #9120 kept a fallback because a missing date lets
  `next` offer a deferred issue; a missing domain blanks a column, and drops the issue from
  the `findings.py list --domain` that `homelab-review` runs to show a reviewer its
  domain's findings (`SKILL.md:121`). Fingerprint matching on `open` still catches an exact
  re-filing, so that gap costs at most a near-duplicate until the re-run.
- **Exercise:** `findings.py open --dry-run --domain network …` prints the field in the
  mutation and no `domain/` label; `findings.py list --domain cicd` lists the same issues
  as before.
- **Complexity:** removes the label loop and the special case in the fragment generator; adds one
  entry to the field write.

### Slice 3: delete the labels, with #9120

Once no live session runs a pre-slice-2 checkout, the same date #9120 waits for:

1. Run `fill_domain.py` a last time on each repository, then delete it and its test.
2. Delete the 14 `domain/*` labels, the 6 `severity/*`, the 6 `kind/*`, the 41
   `not-before:*` and the 2 `escalated` labels across both repositories, and the readers'
   fallbacks.
3. Add a test that fails when `LABELS` names `escalated` or a key under `domain/`,
   `severity/`, `kind/` or `not-before:`, or when a planned `gh` argv adds one.

After the deletion GitHub itself refuses a re-add: `gh issue create --label domain/cicd`
and `gh issue edit --add-label` fail on a label the repository lacks, and `LABELS` is the
only code that creates labels. The test covers that one path.

- **Complexity:** removes three fallbacks, the backfill command and 69 labels.

### Slice 4: derive escalated

`issue_rows` computes `escalated` from `reobservations`; `touch` stops adding the label;
`escalated` leaves `LABELS`. The label itself waits for slice 3: a session on an older
checkout recreates every label its `LABELS` names on each `open`, so deleting it earlier
would not stick.

- **Exercise:** `findings.py list --json` shows `escalated: true` on an issue with two
  `Re-observed` comments and no label.
- **Complexity:** removes one write path.

### Slice 5: delete the unread labels

Delete `red-green`, `claimed` and the nine defaults on server, and `claimed` and
the nine defaults on dotfiles. No code changes; `LABELS` names none of them. The operator
drops `-label:claimed` from the *Open* and *Non-Manual* project views in the same step.

- **Exercise:** each repository's label page lists only the labels this page keeps.
- **Complexity:** removes 21 labels.

## See also

- `docs/issue-claiming-and-fanout.md` describes the register, its fields and its labels.
- `docs/github-quota.md` describes the API budget the backfill runs under.
