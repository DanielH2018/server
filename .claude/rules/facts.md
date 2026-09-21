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

A citation is support only when it names a **tracked** file, or a directory holding a
tracked file — so an untracked or gitignored path is prose, not broken support, the same as
a doc-relative `defaults/main.yml` or a slashed token that names nothing here at all. A
verdict must not depend on which checkout runs it, and a citation resolving only on the
machine that happens to have the file on disk (a gitignored spec, an uncommitted draft) is
exactly the failure this rules out. The tracked set is read from `git ls-files`, not listed.
A rejected form stays rejected whatever its prefix: a line number is a claim about this tree
however it is spelled.

`docs/facts.lock` records the hashes each verified section was checked against. The tool
writes it; a hand edit fails `test_every_recorded_atom_hashes_as_recorded` as tampered.
`fact_status.py status` prints every section's status. `verify '<doc>#<heading>'` is the
only path from OUT back to IN, and `forget '<doc>#<heading>'` drops a row whose heading is
gone — a rename makes a new unit, and the old row cannot be hand-deleted without tripping
the lock's checksum. The `facts-lint-changed` prek hook is the ratchet for everything not
yet in the lock: it runs `lint --changed-since` over the sections a commit edits.

A section that cites atoms but has no lock row is UNVERIFIED and never fails anything. A
section that cites nothing is a convention and is never graded. A `probe.py` citation reads
UNKNOWN rather than IN: nothing in this slice runs a probe, so its shape hash waits on the
reconcile timer. The memory store, the reconcile timer and the re-verifier are the spec's
later slices:
`docs/superpowers/specs/2026-09-19-fact-support-invalidation-design.md`.
