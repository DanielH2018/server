---
paths:
  - "CLAUDE.md"
  - "**/CLAUDE.md"
  - "docs/facts.lock"
  - "scripts/lib/facts/**"
  - "scripts/dev/fact_status.py"
---

# Fact support — how a `CLAUDE.md` section cites what holds it up

A `CLAUDE.md` section's status is derived from the atoms it cites, never stated. A section
is the text under one heading up to the next heading of any level, so every citation has
exactly one owning section, keyed `<doc>#<heading text>`. Heading text is not scanned for
citations: a backticked span in a heading names the section, never an atom in it.

The grammar is closed — `scripts/lib/facts/citations.py:FORMS` — and a backticked span is
support only in one of these six shapes:

```text
# a repo path; a directory keeps its trailing slash
ansible/roles/k8s/traefik/
# a Python symbol
scripts/lib/kubectl.py:kubectl
# a YAML key
ansible/roles/k8s/traefik/defaults/main.yml:traefik_k8s_https_port
# a test node
scripts/lib/tests/test_kubectl.py::test_asking_for_staging_against_a_prod_kubectl_is_flagged
# a DECIDED: marker, cited by text prefix
ansible/roles/setup/gitops_deploy/files/deploy_logic.py:DECIDED: a fixed slice while
# a probe subcommand
probe.py kuma-drift
```

A test node must carry `# fact: <doc>#<heading>` in its body, or the lint warns that the
support points one way. The examples are fenced because the lint reads every `CLAUDE.md`:
unfenced, each one would be support for this section rather than an illustration of the
form. The fence hides them from this lint only:
`ansible/tests/repo/test_documented_paths_exist.py::test_every_cited_test_exists` reads
every line of every doc, fenced or not, so the test node in the example names a real test,
and renaming that test fails the guard here.

A `file:line` citation is rejected: a line number moves under every edit above it. Cite the
symbol or the marker instead.

A path atom hashes existence, so it breaks only when the file goes away; cite a symbol, a YAML
key, a decision marker or a test node to pin content. A citation is support only when it
names a tracked file or a directory holding one, so an untracked path is prose. The reasons
are in the module docstrings under `scripts/lib/facts/`.

## Commands

`docs/facts.lock` records the hashes each verified section was checked against. The tool
writes it, and a hand edit fails `test_every_recorded_atom_hashes_as_recorded` as tampered.

- `fact_status.py status` prints every section's status.
- `fact_status.py verify '<doc>#<heading>'` is the only path from OUT back to IN.
- `fact_status.py verify --unverified` records every section with no lock row yet. It never
  touches an existing row. Run it when you add a section, because nothing fails on an
  UNVERIFIED one.
- `fact_status.py forget '<doc>#<heading>'` drops the row of a renamed or deleted heading.

Two prek hooks run on a commit. `facts-lint-changed` lints the sections the commit edits.
`facts-reverify-changed` re-hashes a section whose prose you edited, when only a citation was
added or dropped. It writes the lock and exits non-zero like a formatter, so
`git add docs/facts.lock` and commit again. A `moved` or `missing` atom stays red until a
person re-reads the section and runs `verify`.
