#!/usr/bin/env python3
"""The promote workflow's `images` job: push each rendered image context to GHCR once (D5.4a2).

`homelab-render cluster` writes each of the ten contexts `k8s/image-builder` builds to
`DIR/images/<name>/`, and each content tag to `DIR/images/tags.json` (D5.4a1,
`scripts/renderer/renderer_lib/images.py`). The `images` job downloads that render and runs:

- `plan OUT`, with no token. It recomputes each context's content tag from its files, by
  image-builder's own rule, and refuses a context whose tag differs from `tags.json`'s, a
  context `tags.json` does not name, and a name with no context.
- `push OUT`, after `docker login`. For each image it reads whether GHCR already serves
  `ghcr.io/danielh2018/<name>:<content tag>`. A served tag is success and is never built
  again: the push-once rule is what makes a content tag immutable, since two builds of one
  context are not byte-identical. An absent tag is built and pushed, then read back with a
  token fetched after the build, and must be served. Any other registry answer fails that image, so a refusal never
  reads as "absent". A failed image does not stop the others.

Stdlib only, so the job that holds `packages: write` installs nothing and imports no renderer
code. The plan is docs/target-architecture/deploy/d5-bulk-move.md, *Images built by CI (D5.4)*.

Run: python3 scripts/deploy_tools/ci_images.py plan out
     python3 scripts/deploy_tools/ci_images.py push out
"""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REGISTRY = "ghcr.io"
OWNER = "danielh2018"
# The repository each package links to, so later pushes from this repo's workflow token reach it.
SOURCE = "https://github.com/DanielH2018/server"
IMAGES_DIR = "images"
TAGS_FILE = "tags.json"
TAG_RE = re.compile(r"sha-[0-9a-f]{12}")
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
# The cluster nodes are amd64, as the in-cluster build is.
PLATFORM = "linux/amd64"
# Every manifest type a build can push, so a served tag never answers 404 for its type.
ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
ATTEMPTS = 3


class Refused(Exception):
    """The render's contexts and `tags.json` disagree, so nothing may be pushed."""


def content_tag(data: dict[str, str]) -> str:
    """`image_builder_content_tag` over a context's `data`, each file name mapped to its text.

    image-builder computes `sha-` + the first 12 hex of
    `data | dictsort | to_json | hash('sha256')`. `dictsort` orders keys case-insensitively,
    so `app.py` sorts before `Dockerfile`, and `to_json` is `json.dumps` with its defaults.
    """
    pairs = [[key, data[key]] for key in sorted(data, key=str.lower)]
    return "sha-" + hashlib.sha256(json.dumps(pairs).encode()).hexdigest()[:12]


def read_context(context: Path) -> dict[str, str]:
    """The `data` a context directory holds, each file name mapped to its text, byte for byte.

    Raises:
        Refused: The directory holds something other than files, or no Dockerfile.
    """
    data = {}
    for path in sorted(context.iterdir()):
        if not path.is_file() or path.is_symlink():
            raise Refused(f"{path} is not a regular file")
        data[path.name] = path.read_bytes().decode("utf-8")
    if "Dockerfile" not in data:
        raise Refused(f"{context} holds no Dockerfile")
    return data


def plan(out: Path) -> list[tuple[str, str]]:
    """Each image the render in `out` describes, as `(name, content tag)`, sorted by name.

    Raises:
        Refused: `out/images/` or `tags.json` is missing or malformed, the contexts and
            `tags.json` name different images, or a context's files hash to a tag other
            than `tags.json`'s.
    """
    images = out / IMAGES_DIR
    tags_file = images / TAGS_FILE
    if not tags_file.is_file():
        raise Refused(f"{tags_file} is missing, so the render wrote no image contexts")
    tags = json.loads(tags_file.read_text())
    if not isinstance(tags, dict) or not tags:
        raise Refused(f"{tags_file} is not a non-empty map of name to tag")
    contexts = {p.name for p in images.iterdir() if p.is_dir()}
    problems = [
        f"{images / name} has no entry in {TAGS_FILE}"
        for name in sorted(contexts - set(tags))
    ] + [
        f"{TAGS_FILE} names {name}, which has no context"
        for name in sorted(set(tags) - contexts)
    ]
    planned = []
    for name in sorted(contexts & set(tags)):
        tag = tags[name]
        if not NAME_RE.fullmatch(name):
            problems.append(f"{name!r} is not a lowercase image name")
        elif not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
            problems.append(
                f"{TAGS_FILE} gives {name} the tag {tag!r}, not sha-<12 hex>"
            )
        elif (computed := content_tag(read_context(images / name))) != tag:
            problems.append(
                f"{name}'s context hashes to {computed}, and {TAGS_FILE} names {tag}"
            )
        else:
            planned.append((name, tag))
    if problems:
        raise Refused("\n".join(problems))
    return planned


@dataclass(frozen=True)
class Answer:
    """The registry's answer for one tag: the HTTP status, and the digest when it is served."""

    status: int
    digest: str = ""


