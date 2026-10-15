"""How the live-read census reads a template or an expression: its names and its lookups.

`Reader.read` returns the names a Jinja source reads, through the syntax tree rather than a
token grep, so a name in a comment or in a Dockerfile's plain text reads nothing. It judges
every `lookup`, `query` or `q` call it finds. It accepts four plugins:

- `file` and `template`, which read the tree. The names in their path expressions are names
  the source reads.
- `pipe`, only as `'base64 -w0 ' ~ <path>`, the one shape `scripts/lib/k8s_yaml.py:make_lookup`
  implements. Each piece of the path is a literal with no space or shell metacharacter,
  `playbook_dir`, or a loop variable over a literal list of such pieces.
- `vars`, only over an attribute of a loop variable whose loop runs over a
  `py_table('CHECKS')` file lookup or over `containers_list | scrape_jobs(...)`. It runs the
  filter, as the render does, and every value of that attribute is a name the source reads.

Any other call is a problem, because nothing can say what it reads.

`vars.X`, `vars['X']`, `hostvars[H].X` and `hostvars[H]['X']` read the name `X`, as a bare
`X` does. A computed `X`, or `vars` or `hostvars` read any other way, is a problem too. `_live_read_census.py`
is the census that calls it.
"""

import re
from collections.abc import Iterator
from pathlib import Path

from jinja2 import Environment, meta, nodes
from py_table import py_table
from scrape_jobs import scrape_jobs

from lib.ansible_jinja_env import make_ansible_env

LOOKUPS = frozenset({"lookup", "query", "q"})
PIPE_PREFIX = "base64 -w0 "
# A literal piece of a piped path: no space and no shell metacharacter.
PATH_PIECE = re.compile(r"[A-Za-z0-9_./-]+")
# The two variables that hold other variables by name.
SCOPES = frozenset({"vars", "hostvars"})


def _path_value(playbook_dir: Path, node: nodes.Expr) -> str | None:
    """A path expression of literals and `playbook_dir`, as a string, or None if it holds more."""
    if isinstance(node, nodes.Const) and isinstance(node.value, str):
        return node.value
    if isinstance(node, nodes.Name) and node.name == "playbook_dir":
        return str(playbook_dir)
    if isinstance(node, (nodes.Concat, nodes.Add)):
        parts = (
            node.nodes if isinstance(node, nodes.Concat) else [node.left, node.right]
        )
        values = [_path_value(playbook_dir, part) for part in parts]
        return None if None in values else "".join(v for v in values if v is not None)
    return None


class _AnyName(dict):
    """A filter or test table that holds every name, for a parse that never renders."""

    def __contains__(self, name: object) -> bool:
        return True

    def get(self, name, default=None):
        return super().get(name, _no_op)


def _no_op(*args, **kwargs) -> None:
    return None


