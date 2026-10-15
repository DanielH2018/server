#!/usr/bin/env python3
"""cluster_vars: keep the cluster's SOPS files in step with ansible/vars/secrets.yml (slice D3).

Flux substitutes a flux-owned role's secret placeholders from one Secret, which
kustomize-controller decrypts from the cluster file `ansible/vars/cluster-vars.sops.env` with
a key only the cluster and the hosts hold. R2's multi-line keys travel as SOPS manifests under
`ansible/vars/cluster-manifests/` instead. `ansible/vars/cluster-vars.source` records, per key,
the digest of its ciphertext in `secrets.yml`, so CI can prove the files fresh without a key.

Two modes:

  write  — OPERATOR-ONLY, because it decrypts. It decrypts `secrets.yml` and the existing
           cluster files into memory, refuses a value Flux cannot carry, then changes only
           what moved: `sops set` for a changed key, `sops unset` for a dropped one, a fresh
           encrypt for a manifest whose plaintext changed. A no-change run leaves every file
           byte-identical. It prints key names, never a value. A refusal adds and changes no
           file, and so does a failed encrypt: every encrypt runs into memory before the first
           write.
  check  — reads no plaintext, so CI can run it; PR D3.2c adds the test that does. It
           renders the cluster in placeholder mode and asserts the files against the render
           and against `secrets.yml`'s ciphertext (`cluster_vars_check.py` holds each
           assertion). `--no-render` runs only the assertions that need no render, the
           annotation and `type:str` checks among them; the pre-commit hook
           `cluster-vars-ciphertext` runs that. `--ciphertext-only FILE...` asserts only that
           each named SOPS file holds ciphertext, for the publish job's copies (D3.3a).

Every `sops` call that resolves a creation rule passes `--config ansible/.sops.yaml`, because
SOPS finds its config from the working directory and the rotation cron runs from the repo
root.

`docs/target-architecture/deploy/d3-secrets.md` is the plan.

Exit codes: 0 clean, or written; 1 a check failed, a value was refused, or a sops call failed;
64 a usage error.

Run: uv run python scripts/secrets_mgmt/cluster_vars.py check --no-render --base HEAD
     uv run python scripts/secrets_mgmt/cluster_vars.py write      # operator-only
"""

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

import yaml

from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK
from lib.git import git
from lib.plain_scalar import refused as standalone_refused
from lib.repo_paths import REPO
from secrets_mgmt.cluster_vars_check import (
    FIX,
    SUBSTITUTE_OPT_OUT,
    Expected,
    ManifestEntry,
    SourceLine,
    Tree,
    check_tree,
    ciphertext_problems,
    ciphertexts,
    digest,
    is_dotenv,
    source_text,
)
from secrets_mgmt.rotation_tools import sops_set

Run = Callable[..., subprocess.CompletedProcess]

# The characters C3's value rule refuses in a substituted value. A newline, a double quote, a
# backslash and a control character make the implicit contract of today's raw embedding
# explicit. `{{` would open a second Go template action in Authelia's configuration once its
# template filter parses the whole file. A single `{` passes.
# An empty value is refused too, because the pinned SOPS writes it into a dotenv as a plain
# `key=` line, which `check` then fails as plaintext.
_FORBIDDEN = (
    ("nothing (SOPS leaves an empty dotenv value unencrypted)", lambda v: v == ""),
    ("a newline", lambda v: "\n" in v or "\r" in v),
    ("a double quote", lambda v: '"' in v),
    ("a backslash", lambda v: "\\" in v),
    ("a control character", lambda v: any(ord(c) < 32 or ord(c) == 127 for c in v)),
    ("the run `{{`", lambda v: "{{" in v),
)


class SopsFailed(RuntimeError):
    """A sops call exited non-zero. Its message names the file, never what sops printed."""


def fleet_expected() -> Expected:
    """The expectations of a placeholder render of every k8s render target.

    The renderer is imported here rather than at the top, so `--no-render` and
    `--ciphertext-only` never pay for loading Ansible.

    Raises:
        RenderError: A template fails to render.
    """
    from renderer.homelab_render import ansible_local_temp, secret_keys
    from renderer.renderer_lib.cluster import select_tags
    from renderer.renderer_lib.placeholders import Sentinels, render_placeholders
    from renderer.renderer_lib.sops_entries import SOPS_MANIFEST_KEYS

    keys = secret_keys()
    with ansible_local_temp():
        render = render_placeholders(select_tags(None), keys)
    referenced = frozenset().union(*render.key_sets.values()) - SOPS_MANIFEST_KEYS
    sentinels = Sentinels(keys)
    entries = tuple(
        ManifestEntry(
            tag, entry, text, sentinels.key_set({entry: text}) & SOPS_MANIFEST_KEYS
        )
        for tag, files in sorted(render.sops_manifests.items())
        for entry, text in sorted(files.items())
        if text is not None
    )
    standalone = frozenset().union(*render.standalone.values()) & referenced
    return Expected(referenced, standalone, entries)


