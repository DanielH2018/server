# ADRs, Style Enforcement, and D2 — Implementation Plan (2 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an architecture-decision record set linked bidirectionally to the `# DECIDED:` markers already in the code, a scoped Vale style gate, and D2 for hand-authored diagrams.

**Architecture:** ADRs live in `docs/adr/` as MADR-lite documents with a `governs:` frontmatter list of `file:line` anchors. A `# DECIDED:` marker whose reasoning outgrew its line gains an `ADR-NNNN` reference, and `ansible/tests/test_adr_links.py` enforces both directions. Vale runs as a prek hook scoped to the new trees only. D2 renders committed `.d2` sources to SVG at commit time.

**Tech Stack:** Vale + the Google style package, D2, Python 3.14 + uv, pytest, prek.

**Spec:** `docs/docs-ui-and-adrs-design.md`

**Prerequisite:** `docs/docs-ui-and-adrs-plan-1.md` must be merged. This plan adds pages to a site that plan 1 builds and serves.

## Global Constraints

- **Python is 3.14 via uv.** Every command is `uv run …`, never bare `python3`/`pytest`.
- **Commits are signed and hooks run.** Never `--no-verify`, `--no-gpg-sign`, or `core.hooksPath=/dev/null`.
- **The `# DECIDED:` marker convention is defined in the repo-root `CLAUDE.md`** under *Review & Memory Hygiene*, and `.claude/skills/homelab-review/SKILL.md` step 3 greps for it. Read both before changing anything about markers.
- **An ADR is never deleted.** A reversed decision gets a new ADR and the old one's Status becomes `Superseded by ADR-NNNN`. The record of a decision that turned out wrong is worth more than the record of one that did not.
- **Vale is scoped, never tree-wide.** The existing 19 documents and 87 role `CLAUDE.md` files are grandfathered. A lint that lands red on day one gets switched off.
- **New pages go in the `mkdocs.yml` nav**, or `scripts/test_mkdocs_config.py` fails.

---

### Task 1: The ADR format, index, and the first record

The first ADR documents this docs system itself. Writing the format by using it is what catches a template nobody can actually fill in.

**Files:**
- Create: `docs/adr/index.md`
- Create: `docs/adr/template.md`
- Create: `docs/adr/0001-mkdocs-site-with-generated-reference.md`
- Modify: `mkdocs.yml` (add the `Decisions` nav section)

**Interfaces:**
- Consumes: nothing.
- Produces: the ADR frontmatter schema — `id`, `title`, `status`, `date`, `governs`. Task 2's test parses it, so the key names are fixed here.

- [ ] **Step 1: Write `docs/adr/template.md`**

```markdown
---
id: NNNN
title: Short present-tense statement of the decision
status: Accepted
date: YYYY-MM-DD
governs: []
---

# ADR-NNNN: Short present-tense statement of the decision

## Status

Accepted.

## Context

What forced a decision. The constraints that were real at the time, the options that
existed, and what was not known. Write this so someone who arrives two years later
understands why the obvious choice was not taken.

## Decision

What was decided, in the present tense. One paragraph.

## Consequences

What this costs, what it rules out, and what breaks if it is reversed. An ADR whose
consequences section is empty is a decision nobody stress-tested.

## Governs

Where this decision is enforced in the tree. Each entry is a `file:line` anchor that
carries a `# DECIDED: … ADR-NNNN` marker pointing back here. The `governs:` frontmatter
list must match; `ansible/tests/test_adr_links.py` checks both directions.
```

**The `governs:` list may be empty.** Some decisions have no single line that enforces them — the choice of MkDocs over a flat index is one. An empty list is valid; a wrong one is not.

- [ ] **Step 2: Write the first ADR**

Create `docs/adr/0001-mkdocs-site-with-generated-reference.md`, filling the template from `docs/docs-ui-and-adrs-design.md`. Its Context covers the three problems the design opens with: no assembled view of the tree, hand-maintained facts drifting, and decisions scattered across four registries. Its Consequences cover the accepted trade-offs — the single-node pin, the commit noise, and the backfill cost.

`governs: []` — this decision is enforced by the existence of the site, not by a line.

- [ ] **Step 3: Write `docs/adr/index.md`**

A table of every ADR: number, title, status, date. Plus a short section stating the rules, because an index that only lists records does not tell a newcomer when to write one:

```markdown
# Architecture decisions

