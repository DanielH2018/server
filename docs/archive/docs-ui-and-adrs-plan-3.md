# Subsystem Docs — Scripts, GitOps, and the Deploy Pipeline — Implementation Plan (3 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover the parts of the homelab the site does not yet describe — the ~40 first-party scripts, the GitOps deploy pipeline, and the deploy path an operator actually drives.

**Architecture:** Two kinds of page, split on the same line the design draws. A **generated** `reference/scripts.md` enumerates every first-party script from its own module docstring, so the list cannot drift. **Hand-written** operator docs explain the GitOps pipeline and the deploy path, because a decision procedure is explanation and no generator can derive it.

**Tech Stack:** Python 3.14 + uv, pytest, MkDocs Material, prek.

**Spec:** `docs/archive/docs-ui-and-adrs-design.md`

**Prerequisite:** `docs/archive/docs-ui-and-adrs-plan-1.md` must be merged. This plan adds pages and one generator to the site plan 1 builds and serves. It is independent of plan 2 and can land before or after it.

## Global Constraints

- **Python is 3.14 via uv.** Every command is `uv run …`, never bare `python3`/`pytest`.
- **Commits are signed and hooks run.** Never `--no-verify`, `--no-gpg-sign`, or `core.hooksPath=/dev/null`.
- **A generated page is never hand-edited.** `.claude/hooks/block-protected-edits.py` rejects an edit under `docs/reference/`; change the generator instead.
- **A generator parses statically.** `yaml.safe_load` and regex only — never `ansible`, never `kubectl`, never importing the script it documents. A fresh worktree has no collections installed, and importing a script runs its module-level code.
- **Every generated page ends with exactly one newline** and writes through `docs_provenance.write_if_body_changed`, or the refresh cron commits on every run and `end-of-file-fixer` fights it.
- **New pages go in the `mkdocs.yml` nav**, or `scripts/test_mkdocs_config.py` fails.
- **Do not restate what a role's `CLAUDE.md` already says.** Link to it. The `gitops_deploy` role's `CLAUDE.md` is the authority on that pipeline's internals; the new page is the operator's view of it.

---

### Task 1: The generated scripts reference

**Why generated.** There are ~40 first-party scripts and every one carries a module docstring — several of them long, with a `Usage::` block. A hand-written index of them is stale the day someone adds the forty-first. The docstrings are already the documentation; this page assembles them.

**Files:**
- Create: `scripts/docs/gen_reference_scripts.py`
- Create: `scripts/docs/test_gen_reference_scripts.py`
- Modify: `scripts/docs/build_docs.py` — add to `GENERATORS`
- Modify: `mkdocs.yml` — add to the Reference nav section

**Interfaces:**
- Consumes: `docs_provenance.generated_banner`, `docs_provenance.write_if_body_changed` (plan 1).
- Produces: `build_rows(scripts_dir) -> list[dict]` with keys `name`, `summary`, `usage`, `tests`; `render_markdown(rows) -> str`.

**What it reads.** Every `scripts/*.py` and `scripts/*.sh`, excluding `test_*`, `conftest.py`, and `_`-prefixed private modules. For a Python file, parse the module docstring with `ast.parse` + `ast.get_docstring` — **not** by importing it, which would execute module-level code. For a shell script, take the leading `#` comment block.

Extract three fields:
- **summary** — the docstring's first line.
- **usage** — the indented block after a `Usage::` marker, if present.
- **tests** — `scripts/test_<name>.py` if it exists, else the empty string. A script with no test is a fact worth surfacing, not an omission to hide.

**Steps:**

- [ ] **Step 1: Write the failing test** — `scripts/docs/test_gen_reference_scripts.py`, over a synthetic `scripts/` under `tmp_path`:

