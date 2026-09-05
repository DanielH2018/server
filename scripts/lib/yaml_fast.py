#!/usr/bin/env python3
"""`yaml.safe_load` backed by libyaml, which parses the same schema an order faster.

WHY THIS EXISTS. PyYAML's `safe_load` uses a parser written in Python, and this repo
parses a lot of YAML: every guard that renders the k8s tree, every validator that reads
`roles/**/tasks/*.yml`, every test that loads a manifest. Measured 2026-09-03 over the
184 `ansible/**/tasks/*.yml` files, warm, the two parsers differ by 10x:

    yaml.safe_load (pure Python):   0.42s
    yaml.load(..., CSafeLoader):    0.04s

That was the largest single cost in the test suite. Full run at `-n 4`, the two variants
interleaved on the same box so a busy neighbour could not favour either:

    yaml.safe_load throughout:  44.4s / 64.4s
    this module throughout:     31.4s / 32.0s

Both CI legs of a landing are pytest-bound and a landing waits on CI twice, so that is
paid four times over per change.

SAME SCHEMA, NOT A LOOSER ONE. `CSafeLoader` is libyaml's implementation of the SAME
YAML 1.1 safe schema `SafeLoader` implements: no arbitrary object construction, no
`!!python/` tags. It is a faster parser, not a more permissive one. Its errors are
`yaml.YAMLError` subclasses exactly as before, so an existing `except yaml.YAMLError`
keeps working.

WHAT THIS MODULE IS NOT FOR. A caller that subclasses `yaml.SafeLoader` to add its own
constructors — `HAConfigLoader` for HA's `!include`/`!secret`, the two mkdocs test
loaders for `!!python/name:` — keeps doing that and does not come through here. Those
already call `yaml.load(..., Loader=...)` with a loader of their own.

NOT REACHABLE FROM A ROLE'S `files/`. A role ships only its own `files/` directory, so
the cluster-side modules under `ansible/roles/*/files/` cannot import this and still
call `yaml.safe_load`. They are not a CI cost and are deliberately left alone.
"""

import yaml

# The C extension ships in PyYAML's manylinux wheels, so this resolves to CSafeLoader on
# every machine this repo runs on. The getattr is for a source build without libyaml,
# where the correct behaviour is to be slow rather than to fail — but that fallback is
# SILENT, and a silent fallback costs the whole 30% with nothing to show for it. Hence
# `test_the_c_loader_is_the_one_actually_selected`, which asserts the fast loader
# specifically rather than "a loader was chosen".
Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def safe_load(stream):
    """`yaml.safe_load`, parsed by libyaml. Same schema, same exceptions."""
    return yaml.load(stream, Loader=Loader)


def safe_load_all(stream):
    """`yaml.safe_load_all`, parsed by libyaml. Lazy, exactly as the original is."""
    return yaml.load_all(stream, Loader=Loader)
