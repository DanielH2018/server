"""Collect pytest test modules with ansible-core refused, and record who tried to import it.

Run as a child process from the repo root:
`python _collect_without_ansible.py <out.json> <allowed,files> [pytest args...]`.
A wrapper around `builtins.__import__` refuses every `ansible` and `ansible.*` import, as
`sys.modules['ansible'] = None` would, unless the importing file is one of the allowed ones.
`ansible/tests/_helpers.py` stays allowed until H7.54, and a conftest imports it, so a refusal
there would abort the whole collection rather than one module. The wrapper sees an import
that an earlier caller already cached, which a `meta_path` finder would not.

`<out.json>` maps each importing file, allowed or not, to the modules it asked for. No
pytest args collects every `testpaths` entry.
"""

import builtins
import json
import sys
from pathlib import Path

HALTED = "import of ansible halted"


def _rel(file: str) -> str:
    path, root = Path(file), Path.cwd()
    return str(path.relative_to(root)) if path.is_relative_to(root) else file


def main(out: Path, allowed: frozenset[str], pytest_args: list[str]) -> int:
    importers: dict[str, set[str]] = {}
    real_import = builtins.__import__

    def refusing_import(name, globals=None, locals=None, fromlist=(), level=0):
        caller = (globals or {}).get("__name__", "")
        if (
            level == 0
            and (name == "ansible" or name.startswith("ansible."))
            and not (caller == "ansible" or caller.startswith("ansible."))
        ):
            importer = _rel((globals or {}).get("__file__") or "<unknown>")
            importers.setdefault(importer, set()).add(name)
            if importer not in allowed:
                raise ModuleNotFoundError(HALTED, name=name)
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = refusing_import  # ty: ignore[invalid-assignment]
    import pytest

    pytest.main(["--collect-only", "-q", "-n0", "-p", "no:cacheprovider", *pytest_args])
    out.write_text(
        json.dumps({rel: sorted(names) for rel, names in sorted(importers.items())})
    )
    return 0


if __name__ == "__main__":
    allow = frozenset(filter(None, sys.argv[2].split(",")))
    sys.exit(main(Path(sys.argv[1]), allow, sys.argv[3:]))
