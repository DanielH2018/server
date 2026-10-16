#!/usr/bin/env python3
"""Vendor an upstream manifest into a role, pinned to the sha256 of the bytes it fetched.

A role that applies an upstream manifest through Flux keeps the file byte for byte under
`files/upstream/` (H1, *Vendored upstream manifests*), because the renderer reads only the git
tree. Its `defaults/main.yml` pins each file in a list whose key ends `vendored_manifests`:

    metallb_vendored_manifests:
      - name: metallb-native.yaml        # the file under files/upstream/; first key of the item
        url: "https://.../{{ k3s_metallb_version }}/config/manifests/metallb-native.yaml"
        fetched_url: "https://.../v0.16.0/config/manifests/metallb-native.yaml"
        sha256: "<hex>"

`url` renders from the same defaults file, so a Renovate bump of the version key changes it.
`fetched_url` and `sha256` record what the tool last fetched. A bump that nobody refreshed
therefore leaves `url` and `fetched_url` apart, and `check_tree` names that as well as a file
whose bytes differ from `sha256`. `ansible/tests/k8s/test_vendored_manifests_match_their_pins.py`
runs `check_tree` over the real tree.

The item also matches `asset_pins.py`'s list shape (`url` beside `sha256`), so its fetch-and-check
covers these pins too. Its `--refresh` rewrites the digest only, which leaves the file and
`fetched_url` behind and fails the check; refresh a vendored pin with this script instead.

The refresh fetches the rendered `url`, writes the file, and rewrites `fetched_url` and `sha256`
in the item. When the rendered URL equals `fetched_url`, the download must hash to the pinned
sha256, or nothing is written: upstream changed bytes under a URL the pin already names. When
the URL moved, the first download is trusted, as `asset_pins.py --refresh` trusts it, because
upstream publishes no checksum for these files. Either way it prints each object the old file
held and the new one lacks, keyed by API group, kind, namespace and name, so a version
graduation is not a removal. With `prune: false` on the plumbing tags such an object stays
live, and the bump PR names how it is removed.

No test fetches anything; the suite runs under `-p leakguard`. Run the refresh by hand.

Usage:
    uv run python scripts/dev/vendor_manifest.py metallb             # refresh every file
    uv run python scripts/dev/vendor_manifest.py longhorn longhorn.yaml
    uv run python scripts/dev/vendor_manifest.py --check             # offline, every role

Exit codes: 0 refreshed or every pin holds; 1 a fetch failed, a download contradicted its pin,
or `--check` found a problem; 64 a role or file name with no pin.
"""

import hashlib
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path as _Path

# `lib` and `validate` are siblings of this directory: a directly-invoked script gets only its
# own directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import jinja2

from lib import yaml_fast
from lib.cli_help import ArgumentParser, error
from lib.exit_codes import FAILED, OK, USAGE_ERROR
from lib.repo_paths import ROLES
from validate.asset_pins import Fetcher, fetch

KEY_SUFFIX = "vendored_manifests"
UPSTREAM = ("files", "upstream")


@dataclass(frozen=True)
class Vendored:
    """One pinned upstream file of one role, as its defaults record it."""

    role: _Path
    key: str  # the `*vendored_manifests` defaults key whose list holds this item
    name: str
    url: str
    fetched_url: str
    sha256: str

    @property
    def path(self) -> _Path:
        return self.role.joinpath(*UPSTREAM, self.name)


def _render(template: str, context: dict) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    return env.from_string(template).render(context)


def pins_of(role: _Path) -> list[Vendored]:
    """The vendored pins in `role`'s defaults, with `url` rendered from that file alone.

    Raises:
        ValueError: an item lacks `name` or `url`, or its `url` does not render.
    """
    defaults_file = role / "defaults" / "main.yml"
    if not defaults_file.is_file():
        return []
    defaults = yaml_fast.safe_load(defaults_file.read_text()) or {}
    pins = []
    for key, items in defaults.items():
        if not key.endswith(KEY_SUFFIX):
            continue
        for item in items or []:
            if (
                not isinstance(item, dict)
                or not item.get("name")
                or not item.get("url")
            ):
                raise ValueError(f"{key}: an item lacks `name` or `url`: {item!r}")
            try:
                url = _render(str(item["url"]), defaults)
            except jinja2.TemplateError as exc:
                raise ValueError(
                    f"{key}: {item['name']}: url does not render: {exc}"
                ) from exc
            pins.append(
                Vendored(
                    role,
                    key,
                    str(item["name"]),
                    url,
                    str(item.get("fetched_url") or ""),
                    str(item.get("sha256") or "").lower(),
                )
            )
    return pins


