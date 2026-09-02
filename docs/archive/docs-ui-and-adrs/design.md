# Docs UI and ADRs — design

**Status:** approved 2026-08-24, not yet implemented
**Implementation plan:** `docs/docs-ui-and-adrs-plan.md`

A browsable documentation site for this homelab, whose reference pages regenerate from the
Ansible tree, plus an architecture-decision record set that links to the `# DECIDED:` markers
already in the code.

## The problem

Three facts about the current state set the scope.

**Nothing assembles the repo's facts into one readable place.** 59 services are declared across
two inventory files and implemented across 87 roles. Answering "what runs here, on which host,
behind which auth, backed up how" means reading the tree. Two generators already solve parts of
this — `scripts/infra_map/gen_infra_map.py` renders a declared-vs-live topology page, and
`scripts/docs/service_catalog.py` renders a service table — but each emits a standalone HTML file
with no navigation between them, and `service_catalog.py` has no cron, no CI wiring, and no
consumer at all.

**Hand-maintained facts drift.** The repo has been bitten by this often enough that CLAUDE.md
warns about the class by name: the service count carries a "don't hand-maintain a precise number
here" instruction, and the `k8s_dry_run_unsupported` count read "~17" against a real 15 for two
commits.

**Decisions are recorded in four places that do not reference each other.** 36 `# DECIDED:`
markers sit at the code lines they govern. `docs/archive/` holds superseded planning documents.
`docs/*.md` holds design documents. The dated review-ledger memories hold review verdicts. A new
ADR set that ignores these produces a fifth registry and more drift.

## What this builds

An MkDocs Material site served behind Authelia, whose content divides into three layers:

| Layer | Source | Regenerated | Hand-edited |
|---|---|---|---|
| `docs/reference/` | the Ansible tree, parsed statically | by cron, committed | never — a hook denies it |
| `docs/adr/` | written once, amended by supersession | no | yes |
| `docs/*.md`, `docs/archive/` | the existing 19 documents plus archive | no | yes |

## Architecture

### Serving

A new role, `ansible/roles/k8s/docs/`, runs nginx serving a static site from a hostPath on
`daniel-box`, behind Traefik and Authelia.

This is the shape `ansible/roles/k8s/artifacts/` already uses, and the reasons carry over. The
pod holds no repo checkout and no git credential. The build runs on `daniel-box`, which already
has the checkout, `uv`, and the cron infrastructure. The pod serves bytes.

The service entry goes in `containers_list` in `ansible/inventory/host_vars/daniel-box.yml` with
`platform: k8s`, positioned **after `traefik` and `authelia`** — that play has no toposort and
runs in list order, and the route uses the `authelia` middleware.

**Consequence:** the site is pinned to `daniel-box`. That node going down takes the docs with it.
This is the same trade the `artifacts` role makes, and it is accepted for the same reason: the
alternative is getting the repo into a pod.

### Content model — the existing documents do not move

`mkdocs.yml` sets `docs_dir: docs` and builds its navigation over the existing `docs/*.md` files
in place.

Moving them would break every reference to `docs/secret-rotation.md`,
`docs/claude-shell-permissions.md` and their siblings across CLAUDE.md, the role documentation,
and the skills — dozens of links, broken to satisfy a navigation tree. The navigation can point
at files where they are.

New subtrees only:

```
docs/
  index.md              hand-written landing page
  reference/            generated; never hand-edited
  adr/                  ADRs plus an index
  assets/generated/     generator SVG output
  *.md                  the existing 19, untouched
  archive/              untouched
```

### The derived / hand-written line

"Automatically kept up to date" holds only where a machine-readable source exists. Naming the
line is what keeps the claim honest.

**Derived.** Each of these has a source the generators parse statically:

| Fact | Source |
|---|---|
| Service inventory, host, platform | `containers_list` in `host_vars/` |
| Route hostname label, auth posture | the per-role `ingressroute.yml.j2` macro call |
| Backup tier membership | the k3s role's Longhorn tier volume lists |
| Autodeploy eligibility | each k8s role's `k8s_autodeploy` declaration |
| Image pins | role defaults |
| Secret registry — names, tiers, due dates | `ansible/secret_rotation.yml` |
| Cron inventory | `roles/setup/initial_setup/tasks/crons.yml` |
| Topology diagram | inventory plus live cluster state |

**Hand-written.** Runbooks, procedures, trade-offs, and every *why*. A generator cannot produce
these and must not try.

**Static parsing only.** The generators never shell out to `ansible` or `kubectl` for repo facts.
A fresh worktree has no Ansible collections installed, and `ansible/inventory/*.yml` contains
SOPS lookups and other Jinja that does not render outside a real deploy. `service_catalog.py`
already documents and follows this rule; the new emitters inherit it. The topology page is the
one exception, and it degrades to declared-only when the cluster is unreachable — that behaviour
already exists in `gen_infra_map.py`.

