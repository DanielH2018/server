"""janitorr's Flux verify Job: it is dry when it should be, reaches the apps, and its links resolve.

`templates/verify-inert/job.yaml.j2` runs this, and nothing renders that directory while
Ansible owns janitorr. janitorr's flip PR renames it to `templates/verify/`, and the renderer
then emits the Job into `janitorr-verify` (target slice D5, *A verify conversion lands as inert
code*). It replaces the three proofs of `tasks/verify.yml`, each of which ran through
`kubectl exec`, with no `pods/exec` grant:

- dry-run: `tasks/verify.yml` grepped `/config/application.yml` inside janitorr's pod. The Job
  mounts the same key of the same `janitorr-config` Secret at the same path. The pod's subPath
  copy is frozen at its start, and the Job runs only after a revision that rolled the pod is
  Ready, so the two hold the same bytes. A dry-run line that is not `true` fails only when
  `JANITORR_EXPECT_DRY_RUN` is `true`, as before; a config with no dry-run line fails always.
- reach: `tasks/verify.yml` opened a TCP connection to sonarr, radarr and jellyfin from inside
  janitorr's pod. The Job dials each over HTTP from its own pod, `app: janitorr-verify`, and any
  HTTP answer counts, as an accepted connection did. The Job census admits no `socket`, so the
  connection goes through `flux_job_connect`, which retries a refusal for 60 seconds while
  kube-router learns the new pod's address.
- leaving-soon: `tasks/verify.yml` wrote a probe symlink from janitorr's pod and read it from
  jellyfin's. The Job writes and reads it in one container whose media mounts are jellyfin's,
  read-write rather than read-only. janitorr's whole claim at `/data` and jellyfin's two
  `media` subPath mounts at `/data` and `/data/media` both put the library's `media/tv` at
  `/data/media/tv`, so the target string and the link's place on disk are the ones janitorr
  writes. `tests/test_verify_janitorr.py` holds the three mount layouts and the configured
  leaving-soon directory to that. The probe is removed in a `finally`; a pod killed at its
  deadline skips that, so each run first removes a probe a killed run left behind.

Environment:
    JANITORR_CONFIG          the mounted `application.yml`.
    JANITORR_EXPECT_DRY_RUN  `true` when the role renders janitorr dry.
    JANITORR_TARGETS         the URLs to dial, separated by spaces.
    MEDIA_ROOT               where jellyfin's layout puts the library, `/data`.

Exit 0 when every check passes, 1 when one fails, naming it.

Run: python3 verify_janitorr.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/janitorr/tests/test_verify_janitorr.py
"""

import argparse
import os
import re
import sys
import urllib.error
from collections.abc import Callable, Mapping
from pathlib import Path

from flux_job_connect import open_url, refused

# The socket timeout of each request, in seconds.
TIMEOUT = 10
# The probe's name, as `tasks/verify.yml` named it: sorted last, and unmistakable in a listing.
PROBE_NAME = "zz-k8s-verify-probe.mkv"
# How much of the target the read check reads, as `tasks/verify.yml`'s `head -c 1024` did.
READ_BYTES = 1024
# The top-level `dry-run:` key under `application:`, two spaces in.
DRY_RUN_LINE = re.compile(r"^  dry-run:\s*(\S+)", re.M)


class Failed(Exception):
    """A check failed; the message names what is wrong."""


def dry_run(config: str, expect_dry: bool) -> str:
    """Reads the dry-run flag janitorr's config holds, and fails if it should be dry and is not.

    Raises:
        Failed: The config has no dry-run line, or it is not `true` while dry is expected.
    """
    try:
        text = Path(config).read_text(encoding="utf-8")
    except OSError as err:
        raise Failed(f"cannot read janitorr's config at {config}: {err}") from err
    match = DRY_RUN_LINE.search(text)
    if not match:
        raise Failed(f"{config} has no `  dry-run:` line, so its mode is unknown")
    value = match.group(1)
    if expect_dry and value != "true":
        raise Failed(
            f"janitorr should run dry, but {config} says `dry-run: {value}`: it would "
            "delete real media"
        )
    return f"dry-run ok: janitorr's config says `dry-run: {value}`"


def reach(url: str, opener: Callable = open_url) -> str:
    """Proves `url` answers this pod; any HTTP answer counts.

    Raises:
        Failed: The connect was still refused after the helper's retries, or failed otherwise.
    """
    try:
        with opener(url, timeout=TIMEOUT):
            pass
    except urllib.error.HTTPError as err:
        err.close()
    except OSError as err:
        why = (
            "the app is down, or its fence no longer admits janitorr-verify"
            if refused(err)
            else "the connection failed after it was accepted"
        )
        raise Failed(f"UNREACHABLE: {url} ({err}): {why}") from err
    return f"reach ok: {url} answers"


def first_episode(tv_dir: Path) -> Path:
    """Returns the path of the first `.mkv` under `tv_dir`, walking in sorted order.

    Raises:
        Failed: The tree holds no `.mkv`, so there is nothing to link to.
    """
    for root, dirs, files in os.walk(tv_dir):
        dirs.sort()
        for name in sorted(files):
            if name.endswith(".mkv"):
                return Path(root) / name
    raise Failed(f"no .mkv under {tv_dir}, so no probe link can be built")


def leaving_soon(media_root: str) -> list[str]:
    """Builds the link janitorr would build, then proves it resolves and reads, and removes it.

    Raises:
        Failed: The link is not a symlink, its target does not resolve, or it cannot be read.
    """
    media = Path(media_root) / "media"
    target = first_episode(media / "tv")
    probe = media / "leaving-soon" / "tv" / PROBE_NAME
    lines = []
    if probe.is_symlink() or probe.exists():
        probe.unlink()
        lines.append(f"removed a probe a killed run left at {probe}")
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.symlink_to(target)
    try:
        if not probe.is_symlink():
            raise Failed(f"{probe} is not a symlink after writing it")
        if probe.readlink() != target:
            raise Failed(f"{probe} points at {probe.readlink()}, not {target}")
        if not probe.exists():
            raise Failed(
                f"{probe} -> {target} does not resolve in jellyfin's mount layout, so every "
                "leaving-soon entry would be a link jellyfin lists and cannot play"
            )
        try:
            with probe.open("rb") as handle:
                handle.read(READ_BYTES)
        except OSError as err:
            raise Failed(f"{probe} resolves but cannot be read: {err}") from err
    finally:
        probe.unlink()
    lines.append(f"leaving-soon ok: {probe} -> {target} resolves and reads; removed")
    return lines


def run(env: Mapping[str, str], opener: Callable = open_url) -> list[str]:
    """Runs the three checks in order and returns their report lines.

    Raises:
        Failed: The first check that failed.
    """
    expect = env.get("JANITORR_EXPECT_DRY_RUN", "")
    if expect not in ("true", "false"):
        raise Failed(f"JANITORR_EXPECT_DRY_RUN is {expect!r}, not true or false")
    targets = env.get("JANITORR_TARGETS", "").split()
    if not targets:
        raise Failed("JANITORR_TARGETS names no URL to dial")
    lines = [dry_run(env["JANITORR_CONFIG"], expect == "true")]
    lines += [reach(url, opener) for url in targets]
    lines += leaving_soon(env["MEDIA_ROOT"])
    return lines


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        lines = run(env)
    except (Failed, OSError, KeyError) as err:
        print(f"janitorr-verify: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"janitorr-verify: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
