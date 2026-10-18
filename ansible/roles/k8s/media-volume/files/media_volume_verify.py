"""media-volume's verify Job: prove /data/torrents and /data/media are one filesystem.

Inert until media-volume's flip: `templates/verify-inert/` holds the Job, and the flip PR moves
it to `templates/verify/`, where the renderer emits it into `media-volume-verify` (target slice
D5, *A verify conversion lands as inert code*). It replaces the hardlink probe Job that
`tasks/main.yml` applies on every deploy, and makes the same check: a file in torrents/ is
hardlinked into media/, and the link count must read 2.

sonarr and radarr fall back to COPYING when an import crosses a filesystem boundary, with no
error and no warning. So a split volume shows up only as double the space, months later. With
one claim mounted once, the link cannot fail today. The check is a regression guard for the
day someone gives torrents and media separate volumes. Its first run also proves the static PV
binds, its nodeAffinity resolves, and the media stack's uid can write to it.

The Job reaches no Service and no API, so it opens no connection and imports no
`flux_job_connect`. `os.link` is a syscall, not a process, which the Job census allows. Inputs
come from the Job's environment: `VERIFY_DATA`, the claim's mount. `MEDIA_VOLUME_RENDER_DIGEST8`
is in the spec only to rename the Job, and the script prints it. The probe file is named after
the pod's `HOSTNAME`, so two runs never collide. Exit 0 when the link holds, 1 when it does not,
naming why.

Run: python3 media_volume_verify.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/media-volume/tests/test_media_volume_verify.py
"""

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

# errno.EXDEV on Linux. The Job census's ALLOWED_IMPORTS holds no `errno`.
EXDEV = 18


# The link call, a seam for the tests: they pass a link that fails as two filesystems do.
Link = Callable[[Path, Path], None]


class Failed(Exception):
    """The check failed; the message names why."""


def check_hardlink(data: Path, probe: str, link: Link = os.link) -> str:
    """Hardlink a file from `data`/torrents into `data`/media and read its link count.

    Both directories are created when absent, as the probe this replaces did, so a volume
    nothing has written to yet still gets a verdict. Each directory's times are put back
    afterwards: a verify Job leaves the library as it found it, and creating and deleting a
    file would otherwise move both directories' mtimes on every run.
    """
    if not data.is_dir():
        raise Failed(f"the volume {data} is not mounted")
    torrents, media = data / "torrents", data / "media"
    try:
        torrents.mkdir(exist_ok=True)
        media.mkdir(exist_ok=True)
        times = {d: os.stat(d) for d in (torrents, media)}
        source, target = torrents / probe, media / probe
        source.write_text("probe\n")
    except OSError as err:
        raise Failed(f"the volume {data} is not writable: {err.strerror}") from err
    try:
        try:
            link(source, target)
        except OSError as err:
            if err.errno == EXDEV:
                raise Failed(
                    f"{torrents} and {media} are two filesystems, so imports become copies"
                ) from err
            raise Failed(f"cannot hardlink into {media}: {err.strerror}") from err
        links = os.stat(target).st_nlink
    finally:
        for path in (source, target):
            path.unlink(missing_ok=True)
        for d, st in times.items():
            os.utime(d, ns=(st.st_atime_ns, st.st_mtime_ns))
    if links != 2:
        raise Failed(f"the hardlink's link count is {links}, expected 2")
    return f"{torrents} <-> {media} are one filesystem"


def run(env: Mapping[str, str], link: Link = os.link) -> list[str]:
    """Run the check, and return its report lines.

    Raises:
        Failed: The check failed.
    """
    probe = f".media-volume-verify-{env.get('HOSTNAME', 'probe')}"
    return [
        f"verifying media-volume's render {env.get('MEDIA_VOLUME_RENDER_DIGEST8', 'unknown')}",
        check_hardlink(Path(env["VERIFY_DATA"]), probe, link),
    ]


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    link: Link = os.link,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    try:
        lines = run(os.environ if env is None else env, link)
    except Failed as err:
        print(f"media-volume-verify: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"media-volume-verify: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