**A fact the generator cannot derive prints its reason, never a guess.** `service_catalog.py`
sets this precedent with its "unknown" fields and its FIELD NOTES section.

**Retiring hand-maintained numbers.** Once `docs/reference/services.md` exists, the service count
and the `k8s_dry_run_unsupported` count have a generated home, and the CLAUDE.md instructions to
grep for them point at the generated page instead.

### Diagrams

Three classes of diagram, three mechanisms. Mermaid serves none of them well enough.

**Data-driven diagrams** — the topology map, service relationships. The generators emit
standalone `.svg` into `docs/assets/generated/`, and Markdown embeds them.
`scripts/infra_map/infra_map_render.py:335` already hand-positions `<rect>` and `<polyline>` with
live-status tinting through CSS classes. The refactor changes the envelope from a wrapped HTML
page to a standalone SVG; it does not change the drawing. Styles inline into the `<svg>` element
so the status colours survive embedding.

**Hand-authored diagrams** — a backup chain in a runbook, a request path in an ADR.
[D2](https://d2lang.com) renders these. It has real layout engines, nested containers, and edge
routing, where mermaid's auto-layout produces the diagram it wants rather than the one you drew.
The `.d2` source is committed and the SVG is rendered by a prek hook, so the diagram is
reviewable in a diff.

**Everything else** — a committed SVG or an Excalidraw export. An escape hatch, not a default.

### ADRs and the `# DECIDED:` markers

The repo already has a decision record. 36 `# DECIDED:` markers sit across 20 files at the lines
they govern, and `.claude/skills/homelab-review/SKILL.md` greps for them in step 3 — they are
machine-consumed, and they exist so a reviewer trips over the decision before spending an hour
re-deriving it.

**The relationship: the ADR is the long-form why, the marker is the pointer.**

The link is bidirectional and enforced:

- A marker whose reasoning outgrew its line gains an `ADR-NNNN` reference.
- An ADR carries a `governs:` frontmatter list of `file:line` anchors.
- `ansible/tests/repo/test_adr_links.py` checks both directions — every `ADR-NNNN` cited in the tree
  resolves to an ADR that exists, and every `governs:` path resolves to a real file.

**Not every marker needs an ADR.** The rule: an ADR exists when the reasoning outgrows the line
it sits on. A marker that fully explains itself in two lines stays a marker.

**Format** is MADR-lite — Status, Context, Decision, Consequences — plus the `governs:`
frontmatter. Numbered `docs/adr/NNNN-kebab-title.md`. A superseded ADR is never deleted; its
Status becomes `Superseded by ADR-NNNN`.

**Backfill scope,** roughly 10 to 14 records, harvested from `docs/archive/` and the existing
design documents:

- the Docker to k3s migration
- NetworkPolicy default-deny, and why egress is not enforced
- the R2 daily / B2 weekly backup tier split
- zero-downtime deploys
- GitOps pull, versus Argo and Flux — `docs/gitops-argo-flux-evaluation.md` is already an ADR
  wearing a different name
- SOPS plus age for secrets
- Authelia for SSO
- the deploy lock and its exit codes
- Longhorn 16 MiB blocks
- this design itself

**What happens to the other registries.** `docs/archive/` stays as historical record and becomes
the raw material — ADRs cite it rather than replacing it. The dated review-ledger memories stay
in memory; they record review state, not architecture.

### Freshness

A cron on `daniel-box` runs the generators, builds the site, commits the changed Markdown and
SVG, and pushes. This is the shape `secret-rotate.sh` already uses.

**It takes `/var/lock/server-git-tree.lock`.** That is the same lock `gitops-deploy.service` and
`deploy.sh` hold, so the docs cron cannot interleave with a GitOps tick, a deploy, or another
session's work on the tree.

**It does not pull.** The GitOps tick already fast-forwards this checkout every 30 minutes, and
it CI-gates the range first. A pull here would take the lock correctly and still skip that gate,
fast-forwarding onto a commit the tick would have refused. The docs cron reads whatever the tick
has already admitted.

**Freshness is two signals, not one, because the two pull against each other.** A stamp
regenerated every run proves the cron is alive; a stamp that changes only with content keeps
diffs meaningful. Left as one field the first wins, every run rewrites every page, and the cron
commits roughly 730 times a year for no content change — which would spend the commit noise this
design accepted in exchange for reviewable diffs, and get nothing back.

| Signal | Where it lives | What it means |
|---|---|---|
| `generated_at`, `generated_sha` | committed frontmatter | when the page's **content** last changed |
| build time | the served site only, never committed | when the cron last ran |

A generator writes a page only when the body below the frontmatter differs. The build stamp goes
into the built site as `build-info.json` and is never committed.

Both are needed. A page that has not changed in three months is fine; a site that has not built
in three months is not. This keeps the property `crons.yml` already states for the infra-map
cron — a failed run leaves the previous page in place rather than corrupting anything — without
paying for it in commits.

**The build is atomic.** `mkdocs build` cleans its `--site-dir` first, so building straight into
the served path would empty the docs pod's tree and refill it over several seconds, twice a day.
The build goes to a sibling directory and is renamed into place. A failed build leaves the
previous site serving.

**A prek hook denies hand-edits to `docs/reference/**`,** in the shape of the existing
`block-protected-edits` hook. The generated pages also carry a "generated file, do not edit"
banner, but the hook is what enforces it.

**Consequence for `homelab-docs-freshness-reviewer`:** its remit shrinks to the hand-written
subset. A generated page cannot drift from its source. Only prose can.

### Style enforcement

[Vale](https://vale.sh) with Google's published style package, run as a prek hook and in CI.

**Scoped, not tree-wide.** `.vale.ini` covers `docs/adr/` and `docs/index.md`. The existing 19
documents and 87 role `CLAUDE.md` files are grandfathered. A tree-wide lint on this corpus lands
red on day one and gets switched off, which is worse than a narrow one that holds.

**`docs/reference/` is excluded, and the reason is mechanical rather than editorial.** The
refresh cron stages exactly those paths and commits with hooks running, so a Vale error on
generated content would abort that commit, alert, and exit non-zero on every run until someone
fixed the generator — a style rule wedging the pipeline that keeps the docs current. A generated
page's prose lives in its render function, which is reviewed as code and covered by that
generator's tests. Running Vale over the generated output by hand is still worthwhile; making it
a gate is not.

Google's package already covers most of the rules CLAUDE.md states — `Google.Will` is the
present-tense rule, and passive voice, headings, and word choice are covered. A small custom
style adds what it misses:

- the datedness ban on *currently*, *now*, *new*, *soon*, *latest*, *as of this writing*
- the noun-stack limit of three words

The one-idea-per-sentence and claim-before-qualification rules stay human-reviewed. No linter
checks them reliably, and a bad approximation of them produces noise.

## Testing

- **Generator emitters** — pytest per generator, following the pattern in
  `scripts/docs/test_service_catalog.py` and `scripts/infra_map/test_gen_infra_map.py`. Assert the Markdown and
  SVG output against fixtures.
- **`ansible/tests/repo/test_adr_links.py`** — the bidirectional ADR ↔ marker check.
- **Manifest validation** — `scripts/validate/validate_k8s_manifests.py` covers the new role's templates
  automatically, since it renders every `*.j2` under `roles/k8s/*/templates/`.
- **Vale** — prek hook plus CI.
- **`--dry-run`** — the docs role mutates nothing outside `roles/k8s/manifests`, so it must not
  need an entry in `k8s_dry_run_unsupported`. `ansible/tests/deploy/test_k8s_dry_run.py` re-derives that
  list from the role sources, so a mistake here fails a test rather than drifting.

## Risks and accepted trade-offs

**The docs cron commits to master.** Each run that changes output adds a CI run on master.
Docs-only changes are already skipped by the deployer, so no deploy fires — but the GitOps tick
still has to fast-forward the commit. The lock is what keeps this orderly.

**New toolchain dependencies.** MkDocs Material and Vale join the `dev` dependency group in
`pyproject.toml`. D2 is a Go binary and needs installing on `daniel-box` and in CI. D2 arrives in
the last slice, so nothing earlier blocks on it.

**The site is pinned to one node.** Stated above under Serving, and accepted.

**The backfill is the largest single piece of work.** 10 to 14 ADRs written from archive
material is most of the effort in this design. It is also where the value is: going-forward-only
ADRs would leave every existing decision scattered across four registries, which is the problem
being solved.

## Implementation slices

Vertical, each one exercisable in a browser rather than a layer that waits on the next.

1. **One page, visible.** The `docs` role, `mkdocs.yml`, navigation over the existing documents,
   built by hand. Open it behind Authelia. Nothing generated yet.
2. **First generated page and SVG.** `service_catalog.py` emits Markdown; `gen_infra_map.py`
   emits standalone SVG. Two real reference pages, still built by hand.
3. **Cron and freshness.** Generate, build, commit, push, under the lock. The provenance footer.
   The hand-edit hook.
4. **ADRs.** Template, index, `test_adr_links.py`, then the backfill.
5. **Vale.** Scoped configuration, prek hook, CI.
6. **The rest of `reference/`.** Hosts, the secret registry, crons, networking.
7. **D2.** Toolchain, render hook, and the first hand-authored diagram.