def check_role(role: _Path) -> list[str]:
    """Every way `role`'s vendored files and their pins disagree, one line each."""
    upstream = role.joinpath(*UPSTREAM)
    files = (
        {p.name for p in upstream.iterdir() if p.is_file()}
        if upstream.is_dir()
        else set()
    )
    try:
        pins = pins_of(role)
    except ValueError as exc:
        return [f"{role.name}: {exc}"]
    problems = [
        f"{role.name}: {name} sits in files/upstream/ with no pin in defaults"
        for name in sorted(files - {pin.name for pin in pins})
    ]
    for pin in pins:
        label = f"{role.name}: {pin.name}"
        if pin.name not in files:
            problems.append(f"{label} is pinned but files/upstream/ does not hold it")
            continue
        actual = hashlib.sha256(pin.path.read_bytes()).hexdigest()
        if actual != pin.sha256:
            problems.append(
                f"{label} hashes {actual}, the pin records {pin.sha256 or 'none'}"
            )
        if pin.url != pin.fetched_url:
            problems.append(
                f"{label}: url renders {pin.url} but the file was fetched from "
                f"{pin.fetched_url or 'nowhere'}"
            )
    return [f"{p}; run scripts/dev/vendor_manifest.py {role.name}" for p in problems]


def roles_in(roles: _Path = ROLES) -> list[_Path]:
    """Every role directory under `<roles>/<plane>/`."""
    return sorted(p for p in roles.glob("*/*") if p.is_dir())


def check_tree(roles: _Path = ROLES) -> list[str]:
    """`check_role` over every role under `roles`."""
    return [problem for role in roles_in(roles) for problem in check_role(role)]


def _object_keys(text: bytes) -> set[tuple[str, str, str, str]]:
    """(API group, kind, namespace, name) of each document in a manifest stream."""
    keys = set()
    for doc in yaml_fast.safe_load_all(text):
        if not isinstance(doc, dict) or "kind" not in doc:
            continue
        api = str(doc.get("apiVersion", ""))
        meta = doc.get("metadata") or {}
        group = api.rsplit("/", 1)[0] if "/" in api else ""
        keys.add(
            (group, doc["kind"], meta.get("namespace") or "", meta.get("name") or "")
        )
    return keys


def removed_objects(old: bytes, new: bytes) -> list[str]:
    """Each object `old` declares and `new` does not, as `Kind.group namespace/name`."""
    return [
        f"{kind}{'.' + group if group else ''} {namespace + '/' if namespace else ''}{name}"
        for group, kind, namespace, name in sorted(
            _object_keys(old) - _object_keys(new)
        )
    ]


def _rewrite_item(text: str, key: str, name: str, values: dict[str, str]) -> str:
    """Set `values` in `key`'s list item whose first key is `name: <name>`, keeping comments.

    Only the item under the top-level `key` is touched, so another list in the same file
    whose item carries the same name is left alone.
    """
    lines = text.splitlines(keepends=True)
    owner = next(
        (i for i, line in enumerate(lines) if re.match(rf"{re.escape(key)}:", line)),
        None,
    )
    if owner is None:
        raise ValueError(f"no top-level `{key}:` in the defaults")
    end = next(
        (
            i
            for i in range(owner + 1, len(lines))
            # The next top-level key ends the list; a `-` at column 0 is still its item.
            if re.match(r"[^\s#-]", lines[i])
        ),
        len(lines),
    )
    start = next(
        (
            i
            for i in range(owner + 1, end)
            if re.fullmatch(
                rf"\s*- name: [\"']?{re.escape(name)}[\"']?\s*(#.*)?\s*", lines[i]
            )
        ),
        None,
    )
    if start is None:
        raise ValueError(f"no `- name: {name}` item under `{key}:` in the defaults")
    body_indent = lines[start].index("-") + 2
    pending = dict(values)
    for i in range(start + 1, end):
        stripped = lines[i].lstrip(" ")
        if stripped.strip() and len(lines[i]) - len(stripped) < body_indent:
            break
        match = re.match(r"(\s*)(\w+):", lines[i])
        if match and match.group(2) in pending and len(match.group(1)) == body_indent:
            lines[i] = (
                f'{match.group(1)}{match.group(2)}: "{pending.pop(match.group(2))}"\n'
            )
    if pending:
        raise ValueError(f"the `{name}` item has no {', '.join(sorted(pending))} key")
    return "".join(lines)