```python
def test_the_summary_is_the_docstrings_first_line(tmp_path):
    _write(tmp_path / "probe.py", '"""Read-only homelab diagnostics.\n\nMore prose.\n"""\n')
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["probe.py"]["summary"] == "Read-only homelab diagnostics."


def test_a_script_is_never_imported_to_read_its_docstring(tmp_path):
    """Importing runs module-level code; ast.parse does not."""
    _write(tmp_path / "boom.py", '"""Summary."""\nraise SystemExit("imported")\n')
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["boom.py"]["summary"] == "Summary."


def test_test_files_and_private_modules_are_excluded(tmp_path):
    _write(tmp_path / "test_probe.py", '"""x"""\n')
    _write(tmp_path / "_render_guard.py", '"""x"""\n')
    assert g.build_rows(tmp_path) == []


def test_a_script_with_no_test_file_is_reported_as_such(tmp_path):
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    assert g.build_rows(tmp_path)[0]["tests"] == ""


def test_markdown_ends_with_exactly_one_newline(tmp_path):
    _write(tmp_path / "probe.py", '"""Summary."""\n')
    out = g.render_markdown(g.build_rows(tmp_path))
    assert out.endswith("\n") and not out.endswith("\n\n")
```

- [ ] **Step 2: Run it and watch it fail** — `uv run pytest scripts/docs/test_gen_reference_scripts.py -v`. Expected: `ModuleNotFoundError: gen_reference_scripts`.
- [ ] **Step 3: Write the generator.** Model the shape on `scripts/docs/gen_reference_crons.py`: an argparse `--out`, a `build_rows`, a `render_markdown`, and a `main` that routes through `write_if_body_changed`.
- [ ] **Step 4: Run the tests** — expected PASS.
- [ ] **Step 5: Register it** — add the `(argv, output)` pair to `GENERATORS` in `scripts/docs/build_docs.py` and a `Scripts: reference/scripts.md` entry to the `mkdocs.yml` Reference section.
- [ ] **Step 6: Build twice** — `uv run python scripts/docs/build_docs.py --site-dir /tmp/site`, then again. `git status --short` must be clean after the second run, or the refresh cron commits on every tick.
- [ ] **Step 7: Commit.**

---

### Task 2: The GitOps pipeline operator page

**Why hand-written.** The pipeline's *behaviour* is a decision procedure — what makes a change eligible, what defers it, what a held SHA means. No generator derives that. The `gitops_deploy` role's `CLAUDE.md` documents the internals for someone changing the code; this page is for someone watching it run.

**Files:**
- Create: `docs/gitops-pipeline.md`
- Modify: `mkdocs.yml` — add to the Operations nav section

**What it must answer,** in this order — each is a question that has actually cost time:

1. **What the tick does, in sequence.** Fetch, CI-gate, `--ff-only` merge, deploy what is eligible, health-gate, roll back on failure — all under `/var/lock/server-git-tree.lock`.
2. **What is eligible for auto-deploy, and what is not.** An image-pin bump to a non-denylisted service auto-deploys; an ordinary manifest or template change is fast-forwarded and left undeployed. Say plainly that a merge is not a deploy here.
3. **What a broad change does.** A `_BROAD_SETUP_PREFIXES` / `_BROAD_DEPLOY_PREFIXES` path anywhere in the `local..origin` range makes the deployer defer-and-alert and return **without fast-forwarding at all** — so an unrelated docs commit in the same range never lands either. Name the symptom: a tick that exits 0, logs nothing, and writes `behind_since`.
4. **How to read `last_run`, `hold_sha`, and `behind_since`.** A non-empty `hold_sha` means a previous SHA failed its health gate and is held; diagnose before deploying anything.
5. **How to trigger a tick by hand** — `./scripts/deploy_tools/gitops_tick.sh`, and that there is no dry-run mode because it is the real code path.
6. **Why the CI gate reads `check-runs` for the merge commit** rather than `gh run list --branch master --limit 1`, which returns the previous run and reports green instantly.

Link to `ansible/roles/setup/gitops_deploy/CLAUDE.md` for internals rather than restating them.

**Steps:**

