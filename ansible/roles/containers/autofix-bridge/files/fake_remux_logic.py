"""Pure decision core for the fake-remux host scan (fake_remux_scan.py).

Split from the I/O shell so it stays stdlib-only + host-Python-floor clean (runs under the deploy
host's /usr/bin/python3, currently 3.12 — see ansible/tests/test_host_scripts_py312.py) and fully
unit-testable without docker/HTTP. The shell reads Sonarr's library, runs ffprobe inside jellyfin,
and hands the results here to decide which "remux"-quality files are actually re-encodes.

The signal (definitive, codec/resolution/size-independent — the generalization of the old codec
heuristic in autofix.py): a genuine Blu-ray remux preserves the source video stream, so its
per-stream ENCODER tag is never a consumer re-encoder and its GOP stays short (~1-2 s). A file that
CLAIMS a remux quality but whose video stream was re-encoded gives itself away two ways —
  - the ENCODER tag names a re-encoder (x264/x265/*_qsv/*_nvenc/Lavc/handbrake …), the cheap
    metadata-only tell; or
  - the keyframe interval (GOP) exceeds a threshold real remuxes never reach.
The NTRX "BD Remux 1080p AVC" that shipped a 10.4 s-GOP hevc_qsv re-encode (2026-07-16) trips both.
"""

from __future__ import annotations

# Consumer re-encoder identifiers that appear in a video stream's ENCODER tag. A remux (stream copy)
# preserves the disc's original tag (usually none), never one of these. Matched case-insensitively as
# substrings. `lavc` (libavcodec) covers any generic ffmpeg encode; `x264`/`x265` the CLI encoders;
# the `*_qsv`/`*_nvenc`/`*_amf`/`*_vaapi` set the hardware encoders.
DEFAULT_RE_ENCODER_MARKERS = (
    "x264",
    "x265",
    "libx264",
    "libx265",
    "hevc_qsv",
    "h264_qsv",
    "hevc_nvenc",
    "h264_nvenc",
    "hevc_amf",
    "h264_amf",
    "hevc_vaapi",
    "h264_vaapi",
    "lavc",
    "handbrake",
)


def sanitize(s, maxlen: int = 160) -> str:
    """Neutralize adversary-controlled text (release paths, encoder tags) before it enters a
    Discord-bound string: collapse whitespace, defuse @mentions/backticks, cap length."""
    s = "?" if s is None else str(s)
    s = " ".join(s.split())
    s = s.replace("@", "(at)").replace("`", "'")
    if len(s) > maxlen:
        s = s[: maxlen - 3] + "..."
    return s


def is_remux_quality(quality_name) -> bool:
    return "remux" in (quality_name or "").lower()


def remux_candidates(episodefiles, series_title):
    """The remux-quality files in one series' /api/v3/episodefile list, flattened to the fields the
    ffprobe pass + report + delete need. `path` is the ABSOLUTE library path (Sonarr mounts
    containers/data at /data; jellyfin mounts the same tree at /data/media, so this path resolves
    unchanged inside jellyfin for ffprobe). Files with no path are skipped (nothing to probe)."""
    out = []
    for ef in episodefiles:
        quality = ((ef.get("quality") or {}).get("quality") or {}).get("name")
        if not is_remux_quality(quality):
            continue
        path = ef.get("path")
        if not path:
            continue
        out.append(
            {
                "fileId": ef.get("id"),
                "seriesId": ef.get("seriesId"),
                "seriesTitle": series_title,
                "path": path,
                "relativePath": ef.get("relativePath") or path,
                "quality": quality,
                "codec": (ef.get("mediaInfo") or {}).get("videoCodec"),
            }
        )
    return out


def parse_encoder_tag(ffprobe_stream_json):
    """The video stream's ENCODER tag from `ffprobe -show_entries stream_tags=ENCODER -of json`
    output, or None. Never raises on malformed input (a probe glitch must not flag a file)."""
    import json

    if not ffprobe_stream_json:
        return None
    # Single-exception except only: repo ruff targets 3.14 and would rewrite a tuple `except (A, B)`
    # into PEP 758 `except A, B` — a SyntaxError on the host's 3.12 floor (test_host_scripts_py312).
    try:
        data = json.loads(ffprobe_stream_json)
    except ValueError:
        return None
    streams = data.get("streams") or [] if isinstance(data, dict) else []
    for s in streams:
        enc = (s.get("tags") or {}).get("ENCODER")
        if enc:
            return enc
    return None