def refresh(pin: Vendored, fetcher: Fetcher = fetch) -> tuple[bool, list[str]]:
    """Fetch `pin.url`, then write the file and its pin; (ok, report lines)."""
    try:
        status, chunks = fetcher(pin.url)
        body = b"".join(chunks) if status == 200 else b""
    except OSError as exc:
        return False, [f"{pin.name}: fetch failed: {exc}"]
    if status != 200:
        return False, [f"{pin.name}: HTTP {status} from {pin.url}"]
    actual = hashlib.sha256(body).hexdigest()
    if pin.url == pin.fetched_url and actual != pin.sha256:
        return False, [
            f"{pin.name}: {pin.url} now hashes {actual}, but the pin for that same URL records "
            f"{pin.sha256}. Upstream changed the bytes under a pinned URL; nothing was written"
        ]
    defaults_file = pin.role / "defaults" / "main.yml"
    try:
        # Rewrite the defaults in memory first, so an item the rewrite cannot find leaves
        # both files as they were.
        defaults = _rewrite_item(
            defaults_file.read_text(),
            pin.key,
            pin.name,
            {"fetched_url": pin.url, "sha256": actual},
        )
    except ValueError as exc:
        return False, [f"{pin.name}: {exc}; nothing was written"]
    old = pin.path.read_bytes() if pin.path.is_file() else b""
    pin.path.parent.mkdir(parents=True, exist_ok=True)
    pin.path.write_bytes(body)
    defaults_file.write_text(defaults)
    report = [f"{pin.name}: sha256 {actual} from {pin.url}"]
    report += [
        f"{pin.name}: removed upstream: {obj}" for obj in removed_objects(old, body)
    ]
    return True, report


def _select(role: _Path, names: Iterable[str]) -> list[Vendored] | None:
    pins = pins_of(role)
    wanted = set(names)
    chosen = [pin for pin in pins if not wanted or pin.name in wanted]
    if not chosen or wanted - {pin.name for pin in chosen}:
        return None
    return chosen


def main(
    argv: list[str] | None = None, roles: _Path = ROLES, fetcher: Fetcher = fetch
) -> int:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("role", nargs="?", help="the role to refresh, such as metallb")
    parser.add_argument(
        "names", nargs="*", help="files under files/upstream/ (default: all)"
    )
    parser.add_argument("--check", action="store_true", help="check every role offline")
    args = parser.parse_args(argv)
    if args.check:
        problems = check_tree(roles)
        for problem in problems:
            print(f"[FAIL] {problem}", file=sys.stderr)
        return FAILED if problems else OK
    if not args.role:
        parser.error("name a role, or pass --check")
    matches = [r for r in roles_in(roles) if r.name == args.role]
    try:
        chosen = _select(matches[0], args.names) if len(matches) == 1 else None
    except ValueError as exc:
        error(f"{args.role}: {exc}")
        return FAILED
    if chosen is None:
        error(f"{args.role} {' '.join(args.names)}: no matching vendored pin".strip())
        return USAGE_ERROR
    ok = True
    for pin in chosen:
        done, report = refresh(pin, fetcher)
        ok &= done
        print("\n".join(report), file=sys.stdout if done else sys.stderr)
    return OK if ok else FAILED


if __name__ == "__main__":
    sys.exit(main())