- [ ] **Step 1: Read the sources** — `ansible/roles/setup/gitops_deploy/CLAUDE.md`, `files/deploy_logic.py`, `files/gitops_deploy.py`, and the repo-root `CLAUDE.md` section *After a PR Merges*. Every claim on the page comes from one of these, with a `file:line` where it is a specific behaviour.
- [ ] **Step 2: Write the page.** Follow the sentence-level rules in the user `CLAUDE.md`: one idea per sentence, claim before qualification, name the actor, present tense.
- [ ] **Step 3: Add it to the nav** and run `uv run pytest scripts/test_mkdocs_config.py`.
- [ ] **Step 4: Build with `--strict`** — `uv run python scripts/docs/build_docs.py --site-dir /tmp/site`. A broken internal link fails the build.
- [ ] **Step 5: Commit.**

---

### Task 3: The deploy path operator page

**Why separate from task 2.** GitOps is the pull-based automatic path. This is the path an operator drives by hand, and conflating them is what makes people reach for the wrong one.

**Files:**
- Create: `docs/deploying.md`
- Modify: `mkdocs.yml` — add to the Operations nav section

**What it must answer:**

1. **`./scripts/deploy.sh`, not bare `ansible-playbook`.** The wrapper takes the git-tree lock, checks the tags against `containers_list`, and refuses a stale tree. The bare form has none of those.
2. **The exit codes as resume points, not failures** — 75 lock busy, 4 tree behind `origin/master`, 3 change too broad to map to one service, 2 tag matched nothing. Each says *nothing was deployed*.
3. **The three check modes and what each actually sees** — `prek run --all-files` (renders locally, no API server), `--check` (the apply is skipped), `--dry-run` (the live API server, so CRDs and admission too). Reproduce the table from the repo-root `CLAUDE.md` rather than paraphrasing it.
4. **What `--dry-run` cannot cover** — the roles in `k8s_dry_run_unsupported`, and that a brand-new service is only half-checked because `seed-volume` is skipped and nothing at admission proves a referenced PVC is provisionable.
5. **Verifying twice.** `probe.py health <svc>` gates the rollout and the 180s restart window; it cannot see whether your change took effect. Give the two standing counterexamples: an Authelia 302 fires before the backend is reached, and 19 dead Grafana panels sat behind a 1/1 pod.

**Steps:**

- [ ] **Step 1: Read the sources** — `scripts/deploy.sh`, `scripts/deploy_tools/deploy_staleness.py`, `scripts/deploy_tools/deploy_tags.py`, and the repo-root `CLAUDE.md` *Common Commands*.
- [ ] **Step 2: Write the page.**
- [ ] **Step 3: Add it to the nav** and run `uv run pytest scripts/test_mkdocs_config.py`.
- [ ] **Step 4: Build with `--strict`.**
- [ ] **Step 5: Commit.**

---

### Task 4: Point the index at the new sections

**Files:**
- Modify: `docs/index.md`

`index.md` describes Reference, Runbooks and Design. Operations is now a fourth section carrying three pages and needs a sentence saying what belongs there — procedures for driving the system by hand, as against Runbooks, which recover a subsystem after something broke.

**Steps:**

- [ ] **Step 1: Add the section description** to *Where to start*.
- [ ] **Step 2: Extend *What generates what*** with the scripts page's source: each script's own module docstring.
- [ ] **Step 3: Build with `--strict`, run `uv run pytest`, run `uv run prek run --all-files`.**
- [ ] **Step 4: Commit.**

---

## Deliberately not in this plan

- **A page per script.** The generated reference plus each script's own docstring is the documentation. A hand-written page per script is 40 pages that go stale together.
- **Documenting the Claude tooling** (`.claude/hooks/`, the skills, the agents). It is developer tooling for this repo, not homelab operations, and the repo-root `CLAUDE.md` already covers it for the audience that needs it.
- **Anything under `ansible/roles/*/CLAUDE.md`.** Those are authoritative and close to the code they describe. The site links to them; it does not copy them.