def parse_keyframe_csv(text):
    """Keyframe presentation timestamps from `ffprobe -show_entries frame=key_frame,pts_time
    -of csv=p=0` output (each line `<key_frame>,<pts_time>`). Keeps only rows flagged as a keyframe
    with a numeric time; malformed rows are skipped."""
    times = []
    for line in (text or "").splitlines():
        parts = line.split(",")
        if len(parts) < 2 or parts[0].strip() != "1":
            continue
        try:
            times.append(float(parts[1]))
        except ValueError:
            continue
    return times


def encoder_is_reencoder(encoder_tag, markers) -> bool:
    """True if a video-stream ENCODER tag names a consumer re-encoder (case-insensitive substring)."""
    tag = (encoder_tag or "").lower()
    if not tag:
        return False
    return any(m.lower() in tag for m in markers)


def max_keyframe_gap(keyframe_times, probe_window_s):
    """Largest gap (seconds) between consecutive keyframes. With fewer than two keyframes in the
    probed window the GOP is at least the window length, so return the window itself."""
    times = sorted(t for t in (keyframe_times or []) if t is not None)
    if len(times) < 2:
        return float(probe_window_s)
    return max(b - a for a, b in zip(times, times[1:]))


def gop_exceeds(keyframe_times, probe_window_s, gop_max_s) -> bool:
    return max_keyframe_gap(keyframe_times, probe_window_s) > gop_max_s


def reencode_evidence(
    quality, encoder, keyframe_times, probe_window_s, gop_max_s, markers
):
    """Why a remux-quality file is really a re-encode, or None if it looks genuine. Encoder tag
    first (metadata-only, cheapest); GOP is the backstop when a re-encode stripped the tag."""
    if not is_remux_quality(quality):
        return None
    if encoder_is_reencoder(encoder, markers):
        return "encoder=%s" % sanitize(encoder, 60)
    if gop_exceeds(keyframe_times, probe_window_s, gop_max_s):
        return "GOP=%.1fs" % max_keyframe_gap(keyframe_times, probe_window_s)
    return None


def select_fakes(probed, probe_window_s, gop_max_s, markers):
    """Filter probed candidates (each enriched by the shell with `encoder` + `keyframes`) to the
    re-encoded remuxes, tagging each with the `evidence` string for the report."""
    out = []
    for p in probed:
        ev = reencode_evidence(
            p.get("quality"),
            p.get("encoder"),
            p.get("keyframes"),
            probe_window_s,
            gop_max_s,
            markers,
        )
        if ev:
            fake = dict(p)
            fake["evidence"] = ev
            out.append(fake)
    return out


def format_fake_line(verb, fake) -> str:
    return "%s [Sonarr] %s — claims %s but %s" % (
        verb,
        sanitize(fake.get("relativePath")),
        sanitize(fake.get("quality"), 60),
        sanitize(fake.get("evidence"), 80),
    )


def plan_fake_remux_actions(fakes, dry_run: bool, max_per_scan: int):
    """Pure blast-valve + dry-run planning. Returns the plan the I/O shell executes plus the
    (ok, summary) the state file / Kuma monitor reports.

    - no fakes -> ok, nothing to do.
    - more than max_per_scan -> a whole-library match is a rule bug / systemic import setting, not N
      independent bad grabs: act on NONE, page (`ok=False`) so a human looks.
    - dry_run + fakes -> flag only (no mutations), page (`ok=False`) so the report-only phase actually
      surfaces the files, self-clearing once they're handled.
    - live + fakes -> delete each file (Sonarr then re-searches the series, the configarr NTRX block
      steering the re-grab clean); a successful sweep is `ok=True` (handled, like the queue auto-block).
    """
    if not fakes:
        return {
            "hold": False,
            "deletes": [],
            "searches": [],
            "lines": [],
            "ok": True,
            "summary": "library clean",
        }
    if len(fakes) > max_per_scan:
        return {
            "hold": True,
            "deletes": [],
            "searches": [],
            "lines": [],
            "ok": False,
            "summary": "%d fake remuxes found — holding (max %d/scan), investigate"
            % (len(fakes), max_per_scan),
        }
    if dry_run:
        return {
            "hold": False,
            "deletes": [],
            "searches": [],
            "lines": [format_fake_line("WOULD delete+re-search", f) for f in fakes],
            "ok": False,
            "summary": "%d fake remux(es) flagged (report-only) — investigate"
            % len(fakes),
        }
    return {
        "hold": False,
        "deletes": [f["fileId"] for f in fakes],
        "searches": sorted({f["seriesId"] for f in fakes}),
        "lines": [format_fake_line("Deleted+re-searched", f) for f in fakes],
        "ok": True,
        "summary": "deleted+re-searched %d fake remux(es)" % len(fakes),
    }