Records of decisions that shaped this homelab, and why. A decision that is still in force
lives here; the current *state* it produced lives in the reference and runbook pages.

## When to write one

Write an ADR when the reasoning behind a decision outgrows the line it sits on. Short
reasoning stays where it is, as a `# DECIDED:` comment at the code line it governs —
that is what a reviewer trips over before they spend an hour re-deriving it.

An ADR is the long-form why; the marker is the pointer. Where both exist they reference
each other, and `ansible/tests/test_adr_links.py` fails if either direction breaks.

## Superseding

An ADR is never deleted or rewritten to match a reversed decision. Write a new one and
set the old one's status to `Superseded by ADR-NNNN`. The record of a decision that
turned out wrong is worth more than the record of one that did not.
```

- [ ] **Step 4: Wire the nav and build**

Add to `mkdocs.yml`, after the `Reference` section:

```yaml
  - Decisions:
      - Index: adr/index.md
      - ADR-0001 MkDocs site with generated reference: adr/0001-mkdocs-site-with-generated-reference.md
```

Do **not** add `template.md` to the nav — it is a form, not a document. `test_nav_covers_every_toplevel_doc` only checks `docs/*.md`, not subdirectories, so this passes.

Run: `uv run mkdocs build --strict`
Expected: exits 0.

- [ ] **Step 5: Commit**

Stage `docs/adr/` and `mkdocs.yml`. Commit with:

```
Add the ADR format, index and first record

The template was written by filling it in, which is what catches a form
nobody can actually complete.

The index states when to write one, because an index that only lists
records does not tell a newcomer the rule: an ADR exists when the
reasoning outgrows the line it sits on. Short reasoning stays a
'# DECIDED:' marker where a reviewer trips over it.
```

---

### Task 2: `test_adr_links.py` — the check that keeps the two registries joined

Two registries that reference each other by convention drift. This is the executable check that makes the link real, and it must exist **before** the backfill — otherwise the backfill writes 13 links with nothing verifying any of them.

**Files:**
- Create: `ansible/tests/test_adr_links.py`

**Interfaces:**
- Consumes: the ADR frontmatter schema from Task 1.
- Produces: nothing another task imports. Task 3's backfill runs against it.

- [ ] **Step 1: Read the marker convention and survey what exists**

Read the *Review & Memory Hygiene* section of the repo-root `CLAUDE.md`, then:

```bash
grep -rn "# DECIDED:" --include="*.py" --include="*.yml" --include="*.j2" --include="*.sh" . | grep -v '\.git/'
```

There were 36 markers across 20 files as of 2026-08-24. Note the exact marker syntax in use before writing a regex against it.

- [ ] **Step 2: Write the test**

```python
"""ADRs and '# DECIDED:' markers must reference each other, in both directions.

WHY THIS IS A TEST. The repo already had a decision record before ADRs existed: 36
'# DECIDED:' markers at the lines they govern, which .claude/skills/homelab-review/
SKILL.md step 3 greps. An ADR set that referenced them only by convention would be a
second registry drifting from the first -- which is the failure ADRs exist to prevent.

WHAT IS NOT CHECKED. A marker without an ADR is fine and common: an ADR exists only when
the reasoning outgrows the line. This asserts that the links which DO exist resolve, not
that every marker has one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
ADR_DIR = REPO / "docs" / "adr"

ADR_REF = re.compile(r"\bADR-(\d{4})\b")
SEARCH_SUFFIXES = (".py", ".yml", ".yaml", ".j2", ".sh", ".toml", ".md")
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "site", ".claude/worktrees"}


def _adr_files() -> list[Path]:
    return sorted(p for p in ADR_DIR.glob("[0-9]*.md"))


def _frontmatter(path: Path) -> dict:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path.name} has no frontmatter"
    _, block, _ = text.split("---", 2)
    meta = yaml.safe_load(block)
    assert isinstance(meta, dict), f"{path.name} frontmatter is not a mapping"
    return meta


def _tree_files() -> list[Path]:
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in SEARCH_SUFFIXES:
            continue
        rel = path.relative_to(REPO).as_posix()
        if any(skip in rel for skip in SKIP_DIRS):
            continue
        yield path


def test_every_adr_has_wellformed_frontmatter():
    for adr in _adr_files():
        meta = _frontmatter(adr)
        for key in ("id", "title", "status", "date", "governs"):
            assert key in meta, f"{adr.name} frontmatter missing '{key}'"
        assert isinstance(meta["governs"], list), f"{adr.name} governs is not a list"


def test_adr_id_matches_its_filename():
    """A mismatch makes every cross-reference in the tree point at the wrong record."""
    for adr in _adr_files():
        meta = _frontmatter(adr)
        assert f"{int(meta['id']):04d}" == adr.name[:4], (
            f"{adr.name} declares id {meta['id']}"
        )


def test_adr_numbers_are_unique():
    ids = [int(_frontmatter(a)["id"]) for a in _adr_files()]
    assert len(ids) == len(set(ids)), f"duplicate ADR ids: {sorted(ids)}"


def test_every_governs_path_exists():
    """A governs entry pointing at a deleted or moved file is a silently dead link."""
    for adr in _adr_files():
        for entry in _frontmatter(adr)["governs"]:
            path = REPO / str(entry).split(":")[0]
            assert path.is_file(), f"{adr.name} governs missing path: {entry}"


def test_every_adr_reference_in_the_tree_resolves():
    """The other direction: an ADR-NNNN cited anywhere must name a record that exists."""
    known = {f"{int(_frontmatter(a)['id']):04d}" for a in _adr_files()}
    dangling: list[str] = []
    for path in _tree_files():
        if path.parent == ADR_DIR:
            continue
        for match in ADR_REF.finditer(path.read_text(errors="ignore")):
            if match.group(1) not in known:
                dangling.append(f"{path.relative_to(REPO)}: ADR-{match.group(1)}")
    assert not dangling, f"references to ADRs that do not exist: {dangling}"


def test_governs_targets_carry_a_marker_back():
    """Bidirectional, not one-way.

    An ADR claiming to govern a line, where that line says nothing about the ADR, is
    exactly the drift this pair of registries has to avoid: the reviewer reading the
    code never learns the ADR exists.
    """
    broken: list[str] = []
    for adr in _adr_files():
        meta = _frontmatter(adr)
        want = f"ADR-{int(meta['id']):04d}"
        for entry in meta["governs"]:
            path = REPO / str(entry).split(":")[0]
            if not path.is_file():
                continue
            if want not in path.read_text(errors="ignore"):
                broken.append(f"{adr.name} governs {entry}, which never mentions {want}")
    assert not broken, "\n".join(broken)


def test_superseded_adrs_name_their_successor():
    for adr in _adr_files():
        status = str(_frontmatter(adr)["status"])
        if status.lower().startswith("superseded"):
            assert ADR_REF.search(status), (
                f"{adr.name} is superseded but names no successor: {status!r}"
            )
```

- [ ] **Step 3: Run it**

Run: `uv run pytest ansible/tests/test_adr_links.py -v`
Expected: PASS. With only ADR-0001 and an empty `governs`, the link assertions have nothing to check yet — that is correct, and they start biting in Task 3.

- [ ] **Step 4: Check `testpaths` covers it**

`pyproject.toml`'s `[tool.pytest.ini_options] testpaths` defines what `uv run pytest` and the prek hook run. Confirm `ansible/tests` is already listed — it is, since `test_toposort.py` lives there.

Run: `uv run pytest` and confirm the new file appears in the collected set. **A test not in `testpaths` passes locally and never runs in CI**, which is the same as not existing.

- [ ] **Step 5: Commit**

Stage `ansible/tests/test_adr_links.py`. Commit with:

```
Enforce the ADR / '# DECIDED:' link in both directions

The repo already had a decision record before ADRs existed: 36 markers at
the lines they govern, which the homelab-review skill greps. An ADR set
referencing them by convention alone would be a second registry drifting
from the first, which is the failure ADRs exist to prevent.

Both directions matter. An ADR claiming to govern a line where that line
never mentions the ADR leaves the reviewer reading the code unaware it
exists -- so the one-way check would pass while the drift it was written
to catch went on.

This lands before the backfill deliberately. Backfilling first would write
13 sets of links with nothing verifying any of them.
```

---

### Task 3: Backfill the existing decisions

The largest single piece of work in either plan, and where the value is. Twelve to fourteen records harvested from `docs/archive/`, the existing design documents, and the `# DECIDED:` markers.

**Write one ADR per commit.** They are independent, the sources differ, and a single commit adding thirteen records cannot be reviewed.

**Files, per ADR:**
- Create: `docs/adr/NNNN-<kebab-title>.md`
- Modify: the `file:line` each one governs (add the `ADR-NNNN` reference to its marker)
- Modify: `docs/adr/index.md` (add the row)
- Modify: `mkdocs.yml` (add the nav line)

- [ ] **Step 1: Build the source list**

```bash
ls docs/archive/
ls docs/archive/k3s-migration/
grep -rn "# DECIDED:" --include="*.py" --include="*.yml" --include="*.j2" --include="*.sh" . | grep -v '\.git/'
```

Read `docs/archive/README.md` first — it says what each archived document was and whether it was executed.

- [ ] **Step 2: Write the ADRs, one commit each**

The target set, in a sensible reading order. Each names its primary source:

| ADR | Subject | Source |
|---|---|---|
| 0002 | k3s over Docker Compose for the two cluster nodes | `docs/archive/k3s-migration/` |
| 0003 | SOPS with age for secrets at rest | `ansible/.sops.yaml`, `docs/secret-rotation.md` |
| 0004 | Authelia as the single sign-on layer | `ansible/roles/k8s/authelia/CLAUDE.md` |
| 0005 | Traefik as the edge, with IngressRoute CRDs | `ansible/roles/k8s/traefik/` |
| 0006 | Longhorn for cluster storage | `docs/longhorn-disaster-recovery.md` |
| 0007 | Backup tiering: R2 daily, B2 weekly shards | `docs/longhorn-backup-tiering.md` |
| 0008 | 16 MiB Longhorn blocks, and why the field is immutable | `docs/longhorn-backup-tiering.md` |
| 0009 | NetworkPolicy default-deny, and why egress is not enforced | `docs/networkpolicy-default-deny.md`, `docs/archive/networkpolicy-slice*.md` |
| 0010 | Pull-based GitOps over Argo CD and Flux | `docs/gitops-argo-flux-evaluation.md` |
| 0011 | One git-tree lock serializing every deploy path | `scripts/deploy.sh`, `ansible/roles/setup/gitops_deploy/CLAUDE.md` |
| 0012 | Zero-downtime deploys: the rollout gate design | `docs/zero-downtime-deploys-design.md` |
| 0013 | daniel-pi stays on Docker | repo-root `CLAUDE.md`, `ansible/roles/containers/` |
| 0014 | Kopia retired; Longhorn owns the B2 credentials | `docs/kopia-disaster-recovery.md` |

For each: fill the template, then **find the `file:line` it governs and add the `ADR-NNNN` reference to that marker**. A marker that currently reads:

```
# DECIDED: 8 chars, not 12 — minimum-not-width, and the assert fires before the scale-down.
```

becomes:

```
# DECIDED (ADR-0011): 8 chars, not 12 — minimum-not-width, and the assert fires before
# the scale-down.
```

Then list that `file:line` in the ADR's `governs:`. `test_adr_links.py` fails if either half is missing, which is the point.

**Where no marker exists**, leave `governs: []` rather than inventing an anchor. Adding a marker to a line that never had one is a separate change, and it belongs in the commit that has a reason for it.

**Where the source document contradicts current state**, the ADR records what was decided and a Consequences note saying what changed since. Do not quietly correct history — a superseding ADR is the mechanism for that.

- [ ] **Step 3: Verify after each ADR**

Run: `uv run pytest ansible/tests/test_adr_links.py -v`
Run: `uv run mkdocs build --strict`

Both must pass before the next ADR. A backfill that accumulates thirteen broken links before anyone runs the test is thirteen debugging sessions at once.

- [ ] **Step 4: Cross-link the archive**

In `docs/archive/README.md`, add the ADR number beside each archived document that produced one. The archive stays as the raw material — the ADR is the distilled decision, and a reader who lands in the archive should be pointed forward.

---

### Task 4: Vale, scoped to the new trees

**Files:**
- Create: `.vale.ini`
- Create: `styles/Homelab/Dated.yml`
- Create: `styles/Homelab/NounStack.yml`
- Modify: `prek.toml` (add the hook)
- Modify: `.gitignore` (ignore `styles/Google/`, which `vale sync` downloads)

- [ ] **Step 1: Install Vale and fetch the Google package**

Vale is a Go binary. Install it on this host and note the version, then:

```bash
vale sync
```

- [ ] **Step 2: Write `.vale.ini`**

```ini
StylesPath = styles
MinAlertLevel = error

Packages = Google

# Scoped deliberately. The existing 19 documents in docs/ and the 87 role CLAUDE.md files
# are grandfathered: a tree-wide lint over this corpus lands red on day one and gets
# switched off, which is worse than a narrow gate that holds.
[docs/reference/*.md]
BasedOnStyles = Vale, Google, Homelab

[docs/adr/*.md]
BasedOnStyles = Vale, Google, Homelab

[docs/index.md]
BasedOnStyles = Vale, Google, Homelab
```

- [ ] **Step 3: Write the two custom rules**

Google's package covers present tense (`Google.Will`), passive voice, headings and word choice. These two it does not.

`styles/Homelab/Dated.yml`:

```yaml
extends: existence
message: "'%s' dates the prose — say what is true, or give an absolute date."
level: error
ignorecase: true
tokens:
  - currently
  - as of this writing
  - for now
  - at present
  - in the near future
```

Note the words this deliberately omits. *New*, *now*, *latest* and *existing* all have legitimate literal uses — "the new ReplicaSet", "now that the lock is held" — and a rule that fires on those trains people to ignore it.

`styles/Homelab/NounStack.yml`:

```yaml
extends: existence
message: "Noun stack '%s' — rewrite as a phrase with a verb or a preposition."
level: warning
nonword: true
tokens:
  - '\b([A-Z]?[a-z]+ ){3,}(state|check|status|target|policy|config|setting|mode)\b'
```

`level: warning`, not error: this pattern has false positives, and `MinAlertLevel = error` means warnings show without failing the build.

- [ ] **Step 4: Run it and fix what it finds**

```bash
vale docs/reference docs/adr docs/index.md
```

Fix every error in the hand-written pages. **For an error in a generated page, fix the generator's output template, never the page** — the next cron run overwrites the page.

If a rule fires repeatedly on something genuinely correct, adjust the rule and say why in a comment. Do not add per-file exceptions; an exception list is how a style gate becomes decorative.

- [ ] **Step 5: Add the prek hook**

Add to `prek.toml`, following the shape of the existing local hooks. Restrict `files:` to the same three paths as `.vale.ini` — a hook that invokes Vale on every file relies on `.vale.ini` alone for scoping, and the two then drift.

Run: `uv run prek run --all-files`
Expected: the Vale hook passes.

- [ ] **Step 6: Add it to CI**

`.github/workflows/ci.yml` runs `prek run --all-files`, so the hook runs in CI already — **but only if Vale is on the runner**. Add an install step, pinned to the version from Step 1.

Verify by pushing the branch and reading the run. A hook whose binary is missing may skip silently rather than fail, which reads green while checking nothing.

- [ ] **Step 7: Commit**

```
Add Vale, scoped to the generated docs and ADRs

Google's published style package plus two rules it lacks: a datedness ban
and a noun-stack warning.

Scoped to docs/reference/, docs/adr/ and docs/index.md. The existing 19
documents and 87 role CLAUDE.md files are grandfathered -- a tree-wide lint
over this corpus lands red on day one and gets switched off, which is worse
than a narrow gate that holds.

Dated.yml omits 'new', 'now', 'latest' and 'existing' on purpose. All four
have legitimate literal uses, and a rule that fires on those trains people
to ignore it.
```

---

### Task 5: D2 for hand-authored diagrams

The last slice. Nothing else depends on it, which is why it is last.

**Files:**
- Create: `docs/diagrams/<name>.d2` (source, committed)
- Create: `docs/assets/generated/<name>.svg` (rendered, committed)
- Modify: `prek.toml` (add the render hook)
- Modify: `.github/workflows/ci.yml` (install D2)
- Create: `scripts/render_d2.py`
- Test: `scripts/test_render_d2.py`

- [ ] **Step 1: Install D2 and render one diagram**

Install the D2 binary and note the version. Then write the first real diagram — the backup chain from `docs/longhorn-backup-tiering.md` is a good first subject, because it has containers, two tiers, and edges that mermaid routes badly.

```bash
d2 --theme 0 docs/diagrams/backup-chain.d2 docs/assets/generated/backup-chain.svg
```

Embed it in `docs/longhorn-backup-tiering.md` and check it renders in the built site.

- [ ] **Step 2: Write the render script and its test**

`scripts/render_d2.py` renders every `docs/diagrams/*.d2` to `docs/assets/generated/<stem>.svg`, and takes a `--check` flag that renders to a temp directory and compares, exiting non-zero on a difference.

The `--check` mode is what the hook uses. Assert it in the test:

```python
def test_check_mode_fails_when_the_svg_is_stale(tmp_path):
    """A committed .d2 whose .svg was not re-rendered is the whole failure mode.

    The diagram then shows the previous version while the source shows the new one,
    and a reviewer reading the diff sees the change and believes it shipped.
    """
    ...


def test_check_mode_writes_nothing(tmp_path):
    """--check runs in the hook. A hook that mutates the tree it is checking is a
    hook that makes 'git diff' lie."""
    ...
```

Fill both in against the real script.

- [ ] **Step 3: Add the prek hook**

A `files: ^docs/diagrams/.*\.d2$` hook running `render_d2.py --check`. It fails when a `.d2` changed and its `.svg` did not.

**Do not have the hook re-render in place.** A hook that writes files makes `git diff` show changes the author did not make, and prek reports a confusing "files were modified by this hook" on every commit.

- [ ] **Step 4: Add D2 to CI**

Same as Task 4 step 6: install the pinned D2 version in `.github/workflows/ci.yml`, then verify from a real run that the hook executed rather than skipped.

- [ ] **Step 5: Document the three diagram classes**

Add a short section to `docs/index.md` stating when to use which:

- a diagram whose content comes from the tree → a generator emits SVG
- a diagram someone draws → D2 source in `docs/diagrams/`
- neither → a committed SVG, as an escape hatch

Without this, the next person adds a mermaid block and nothing stops them.

- [ ] **Step 6: Commit, then open the PR**

Run: `uv run prek run --all-files`
Run: `uv run pytest`
Run: `uv run mkdocs build --strict`

Then follow the repo's *After a PR Merges* procedure: record the pre-merge SHA, wait for master CI on the merge commit specifically, `./scripts/gitops_tick.sh`, then deploy from the primary checkout.
