# Hand-authored diagrams with D2

The docs site renders three classes of diagram. Only one of them is not built yet, and this is
the plan for that one.

| Class | Source | Built by |
|---|---|---|
| Content comes from the tree | the tree itself | a generator emits SVG (`scripts/docs/build_docs.py`) |
| Someone draws it | `docs/diagrams/*.d2` | **this plan** — not yet built |
| Neither | a committed SVG | an escape hatch, checked in by hand |

Mermaid is deliberately not on that list. It routes the edges in a container-and-tier
diagram badly enough that the picture misleads, which is why the site carries D2 source
instead.

This is the last slice of the docs-UI programme; the four that preceded it shipped in
PRs #416, #417 and #418. Nothing depends on it, which is why it is last. The rest of that
plan is archived at
[`docs/archive/docs-ui-and-adrs-plan-2.md`](docs-ui-and-adrs-plan-2.md).

**Cost to be aware of before starting:** D2 is a second binary. It needs installing on the
host and pinning in CI, the way Vale was in Task 4 of the archived plan.


## Files
- Create: `docs/diagrams/<name>.d2` (source, committed)
- Create: `docs/assets/generated/<name>.svg` (rendered, committed)
- Modify: `prek.toml` (add the render hook)
- Modify: `.github/workflows/ci.yml` (install D2)
- Create: `scripts/render_d2.py`
- Test: `scripts/test_render_d2.py`

## Step 1: Install D2 and render one diagram

Install the D2 binary and note the version. Then write the first real diagram — the backup chain from `docs/longhorn-backup-tiering.md` is a good first subject, because it has containers, two tiers, and edges that mermaid routes badly.

```bash
d2 --theme 0 docs/diagrams/backup-chain.d2 docs/assets/generated/backup-chain.svg
```

Embed it in `docs/longhorn-backup-tiering.md` and check it renders in the built site.

## Step 2: Write the render script and its test

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

## Step 3: Add the prek hook

A `files: ^docs/diagrams/.*\.d2$` hook running `render_d2.py --check`. It fails when a `.d2` changed and its `.svg` did not.

**Do not have the hook re-render in place.** A hook that writes files makes `git diff` show changes the author did not make, and prek reports a confusing "files were modified by this hook" on every commit.

## Step 4: Add D2 to CI

Install the pinned D2 version in `.github/workflows/ci.yml`, then verify from a real run that the hook executed rather than skipped.

## Step 5: Document the three diagram classes

Add a short section to `docs/index.md` stating when to use which:

- a diagram whose content comes from the tree → a generator emits SVG
- a diagram someone draws → D2 source in `docs/diagrams/`
- neither → a committed SVG, as an escape hatch

Without this, the next person adds a mermaid block and nothing stops them.

## Step 6: Commit, then open the PR

Run: `uv run prek run --all-files`
Run: `uv run pytest`
Run: `uv run mkdocs build --strict`

Then follow the repo's *After a PR Merges* procedure: record the pre-merge SHA, wait for master CI on the merge commit specifically, `./scripts/deploy_tools/gitops_tick.sh`, then deploy from the primary checkout.
