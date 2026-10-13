"""Whether a secret value survives Flux's substitution as the same string (target slice R2).

A placeholder that is the whole scalar of a field, such as `main: ${domain}`, is a standalone
use. Flux substitutes into a resource it has already serialised, without the template's
quotes, and then reads the result as YAML again. A standalone value therefore keeps its
string type only when a YAML reader takes its bare text for exactly that string.
`refused(text)` answers that for one value, on the union of two readers:

- PyYAML's `safe_load`, a YAML 1.1 reader. It types timestamps and sexagesimal numbers, and
  it changes or rejects any text that is not a valid plain scalar: a leading `#`, `&`, `!` or
  `{` reads as null or a collection, surrounding spaces and quotes are stripped, and `@`, `%`
  or `: ` fail to parse.
- `go_yaml_v2_types`, a port of the plain-scalar resolver of go-yaml v2.4.4. The pinned flux
  CLI v2.9.6 carries `go.yaml.in/yaml/v2@v2.4.4`, which `sigs.k8s.io/yaml.YAMLToJSON` reads
  every substituted resource with. PyYAML reads `1e3`, `08`, `0o17` and `y` as strings, and
  the pinned CLI returned 1000, 8, 15 and true.

Refusing on the union fails closed where the readers differ. The CLI keeps a sexagesimal
value such as `1:20`, which PyYAML reads as the integer 80, so the union refuses more than the
CLI changes. `FLUX_KEEPS_BUT_REFUSED` in `renderer/renderer_lib/flux_probe.py` names each such
corpus value, and CI's flux probe holds both readers to the pinned CLI.

Two producers import this module: the deploy-time guard in
`ansible/filter_plugins/standalone_guard.py`, which loads it by path, and D3's
`cluster_vars.py write`. It imports the standard library and PyYAML only, so the filter plugin
can load it inside Ansible. `docs/homelab-render.md` is the operator's page.
"""

import re

import yaml

# The loader `lib.yaml_fast` selects, restated because the filter plugin loads this module by
# path and so cannot import `lib`. The same YAML 1.1 safe schema either way.
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# go-yaml v2.4.4 resolve.go: the plain scalars its resolve map types, every one but `<<`.
# The merge key `<<` decodes as the string `<<` in a value, as the pinned CLI returned it.
_GO_MAP = frozenset(
    "y Y yes Yes YES true True TRUE on On ON "
    "n N no No NO false False FALSE off Off OFF "
    "~ null Null NULL "
    ".nan .NaN .NAN .inf .Inf .INF +.inf +.Inf +.INF -.inf -.Inf -.INF".split()
)
# resolve.go's resolveTable: the first characters that send a scalar past the string default.
_GO_SIGNED, _GO_DIGITS, _GO_DOT = "+-", "0123456789", "."
# strconv.ParseInt(s, 0, 64) syntax, after resolve has stripped every `_`. An out-of-range
# value fails ParseInt and ParseUint and then matches the float rule below, so range is moot.
_GO_INT = re.compile(
    r"[-+]?(0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|0[0-7]*|[1-9][0-9]*)"
)
# resolve.go's yamlStyleFloat.
_GO_FLOAT = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?")
# strconv.ParseFloat over a `.`-led scalar: only a decimal fraction parses.
_GO_DOT_FLOAT = re.compile(r"\.[0-9]+([eE][-+]?[0-9]+)?")
# resolve.go's parseTimestamp: four digits and a `-`, then one of four time.Parse layouts.
# The shapes are matched without range checks, so an impossible date such as 2001-13-40 is
# refused too, which fails closed.
_GO_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}"
    r"([Tt][0-9]{1,2}:[0-9]{1,2}:[0-9]{1,2}(\.[0-9]+)?(Z|[-+][0-9]{2}:[0-9]{2})"
    r"| [0-9]{1,2}:[0-9]{1,2}:[0-9]{1,2}(\.[0-9]+)?)?"
)


def go_yaml_v2_types(text: str) -> bool:
    """Whether go-yaml v2.4.4 resolves the plain scalar `text` to anything but a string."""
    if text == "" or text in _GO_MAP:
        return True
    first = text[0]
    if first == _GO_DOT:
        return _GO_DOT_FLOAT.fullmatch(text) is not None
    if first not in _GO_SIGNED + _GO_DIGITS:
        return False
    if _GO_TIMESTAMP.fullmatch(text):
        return True
    plain = text.replace("_", "")
    return bool(
        _GO_INT.fullmatch(plain)
        or _GO_FLOAT.fullmatch(plain)
        or re.fullmatch(r"-?0b[01]+", plain)
    )


def yaml_1_1_keeps(text: str) -> bool:
    """Whether PyYAML's `safe_load` reads the bare text `text` back as exactly `text`."""
    try:
        return yaml.load(text, Loader=_LOADER) == text
    except yaml.YAMLError:
        return False


def refused(text: str) -> bool:
    """Whether a value whose placeholder stands alone in a field must be refused.

    True when either reader takes the bare text for anything but the same string.
    """
    return go_yaml_v2_types(text) or not yaml_1_1_keeps(text)
