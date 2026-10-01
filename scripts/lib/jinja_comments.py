#!/usr/bin/env python3
"""Blank a template's `{# … #}` comments so a text reader cannot match inside one.

WHY. The docs generators read `.j2` templates as text and match macro calls and their
arguments with regexes (`lib.k8s_roles`, `docs/route_facts.py`, `docs/catalog_backup.py`).
A regex sees no difference between an argument a call passes and the same text quoted in a
Jinja comment above it, so a comment that mentions `public=false` makes a public route read
as LAN-only — which is what `ansible/roles/k8s/docs/CLAUDE.md` said while its
`templates/ingressroute.yaml.j2` passed no `public` argument at all (#3148).

LINE NUMBERS AND INDENTATION SURVIVE. Each comment is replaced by its own newlines rather
than removed, because the readers beside this one are line-oriented: `glance_facts.py`
binds an `image:` to the two-space-indented service key above it, so merging the line
before a comment with the line after it would bind the pin to the wrong service.

A STRIPPED TEMPLATE IS NOT A RENDERABLE ONE. The output is for text matching only. Jinja's
own whitespace control (`{#-`, `-#}`) trims text around a comment that this function keeps,
so the result differs from what a render emits — intentionally, since a reader that counts
lines needs the lines.

Jinja comments do not nest, and `#}` closes the first one open, so a non-greedy span match
is the whole grammar. An unterminated `{#` is a `TemplateSyntaxError` at render time; here
it is left as written, so a reader of a broken template sees the text the deploy would
refuse rather than a silently truncated file.

Imported as `from lib.jinja_comments import strip_jinja_comments` after the caller's own
`sys.path` bootstrap puts `scripts/` on the path (`.claude/rules/python-layout.md`).
"""

import re

# Jinja's default `comment_start_string` / `comment_end_string`, non-greedy over newlines.
# Every template in this repo uses the default delimiters; a role that changed them would
# have to change the deploy's environment too.
_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)


def strip_jinja_comments(text: str) -> str:
    """`text` with every `{# … #}` span replaced by the newlines it spanned.

    Byte-identical to its input for a template that carries no comment, which is the
    property that makes it safe to add in front of an existing text reader.
    """
    return _COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