class Registry:
    """Reads GHCR's manifests for this owner's packages with the workflow's own token."""

    def __init__(self, actor: str, password: str) -> None:
        self._basic = base64.b64encode(f"{actor}:{password}".encode()).decode()

    def _open(
        self, request: urllib.request.Request
    ) -> tuple[int, dict[str, str], bytes]:
        """One request, retried on a network error, a 429 or a 5xx."""
        for attempt in range(1, ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request, timeout=30) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500 or attempt == ATTEMPTS:
                    return exc.code, dict(exc.headers), b""
            except urllib.error.URLError:
                if attempt == ATTEMPTS:
                    raise
            time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def _token(self, name: str) -> str:
        """A bearer token for `name`, scoped to push so a package not yet created answers 404.

        Never cached: the read-back follows a build that can outlast a token issued before it.
        """
        scope = f"repository:{OWNER}/{name}:pull,push"
        status, _, body = self._open(
            urllib.request.Request(
                f"https://{REGISTRY}/token?scope={scope}&service={REGISTRY}",
                headers={"Authorization": f"Basic {self._basic}"},
            )
        )
        token = json.loads(body).get("token", "") if status == 200 else ""
        if not token:
            raise RuntimeError(
                f"{REGISTRY} issued no push token for {name} (HTTP {status})"
            )
        return token

    def lookup(self, name: str, tag: str) -> Answer:
        status, headers, _ = self._open(
            urllib.request.Request(
                f"https://{REGISTRY}/v2/{OWNER}/{name}/manifests/{tag}",
                method="HEAD",
                headers={
                    "Authorization": f"Bearer {self._token(name)}",
                    "Accept": ACCEPT,
                },
            )
        )
        lowered = {k.lower(): v for k, v in headers.items()}
        return Answer(
            status, lowered.get("docker-content-digest", "") if status == 200 else ""
        )


def ref(name: str, tag: str) -> str:
    return f"{REGISTRY}/{OWNER}/{name}:{tag}"


def docker_build(name: str, tag: str, context: Path) -> str:
    """Build `context` and push it as `name:tag`, returning the pushed digest, or "" on failure.

    The local image and build cache are pruned afterwards, so ten builds fit on one runner.
    """
    with tempfile.TemporaryDirectory() as tmp:
        metadata = Path(tmp) / "metadata.json"
        result = subprocess.run(
            [
                "docker", "buildx", "build",
                "--platform", PLATFORM,
                "--provenance=false",
                "--label", f"org.opencontainers.image.source={SOURCE}",
                "--tag", ref(name, tag),
                "--metadata-file", str(metadata),
                "--push",
                str(context),
            ],
            check=False,
        )  # fmt: skip
        digest = ""
        if result.returncode == 0 and metadata.is_file():
            digest = json.loads(metadata.read_text()).get("containerimage.digest", "")
    subprocess.run(["docker", "system", "prune", "--all", "--force"], check=False)
    return digest


def push(
    out: Path,
    lookup: Callable[[str, str], Answer],
    build: Callable[[str, str, Path], str],
) -> list[str]:
    """Push each image of `out`'s render whose content tag GHCR does not serve.

    Returns:
        One problem per image that failed, empty when every tag is served at the end.

    Raises:
        Refused: `plan` refuses the render, so nothing is looked up or built.
    """
    problems = []
    for name, tag in plan(out):
        try:
            before = lookup(name, tag)
        except (OSError, RuntimeError, ValueError) as exc:
            problems.append(f"{ref(name, tag)}: the registry read failed: {exc}")
            continue
        if before.status == 200:
            print(f"{ref(name, tag)} is already served at {before.digest}; not rebuilt")
            continue
        if before.status != 404:
            problems.append(
                f"{ref(name, tag)}: the registry answered HTTP {before.status}, "
                "so whether the tag exists is unknown and nothing is pushed"
            )
            continue
        print(f"::group::build {ref(name, tag)}", flush=True)
        digest = build(name, tag, out / IMAGES_DIR / name)
        print("::endgroup::", flush=True)
        if not DIGEST_RE.fullmatch(digest):
            problems.append(f"{ref(name, tag)}: the build or its push failed")
            continue
        try:
            after = lookup(name, tag)
        except (OSError, RuntimeError, ValueError) as exc:
            problems.append(f"{ref(name, tag)}: the read-back failed: {exc}")
            continue
        # DECIDED: the read-back requires that GHCR serves the tag, not that it serves the
        # build metadata's digest. The job builds on the runner's default `docker` driver,
        # whose `containerimage.digest` can be the config digest rather than the manifest
        # digest GHCR serves. docker/setup-buildx-action would move the build cache out of
        # reach of the `docker system prune` that lets ten builds fit on one runner.
        if after.status != 200 or not DIGEST_RE.fullmatch(after.digest):
            problems.append(
                f"{ref(name, tag)}: pushed {digest}, but the registry answers HTTP "
                f"{after.status} at {after.digest or 'no digest'}"
            )
            continue
        print(f"pushed {ref(name, tag)}; GHCR serves it at {after.digest}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("plan", "push"))
    parser.add_argument("out", type=Path, help="the render's output directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            for name, tag in plan(args.out):
                print(f"{name} {tag}")
            return 0
        registry = Registry(os.environ["GH_ACTOR"], os.environ["GITHUB_TOKEN"])
        problems = push(args.out, registry.lookup, docker_build)
    except Refused as exc:
        print(f"::error::{exc}".replace("\n", "%0A"), file=sys.stderr)
        return 1
    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
