"""Hash a render tree and count its stub tokens: the library behind `shadow_diff.py digest`.

- `digest_lines` hashes each directory under `cluster/`, and the whole tree. R1's
  determinism tests write the same lines with `digest_line` and `directory_digests`.
- `stub_tokens` counts each stub token in a file, for the bundle gates.

`docs/homelab-render.md` is the operator's page.
"""

import base64
import binascii
import hashlib
import os
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import yaml

from lib import yaml_fast

CLUSTER = "cluster"
# A stub token, as `homelab_secret_stub` builds it.
STUB_TOKEN = re.compile(r"stub-[a-z0-9-]+")


class TreeRefused(ValueError):
    """A directory that is not a C1 tree holding at least one file."""


def _files_under(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*") if p.is_file())


# -- stub tokens --------------------------------------------------------------------------


def _secret_data_values(text: str) -> Iterable[str]:
    """The decoded `data` values of each `kind: Secret` document in `text`.

    A file that does not parse, or a value that is not base64, yields nothing; its raw text is
    still searched.
    """
    try:
        docs = list(yaml_fast.safe_load_all(text))
    except yaml.YAMLError:
        return
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") != "Secret":
            continue
        for value in (doc.get("data") or {}).values():
            try:
                yield base64.b64decode(str(value), validate=True).decode(
                    "utf-8", errors="replace"
                )
            except binascii.Error, ValueError:
                continue


def stub_tokens(data: bytes) -> Counter[str]:
    """How often each stub token occurs in a file, with its Secret documents' `data` decoded."""
    text = data.decode("utf-8", errors="replace")
    tokens = Counter(STUB_TOKEN.findall(text))
    for value in _secret_data_values(text):
        tokens.update(STUB_TOKEN.findall(value))
    return tokens


# -- digest -------------------------------------------------------------------------------


def files_digest(base: Path) -> str:
    """A sha256 over every file under `base`: each relative path and its bytes.

    Paths sort bytewise and each field is length-prefixed, so no line depends on the absolute
    directory or the locale, and no path and content can run together.
    """
    digest = hashlib.sha256()
    entries = sorted(
        (os.fsencode(p.relative_to(base).as_posix()), p) for p in _files_under(base)
    )
    for rel, path in entries:
        data = path.read_bytes()
        for part in (rel, data):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()


def digest_line(name: str, hexdigest: str) -> str:
    """One digest line, the format CI's job summary and `digest` share."""
    return f"{name} sha256:{hexdigest}"


def directory_digests(root: Path) -> dict[str, str]:
    """Each directory under `root/cluster/` mapped to its `files_digest`, in bytewise order.

    A directory's line depends on its own files alone, so a render of some tags gives the same
    lines for those tags as a render of all of them.
    """
    cluster = root / CLUSTER
    dirs = sorted(
        (p for p in cluster.iterdir() if p.is_dir()), key=lambda p: os.fsencode(p.name)
    )
    return {d.name: files_digest(d) for d in dirs}


def digest_lines(root: Path) -> list[str]:
    """`digest`'s output: one line per directory under `root/cluster/`, then `total`.

    Raises:
        TreeRefused: `root` holds no `cluster/` directory with a file in it.
    """
    if not (root / CLUSTER).is_dir() or not _files_under(root / CLUSTER):
        raise TreeRefused(f"{root} holds no {CLUSTER}/ directory with a file in it")
    lines = [digest_line(name, h) for name, h in directory_digests(root).items()]
    return [*lines, digest_line("total", files_digest(root))]
