---
id: "0001"
title: A MkDocs site whose reference pages are generated from the Ansible tree
status: Accepted
date: 2026-08-24
governs: []
---

# ADR-0001: A MkDocs site whose reference pages are generated from the Ansible tree

## Status

Accepted.

## Context

Three facts about the repo set the scope.

**Nothing assembled the repo's facts into one readable place.** Around 60 services are
declared across two inventory files and implemented across 87 roles. Answering "what runs
here, on which host, behind which auth, backed up how" meant reading the tree. Two
generators already solved parts of it — `scripts/gen_infra_map.py` rendered a
declared-versus-live topology page and `scripts/service_catalog.py` rendered a service
table — but each emitted a standalone HTML file with no navigation between them, and
`service_catalog.py` had no cron, no CI wiring and no consumer at all.

**Hand-maintained facts drift.** The repo had been bitten often enough that `CLAUDE.md`
warned about the class by name: the service count carries a "don't hand-maintain a precise
number here" instruction, and the `k8s_dry_run_unsupported` count read "~17" against a real
15 for two commits.

**Decisions were recorded in four places that did not reference each other.** 36
`# DECIDED:` markers sat at the lines they governed, `docs/archive/` held superseded
planning documents, `docs/*.md` held design documents, and the dated review-ledger memories
held review verdicts. Any new record set that ignored these would be a fifth registry.

The alternative considered and rejected was a flat generated index — one HTML page, no
navigation, no search. It is cheaper and it fails the same way the two existing generators
did: a page nobody can navigate from is a page nobody opens twice.

## Decision

An MkDocs Material site, served by nginx from a hostPath on `daniel-box` behind Traefik and
Authelia, whose content divides into three layers: `docs/reference/` generated from the
Ansible tree by cron and never hand-edited, `docs/adr/` written once and amended only by
supersession, and the existing hand-written documents left where they are.

The existing 19 documents do not move. The navigation names them in place, because
relocating them would break every reference to `docs/secret-rotation.md` and its siblings
across `CLAUDE.md`, the role documentation and the skills.

## Consequences

**The site is pinned to `daniel-box`.** A hostPath is node-local, so that node going down
takes the docs with it. This is the trade the `artifacts` role already makes, accepted for
the same reason: the alternative is getting a repo checkout and a git credential into a pod.

**Freshness needs two signals, not one.** A page's `generated_at` frontmatter records when
that page's content last changed; the served `build-info.json` records when the cron last
ran and is never committed. Collapsing them into one field would rewrite every page on every
run and produce roughly 730 commits a year for no content change.

**Generated pages are hostile to any hook that rewrites files.** `end-of-file-fixer` rewrote
both the Markdown and the SVG on the first attempt, which would have aborted the cron's
commit on every run. Every generator now emits canonical output, and Vale is scoped to
exclude `docs/reference/` for the same reason.

**A hostPath bind mount follows the inode, not the path.** The first implementation swapped
a freshly built directory over the served one; the pod stayed mounted on the old inode,
which the cleanup then deleted, and nginx answered 403 until it was restarted. The site is
now synced in place.

**The backfill is a real cost.** Recording decisions that were made before this ADR set
existed means reading `docs/archive/`, the design documents and the markers, and writing
each record by hand.

## Governs

Nothing by a single line. This decision is enforced by the existence of the site and its
generators rather than by a trade-off at one place in the code, so `governs:` is empty.
