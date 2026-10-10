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

A YAML key whose value Renovate bumps is not citable as a YAML atom. Every bump would move the
atom, and the Renovate PR cannot run `verify`, so it stays red until a person commits to it
(#4012). Cite the file as a path atom and name the key in prose. The lint's `renovate-pin`
rule derives the managed keys from `renovate.json`'s `customManagers`, plus the built-in
github-actions and ansible managers, whose default file patterns `scripts/lib/facts/pins.py`
restates. A claim that must hold for one specific version belongs in a
test, which the bump PR runs.

## Rules that read prose, and the history marker

The lock hashes cited atoms only, so a sentence that cites nothing can go stale behind an IN
grade. Some lint rules therefore read the prose itself.

- `retired-host` (error) flags a host-shaped word that names no host in
  `ansible/inventory/hosts.ini`. The rule derives the host prefix from the inventory, never
  from a list. A word inside a path or a longer name, such as `/srv/artifacts/daniel-box-claude`
  or the domain `daniel-hunter.com`, is not a host mention.
- `version-as-fact` (warning) flags a backticked `image:tag` span whose image the tree pins.
  The pins are the `*_image` keys in role defaults and inventory vars, plus the `image:` lines
  in the Pi's compose templates. A tag no pin carries reads as stale, and the message names
  the pin's variable and file. A tag that matches still restates a value Renovate moves, so
  name the variable instead (`registry` (`registry_k8s_image`)). It stays a warning because
  a Renovate PR cannot edit prose and must not arrive red (#4012). A `generated_from` block
  is skipped, since it regenerates from the pin. A version that is not an `image:tag` span is
  out of scope, such as speedtest's upstream `v1.14.7`: its pin is `latest@sha256`, so the
  tree holds no tag to compare it with.

A paragraph or bullet that opens with `**HISTORY —` is history, and every prose rule skips it.
A sentence such as "it lived on the staging guest until 2026-09-28" is correct exactly because
the thing it names is gone, and the marker says so. A history bullet takes its indented
continuation lines and nested bullets with it; a history paragraph ends at the next blank line.
`scripts/lib/facts/citations.py:HISTORY_MARKER` is the one definition.

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
