"""The census row that keeps `renderer_lib` writing YAML through its one writer.

The row sits in its own module because it carries its own allow-list and named members.
"""

import re

from _row_table import Census, Subject, lines_matching, tracked

WRITER = "scripts/renderer/renderer_lib/manifest_yaml.py"

RENDERER_LIB_WRITES_YAML_THROUGH_MANIFEST_YAML = Census(
    name="renderer-lib-writes-yaml-through-manifest-yaml",
    reason=(
        "`yaml.safe_dump` writes a string such as `05094468` without quotes, and Flux reads "
        "it back as a number, which failed a snapshot Job's dry run and set DEPLOY_HOLD on "
        "2026-11-27. `renderer_lib.manifest_yaml`'s `dump` and `dump_all` quote such a "
        "string. Write a document the renderer built through them."
    ),
    files=lambda: tracked("scripts/renderer/renderer_lib/*.py"),
    offence=lines_matching(re.compile(r"\byaml\.(?:safe_)?dump(?:_all)?\(")),
    red=(
        Subject("a.py", "text = yaml.safe_dump(job, sort_keys=False)\n"),
        Subject("b.py", "text = yaml.safe_dump_all(docs)\n"),
        Subject("c.py", "text = yaml.dump(job, Dumper=Mine)\n"),
    ),
    green=(
        Subject("a.py", "text = dump(job)\n"),
        Subject("b.py", "docs = yaml_fast.safe_load_all(text)\n"),
    ),
    min_matches=40,
    must_find=frozenset(
        {
            "scripts/renderer/renderer_lib/snapshot.py",
            "scripts/renderer/renderer_lib/soak.py",
            "scripts/renderer/renderer_lib/prepull.py",
        }
    ),
    allow={WRITER: "the writer itself, which calls PyYAML with its own dumper"},
)
