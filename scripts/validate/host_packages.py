#!/usr/bin/env python3
"""Render, diff and lint the host packages: the checks behind CI's `host_packages` job.

H3's *CI (builds, never signs)* runs these in order. `render` writes every package a tree
holds, `diff` compares the render of the PR head with the render of its merge base, and the
job ends there when no rendered tree differs. Otherwise the job builds both renders with the
pinned nfpm, and `lint` checks what it rendered and what it built:

  * the `type: config` lint (H3's *Package rules*, plain files only): no `nfpm.yaml` content
    entry carries a `type` starting `config`, and no built `.deb` lists a conffile. A conffile
    survives an upgrade that drops or changes it, so the package would stop owning the file it
    ships.
  * the maintainer-script lint: every control script of a built `.deb` holds only the line
    shapes in `SCRIPT_WHITELIST`. `postinst` reloads systemd and starts reconcile without
    blocking, `prerm` stops the package's own `homelab-*` units only on `remove`, and `preinst`
    refuses a hostname mismatch. The whitelist admits no `dpkg-divert` line; H7's *In-place
    edits* adds those shapes with the first diversion.

`render` runs the renderer that ships in the tree it renders, so a renderer change that alters
a render shows in the diff. A tree with no `hosts/` directory predates H3.1 and renders no
package.

Usage:
    uv run python scripts/validate/host_packages.py render --tree . --out /tmp/head
    uv run python scripts/validate/host_packages.py diff /tmp/base /tmp/head --summary FILE
    uv run python scripts/validate/host_packages.py lint /tmp/head /tmp/debs

Exit codes: 0 the render, diff or lint succeeded and the lint found nothing; 1 the lint found a
problem (each `[FAIL]` line on stderr names it), or nothing to lint, or a render failed; 64 a
usage error. `diff` exits 0 whether or not the renders differ, and prints `changed=true` or
`changed=false` on stdout for `$GITHUB_OUTPUT`.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from lib import yaml_fast
from lib.cli_help import ArgumentParser

_UNIT = r"homelab-[a-z0-9-]+\.(?:service|timer|socket|path)"
_HOST = r"[a-z0-9][a-z0-9-]*"
_COMMON = [r"#!/bin/sh", r"", r"#.*", r"exit 0"]
SCRIPT_WHITELIST = {
    "postinst": [
        r"systemctl daemon-reload \|\| true",
        r"systemctl --no-block start homelab-reconcile\.service \|\| true",
    ],
    "prerm": [
        r'if \[ "\$1" = remove \]; then',
        rf"    systemctl stop {_UNIT} \|\| true",
        r"fi",
    ],
    "preinst": [
        rf'if \[ "\$\(hostname\)" != "{_HOST}" \]; then',
        rf'    echo "homelab-{_HOST} installs only on {_HOST}" >&2',
        r"    exit 1",
        r"fi",
    ],
}

# Control members dpkg-deb writes for every package, which hold no script.
_CONTROL_DATA = frozenset({"control", "md5sums"})


def lint_script(name: str, text: str) -> list[str]:
    """Each line of maintainer script `name` that no whitelisted shape matches.

    A script name outside the whitelist is one finding on its own.
    """
    if name not in SCRIPT_WHITELIST:
        return [f"{name}: no maintainer script of this name is allowed"]
    shapes = [re.compile(shape) for shape in [*_COMMON, *SCRIPT_WHITELIST[name]]]
    return [
        f"{name}:{number}: {line!r}"
        for number, line in enumerate(text.splitlines(), 1)
        if not any(shape.fullmatch(line) for shape in shapes)
    ]


def lint_config_types(spec: str) -> list[str]:
    """Each content entry of an `nfpm.yaml` whose `type` makes it a conffile."""
    contents = (yaml_fast.safe_load(spec) or {}).get("contents") or []
    return [
        f"{entry.get('dst')}: type {entry['type']!r} ships a conffile; "
        "host packages ship plain files only"
        for entry in contents
        if str(entry.get("type", "")).startswith("config")
    ]


def lint_deb(deb: Path) -> list[str]:
    """Each conffile and each non-whitelisted maintainer-script line in a built `.deb`."""
    problems = []
    with tempfile.TemporaryDirectory() as control:
        subprocess.run(["dpkg-deb", "--control", str(deb), control], check=True)
        for member in sorted(Path(control).iterdir()):
            if member.name in _CONTROL_DATA:
                continue
            text = member.read_text()
            if member.name == "conffiles":
                problems.extend(
                    f"conffiles lists {path}; host packages ship plain files only"
                    for path in text.split()
                )
            else:
                problems.extend(lint_script(member.name, text))
    return problems


def specs(tree: Path) -> list[tuple[str, str, str]]:
    """Each package `tree` holds, as the renderer's `(mode, flag, name)` arguments."""
    hosts = tree / "hosts"
    shared = [
        ("package", "--name", spec.parent.name)
        for spec in sorted(hosts.glob("packages/*/package.yml"))
    ]
    host = [
        ("host", "--host", spec.parent.name)
        for spec in sorted(hosts.glob("*/package.yml"))
    ]
    return shared + host