class Reader:
    """Reads Jinja sources and records each lookup it accepts or cannot name the reads of.

    Attributes:
        accepted: Each accepted `pipe` or `vars` lookup, as `<plugin> in <where>`.
        vars_names: Every name a `vars` lookup source yields.
        problems: Each lookup whose reads it cannot name.
    """

    def __init__(self, playbook_dir: Path, containers: list[dict]) -> None:
        """A reader for one tree.

        Args:
            playbook_dir: What `playbook_dir` holds, so a file lookup's path resolves.
            containers: The k8s play's `containers_list`, which `scrape_jobs` runs over.
        """
        self.playbook_dir = playbook_dir
        self.containers = containers
        self.accepted: list[str] = []
        self.vars_names: set[str] = set()
        self.problems: list[str] = []
        env: Environment = make_ansible_env()
        # `find_undeclared_variables` compiles, and compiling refuses an unknown filter or
        # test. The census renders nothing, and an inventory value may name a filter only a
        # setup role's play loads, so every name passes.
        env.filters = _AnyName(env.filters)
        env.tests = _AnyName(env.tests)
        self.env = env

    def _vars_source(self, loop: nodes.For, attr: str) -> set[str] | None:
        """The names `lookup('vars', <loop var>.<attr>)` reads, or None for another source."""
        source = loop.iter
        if not isinstance(source, nodes.Filter):
            return None
        if source.name == "py_table" and [
            a.value for a in source.args if isinstance(a, nodes.Const)
        ] == ["CHECKS"]:
            call = source.node
            if not (isinstance(call, nodes.Call) and isinstance(call.node, nodes.Name)):
                return None
            if call.node.name not in LOOKUPS or len(call.args) != 2:
                return None
            plugin = call.args[0]
            path = _path_value(self.playbook_dir, call.args[1])
            if not (
                isinstance(plugin, nodes.Const) and plugin.value == "file" and path
            ):
                return None
            rows = py_table(Path(path).read_text(), "CHECKS")
        elif source.name == "scrape_jobs" and isinstance(source.node, nodes.Name):
            if source.node.name != "containers_list":
                return None
            rows = scrape_jobs(self.containers, "namespace")
        else:
            return None
        return {str(row[attr]) for row in rows if attr in row}

    def _lookup(self, where: str, call: nodes.Call, loops: list[nodes.For]) -> set[str]:
        """Judge one lookup call; return the names it reads beyond its arguments."""
        plugin = call.args[0] if call.args else None
        if not (isinstance(plugin, nodes.Const) and isinstance(plugin.value, str)):
            self.problems.append(f"{where}: a lookup whose plugin is not a literal")
            return set()
        kind = plugin.value.removeprefix("ansible.builtin.")
        arg = call.args[1] if len(call.args) > 1 else None
        if kind in ("file", "template"):
            return set()
        if kind == "pipe":
            if _is_base64_of_a_path(arg, loops):
                self.accepted.append(f"pipe in {where}")
                return set()
            self.problems.append(
                f"{where}: lookup('pipe') in a shape other than 'base64 -w0 ' ~ <path>"
            )
            return set()
        if kind == "vars":
            if isinstance(arg, nodes.Getattr) and isinstance(arg.node, nodes.Name):
                for loop in reversed(loops):
                    if (
                        isinstance(loop.target, nodes.Name)
                        and loop.target.name == arg.node.name
                    ):
                        names = self._vars_source(loop, arg.attr)
                        if names is not None:
                            self.accepted.append(f"vars in {where}")
                            self.vars_names |= names
                            return names
                        break
            self.problems.append(
                f"{where}: lookup('vars') over an argument the census cannot resolve"
            )
            return set()
        self.problems.append(
            f"{where}: lookup({kind!r}), a plugin the census does not accept"
        )
        return set()

    def _scan(self, where: str, node: nodes.Node, loops: list[nodes.For]) -> set[str]:
        names = set()
        scope = _scope_access(node)
        if scope is not None:
            # `vars.X`, `vars['X']`, `hostvars[H].X` and `hostvars[H]['X']` read the name X,
            # which `find_undeclared_variables` never sees.
            key, subscripts = scope
            if key is None:
                self.problems.append(
                    f"{where}: vars or hostvars subscripted by a name the census cannot resolve"
                )
            else:
                names.add(key)
            for child in subscripts:
                names |= self._scan(where, child, loops)
            return names
        if isinstance(node, nodes.Name) and node.ctx == "load" and node.name in SCOPES:
            self.problems.append(
                f"{where}: {node.name} read other than as one named variable, so the census "
                "cannot name what it reads"
            )
        if (
            isinstance(node, nodes.Call)
            and isinstance(node.node, nodes.Name)
            and node.node.name in LOOKUPS
        ):
            names |= self._lookup(where, node, loops)
        if isinstance(node, nodes.For):
            # The loop's own source is outside its loop variable's scope.
            names |= self._scan(where, node.iter, loops)
            inner = [*loops, node]
            for child in [
                node.target,
                *node.body,
                *node.else_,
                *([node.test] if node.test else []),
            ]:
                names |= self._scan(where, child, inner)
            return names
        for child in node.iter_child_nodes():
            names |= self._scan(where, child, loops)
        return names

    def read(self, where: str, source: str) -> tuple[set[str], nodes.Template]:
        """The names `source` reads, the `vars` lookup names included, and its syntax tree."""
        ast = self.env.parse(source)
        names = meta.find_undeclared_variables(ast) - self.env.globals.keys()
        return names | self._scan(where, ast, []), ast


