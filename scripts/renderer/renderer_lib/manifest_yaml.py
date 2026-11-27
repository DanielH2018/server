"""Writes a document the renderer built as YAML that Flux and kubectl read back unchanged.

PyYAML writes YAML 1.1, and the readers on the cluster side resolve plain scalars by go-yaml's
rules. The two disagree on which plain scalars are strings. `05094468` is a string to PyYAML,
because a leading zero means octal and 8 and 9 are not octal digits, so it writes the value
without quotes. go-yaml reads the integer 5094468, and a string field that holds it, such as a
container's env value, fails the API server's schema. On 2026-11-27 a role's `digest8` was
`05094468`, its snapshot Job failed Flux's dry run, and the promotion set `DEPLOY_HOLD`. About
1.6% of 8-hex digests have such a shape.

`dump` and `dump_all` quote every string go-yaml would not read as a string, and write every
other scalar as `yaml.safe_dump` does. Every module of `renderer_lib` writes YAML through
them, which the `renderer-lib-writes-yaml-through-manifest-yaml` census row checks.
"""

import re
from collections.abc import Iterable

import yaml

# go-yaml (kubectl, Flux) resolves `y`/`n`, `1e3`, `0o17` and `0900` as non-strings where
# PyYAML writes them plain.
GO_YAML_NON_STRING = re.compile(
    r"""
    (?: y|Y|yes|Yes|YES|n|N|no|No|NO
      | true|True|TRUE|false|False|FALSE
      | on|On|ON|off|Off|OFF
      | null|Null|NULL|~
      | [-+]?(?:\.[0-9]+|[0-9][0-9_]*(?:\.[0-9_]*)?)(?:[eE][-+]?[0-9]+)?
      | [-+]?0[xX][0-9a-fA-F_]+
      | [-+]?0[oO][0-7_]+
      | [-+]?0[bB][01_]+
      | [-+]?\.(?:inf|Inf|INF)
      | \.(?:nan|NaN|NAN)
    )
    """,
    re.VERBOSE,
)


class _Dumper(yaml.SafeDumper):
    pass


# PyYAML quotes a string whose plain form would resolve to another type, in the style it
# picks for any such string. Resolving these shapes too is what quotes them, and it leaves
# the text of every string PyYAML already quoted as it was.
_Dumper.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(rf"^(?:{GO_YAML_NON_STRING.pattern})$", re.VERBOSE),
    None,
)


def dump(document: object, *, width: int | None = None) -> str:
    """One document as YAML, its keys in the order given.

    Args:
        document: The document to write.
        width: The column a long scalar folds at, for a caller whose reader compares lines.
    """
    return yaml.dump(document, Dumper=_Dumper, sort_keys=False, width=width)


def dump_all(documents: Iterable[object]) -> str:
    """Several documents as one YAML stream, each one's keys in the order given."""
    return yaml.dump_all(documents, Dumper=_Dumper, sort_keys=False)