def render(tree: Path, out: Path) -> int:
    """Render every package in `tree` into `out` with `tree`'s own renderer."""
    if not (tree / "hosts").is_dir():
        print(f"{tree} has no hosts/ directory, so it renders no package")
        return 0
    found = specs(tree)
    if not found:
        # hosts/ exists but the globs matched nothing, so they no longer find the specs.
        print(f"[FAIL] {tree / 'hosts'}: holds no package.yml", file=sys.stderr)
        return 1
    renderer = tree / "scripts" / "renderer" / "homelab_render.py"
    for mode, flag, name in found:
        result = subprocess.run(
            [sys.executable, str(renderer), mode, flag, name, "--out", str(out)],
            cwd=tree,
            check=False,
        )
        if result.returncode != 0:
            print(
                f"[FAIL] {name}: the render exited {result.returncode}", file=sys.stderr
            )
            return 1
    return 0


def _packages(out: Path) -> dict[str, dict[str, tuple[bytes, int]]]:
    """Each rendered package directory under `out`, mapped to its files' bytes and modes."""
    packages = {}
    for spec in sorted(out.rglob("nfpm.yaml")):
        directory = spec.parent
        packages[str(directory.relative_to(out))] = {
            str(path.relative_to(directory)): (
                path.read_bytes(),
                path.stat().st_mode & 0o7777,
            )
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }
    return packages


def diff(base: Path, head: Path) -> list[str]:
    """The file-level difference between two renders, one Markdown list item per change."""
    old, new = _packages(base), _packages(head)
    lines = []
    for package in sorted(old.keys() | new.keys()):
        if package not in new:
            lines.append(f"- `{package}`: dropped")
            continue
        if package not in old:
            lines.append(f"- `{package}`: new, {len(new[package])} files")
            continue
        before, after = old[package], new[package]
        for path in sorted(before.keys() | after.keys()):
            if path not in after:
                change = "removed"
            elif path not in before:
                change = "added"
            elif before[path][0] != after[path][0]:
                change = "content changed"
            elif before[path][1] != after[path][1]:
                change = f"mode {before[path][1]:04o} -> {after[path][1]:04o}"
            else:
                continue
            lines.append(f"- `{package}/{path}`: {change}")
    return lines


def lint(paths: list[Path]) -> list[str]:
    """Every problem in each `nfpm.yaml` and `.deb` under `paths`, as `[FAIL]` lines."""
    problems, checked = [], 0
    for path in paths:
        for spec in sorted(path.rglob("nfpm.yaml")):
            checked += 1
            problems.extend(
                f"[FAIL] {spec}: {p}" for p in lint_config_types(spec.read_text())
            )
        for deb in sorted(path.rglob("*.deb")):
            checked += 1
            problems.extend(f"[FAIL] {deb}: {p}" for p in lint_deb(deb))
    if not checked:
        problems.append(
            f"[FAIL] {' '.join(map(str, paths))}: no nfpm.yaml or .deb to lint"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    render_cmd = commands.add_parser(
        "render", help="render every package of TREE into DIR"
    )
    render_cmd.add_argument("--tree", type=Path, required=True, metavar="TREE")
    render_cmd.add_argument("--out", type=Path, required=True, metavar="DIR")
    diff_cmd = commands.add_parser("diff", help="compare two renders")
    diff_cmd.add_argument("base", type=Path)
    diff_cmd.add_argument("head", type=Path)
    diff_cmd.add_argument(
        "--summary", type=Path, metavar="FILE", help="append the Markdown diff to FILE"
    )
    lint_cmd = commands.add_parser(
        "lint", help="lint every nfpm.yaml and .deb under PATH"
    )
    lint_cmd.add_argument("paths", nargs="+", type=Path, metavar="PATH")
    args = parser.parse_args(argv)

    if args.command == "render":
        return render(args.tree.resolve(), args.out.resolve())
    if args.command == "diff":
        lines = diff(args.base, args.head)
        report = "\n".join(
            ["### Host packages: rendered diff against the merge base", ""]
            + (lines or ["No rendered tree differs."])
        )
        print(report, file=sys.stderr)
        if args.summary:
            with args.summary.open("a") as summary:
                summary.write(report + "\n")
        print(f"changed={'true' if lines else 'false'}")
        return 0
    problems = lint(args.paths)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