def _key(node: nodes.Getattr | nodes.Getitem) -> str | None:
    """The literal name a `.X` or `['X']` access selects, or None for a computed one."""
    if isinstance(node, nodes.Getattr):
        return node.attr
    arg = node.arg
    return (
        arg.value
        if isinstance(arg, nodes.Const) and isinstance(arg.value, str)
        else None
    )


def _scope_access(node: nodes.Node) -> tuple[str | None, list[nodes.Expr]] | None:
    """For a variable read through `vars` or a host's `hostvars` entry, the name and subscripts.

    Returns None for any other node. Otherwise returns the name the access selects, None when
    it is computed, and the subscript expressions the access evaluates, which read names too.
    """
    if not isinstance(node, (nodes.Getattr, nodes.Getitem)):
        return None
    base = node.node
    own = [node.arg] if isinstance(node, nodes.Getitem) else []
    if isinstance(base, nodes.Name) and base.name == "vars":
        return _key(node), own
    if (
        isinstance(base, (nodes.Getattr, nodes.Getitem))
        and isinstance(base.node, nodes.Name)
        and base.node.name == "hostvars"
    ):
        host = [base.arg] if isinstance(base, nodes.Getitem) else []
        return _key(node), [*host, *own]
    return None


def _is_path_part(node: nodes.Node, loops: list[nodes.For]) -> bool:
    """A literal path piece, `playbook_dir`, or a loop variable over literal path pieces."""
    if isinstance(node, nodes.Const):
        return isinstance(node.value, str) and bool(PATH_PIECE.fullmatch(node.value))
    if not isinstance(node, nodes.Name):
        return False
    if node.name == "playbook_dir":
        return True
    for loop in reversed(loops):
        if isinstance(loop.target, nodes.Name) and loop.target.name == node.name:
            items = loop.iter
            return isinstance(items, (nodes.List, nodes.Tuple)) and all(
                _is_path_part(item, []) for item in items.items
            )
    return False


def _is_base64_of_a_path(arg: nodes.Node | None, loops: list[nodes.For]) -> bool:
    """`'base64 -w0 ' ~ <path>`, where every piece of the path is a path piece.

    A piece is never a shell metacharacter or a space, so the command stays base64 over one
    file: `'base64 -w0 ' ~ p ~ '; cmd'` does not pass.
    """
    if not (isinstance(arg, nodes.Concat) and len(arg.nodes) > 1):
        return False
    first, *path = arg.nodes
    return (
        isinstance(first, nodes.Const)
        and first.value == PIPE_PREFIX
        and all(_is_path_part(piece, loops) for piece in path)
    )


def references(ast: nodes.Template) -> Iterator[str]:
    """Each template name an `{% import %}`, `{% from %}` or `{% include %}` gives as a literal."""
    for node in ast.find_all((nodes.Import, nodes.FromImport, nodes.Include)):
        template = node.template
        consts = (
            template.items
            if isinstance(template, (nodes.List, nodes.Tuple))
            else [template]
        )
        for const in consts:
            if isinstance(const, nodes.Const) and isinstance(const.value, str):
                yield const.value


def template_lookups(playbook_dir: Path, ast: nodes.Template) -> Iterator[str]:
    """Each `lookup('template', P)` path that is a literal or `playbook_dir` plus literals."""
    for call in ast.find_all(nodes.Call):
        if not (
            isinstance(call.node, nodes.Name)
            and call.node.name in LOOKUPS
            and len(call.args) > 1
        ):
            continue
        plugin = call.args[0]
        if (
            isinstance(plugin, nodes.Const)
            and str(plugin.value).removeprefix("ansible.builtin.") == "template"
        ):
            path = _path_value(playbook_dir, call.args[1])
            if path:
                yield path