def git_base(tree: Tree, ref: str | None) -> Callable[[Path], str | None]:
    """A reader of each file's base version: `ref`, or the merge base with origin/master.

    The merge base is the rule `scripts/dev/pr_changed_files.sh` applies in CI. The returned
    reader gives None for a file the base does not hold.
    """
    base = (
        ref or git("merge-base", "HEAD", "origin/master", cwd=tree.root).stdout.strip()
    )

    def read(path: Path) -> str | None:
        shown = git("show", f"{base}:{tree.rel(path)}", cwd=tree.root, check=False)
        return shown.stdout if shown.returncode == 0 else None

    return read


def _sops(
    tree: Tree, args: list[str], run: Run, label: str, stdin: str | None = None
) -> str:
    """Run `sops --config <tree's .sops.yaml> <args>` and return its stdout.

    Raises:
        SopsFailed: sops exited non-zero, timed out or did not start. The message names
            `label` and carries nothing sops printed, which can hold plaintext.
    """
    try:
        return run(
            ["sops", "--config", str(tree.sops_config), *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise SopsFailed(
            f"sops {args[0]} of {label} failed ({type(exc).__name__})"
        ) from None


def decrypt(tree: Tree, path: Path, run: Run) -> dict:
    """`path`'s plaintext, parsed from sops's JSON output into memory."""
    args = ["decrypt", "--output-type", "json", str(path)]
    return json.loads(_sops(tree, args, run, path.name))


def encrypt(tree: Tree, plaintext: str, target: Path, run: Run) -> str:
    """The ciphertext of `plaintext` as SOPS would write `target`, from stdin.

    `--filename-override` picks `target`'s creation rule, and nothing is written beside it.
    """
    kind = "dotenv" if is_dotenv(target) else "yaml"
    return _sops(
        tree,
        [
            "encrypt",
            "--filename-override",
            str(target),
            "--input-type",
            kind,
            "--output-type",
            kind,
            "/dev/stdin",
        ],
        run,
        target.name,
        stdin=plaintext,
    )


def value_refusals(values: dict, expected: Expected) -> list[str]:
    """Why each referenced value cannot be written, naming the key and never the value."""
    out = []
    for key in sorted(expected.keys):
        if key.startswith("sops_"):
            out.append(f"{key}: a `sops_` name, which check reads as SOPS metadata")
        elif key not in values:
            out.append(f"{key}: referenced by the render and absent from secrets.yml")
        elif isinstance(values[key], (dict, list)) or values[key] is None:
            out.append(f"{key}: not a scalar")
        else:
            text = str(values[key])
            out += [f"{key}: holds {name}" for name, hit in _FORBIDDEN if hit(text)]
            if key in expected.standalone and standalone_refused(text):
                out.append(
                    f"{key}: stands alone in a field, and Flux would read its text as "
                    "something other than a string (C3's standalone value rule)"
                )
    for entry in expected.manifests:
        for key in sorted(entry.keys - values.keys()):
            out.append(f"{key}: named by {entry.name} and absent from secrets.yml")
    return out


def build_manifest(entry: ManifestEntry, values: dict) -> dict:
    """The manifest `entry` describes, with each sentinel replaced by its value as a string.

    The sentinel render is parsed first and each value placed in the parsed strings, so no
    template runs on plaintext. Every value becomes a string, so SOPS records `type:str`.

    Raises:
        ValueError: A key's sentinel appears in no `data` or `stringData` string, so the
            template transforms it and `write` cannot place the value.
    """
    from renderer.renderer_lib.placeholders import sentinel

    obj = yaml_fast.safe_load(entry.text)
    placed = set()
    for field in ("data", "stringData"):
        for name, text in (obj.get(field) or {}).items():
            text = str(text)
            for key in entry.keys:
                if sentinel(key) in text:
                    text = text.replace(sentinel(key), str(values[key]))
                    placed.add(key)
            obj[field][name] = text
    if placed != entry.keys:
        raise ValueError(
            f"{entry.name}: the template does not hold {sorted(entry.keys - placed)} as "
            "plain text in a data or stringData value"
        )
    obj.setdefault("metadata", {}).setdefault("annotations", {})[SUBSTITUTE_OPT_OUT] = (
        "disabled"
    )
    return obj


def write(tree: Tree, expected: Expected, run: Run = subprocess.run) -> int:
    """Bring the cluster files in line with `secrets.yml` and `expected`; return the exit code.

    Args:
        tree: The tree to read and write.
        expected: The render's expectations.
        run: The subprocess runner for every sops call; a test binds its own environment.
    """
    try:
        values = decrypt(tree, tree.secrets, run)
    except SopsFailed as exc:
        print(f"cluster_vars write: {exc}. Nothing was written.", file=sys.stderr)
        return FAILED
    refusals = value_refusals(values, expected)
    manifests: dict[Path, dict] = {}
    for entry in expected.manifests:
        try:
            manifests[tree.manifests / entry.name] = build_manifest(entry, values)
        except ValueError as exc:
            refusals.append(str(exc))
    if refusals:
        print("cluster_vars write: refused, and nothing was written:", file=sys.stderr)
        print("\n".join(f"  {line}" for line in refusals), file=sys.stderr)
        return FAILED
    desired = {key: str(values[key]) for key in sorted(expected.keys)}
    try:
        written = _encrypt_all(tree, desired, manifests, run)
        current = (
            decrypt(tree, tree.cluster_file, run)
            if tree.cluster_file.is_file()
            else None
        )
        changed = _update_dotenv(tree, desired, current, run)
    except SopsFailed as exc:
        print(f"cluster_vars write: {exc}. No file was changed.", file=sys.stderr)
        return FAILED
    for path, text in written.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    stale = [p for p in tree.manifest_files() if p not in manifests]
    for path in stale:
        path.unlink()
    held = ciphertexts(tree.secrets.read_text())
    lines = {key: SourceLine(digest(held[key])) for key in desired}
    for entry in expected.manifests:
        for key in entry.keys:
            lines[key] = SourceLine(digest(held[key]), entry.name)
    text = source_text(lines)
    if not tree.source.is_file() or tree.source.read_text() != text:
        tree.source.write_text(text)
    report = [f"{verb} {', '.join(names)}" for verb, names in changed if names]
    report += [f"encrypted {', '.join(p.name for p in written)}"] if written else []
    report += [f"removed {', '.join(p.name for p in stale)}"] if stale else []
    print(f"cluster_vars write: {'; '.join(report) or 'no change'}")
    return OK


def _encrypt_all(
    tree: Tree, desired: dict[str, str], manifests: dict[Path, dict], run: Run
) -> dict[Path, str]:
    """The ciphertext of each file that must be written whole, all encrypted before any write.

    The cluster file when it is absent, and each manifest that is absent or whose decrypted
    plaintext differs from the one `secrets.yml` gives.
    """
    out = {}
    if not tree.cluster_file.is_file():
        body = "".join(f"{k}={v}\n" for k, v in desired.items())
        out[tree.cluster_file] = encrypt(tree, body, tree.cluster_file, run)
    for path, obj in manifests.items():
        if path.is_file() and _without_sops(decrypt(tree, path, run)) == obj:
            continue
        text = yaml.safe_dump(obj, sort_keys=False, default_flow_style=False)
        out[path] = encrypt(tree, text, path, run)
    return out


def _without_sops(obj: dict) -> dict:
    return {k: v for k, v in obj.items() if k != "sops"}


def _update_dotenv(
    tree: Tree, desired: dict[str, str], current: dict | None, run: Run
) -> list[tuple[str, list[str]]]:
    """`sops set` each changed key and `sops unset` each dropped one on the existing file.

    Every other key keeps its ciphertext. A failure part way restores the file's bytes.

    Raises:
        SopsFailed: A set or unset failed; the file is as it was.
    """
    if current is None:
        return []
    changed = [k for k, v in desired.items() if current.get(k) != v]
    dropped = sorted(k for k in current if k not in desired)
    original = tree.cluster_file.read_bytes()
    try:
        for key in changed:
            sops_set(key, desired[key], path=str(tree.cluster_file), run=run)
        for key in dropped:
            run(
                ["sops", "unset", str(tree.cluster_file), json.dumps([key])],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
    except OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired:
        tree.cluster_file.write_bytes(original)
        raise SopsFailed(
            f"sops set or unset failed on {tree.cluster_file.name}; it was restored"
        ) from None
    return [("set", changed), ("unset", dropped)]


def ciphertext_only(paths: list[Path]) -> list[str]:
    """The `ENC[` assertion alone, over SOPS files named one by one."""
    out = []
    for path in paths:
        out += ciphertext_problems(str(path), path.read_text(), is_dotenv(path))
    return out


def main(argv: list[str] | None = None, tree: Tree | None = None) -> int:
    """Parse `argv`, run the mode, and return its exit code."""
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("write", help="operator-only: decrypt and rewrite the cluster files")
    pc = sub.add_parser("check", help="assert the cluster files without decrypting")
    pc.add_argument(
        "--no-render",
        action="store_true",
        help="skip the assertions that need a render",
    )
    pc.add_argument(
        "--base",
        metavar="REF",
        help="compare each file with REF for the MAC proxy (default: the merge base of "
        "HEAD with origin/master)",
    )
    pc.add_argument(
        "--ciphertext-only",
        nargs="+",
        metavar="FILE",
        type=Path,
        help="assert only that each named SOPS file holds ciphertext; never a directory",
    )
    args = parser.parse_args(argv)
    tree = tree or Tree(REPO)
    if args.mode == "write":
        return write(tree, fleet_expected())
    if args.ciphertext_only:
        dirs = [str(p) for p in args.ciphertext_only if p.is_dir()]
        if dirs:
            parser.error(f"--ciphertext-only takes files, not the directories {dirs}")
        problems = ciphertext_only(args.ciphertext_only)
    else:
        expected = None if args.no_render else fleet_expected()
        problems = check_tree(tree, expected, git_base(tree, args.base))
    if problems:
        print("\n".join(problems + [FIX]), file=sys.stderr)
        return FAILED
    print("cluster_vars check: clean")
    return OK


if __name__ == "__main__":
    sys.exit(main())
