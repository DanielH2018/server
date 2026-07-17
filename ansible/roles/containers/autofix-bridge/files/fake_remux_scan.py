#!/usr/bin/env python3
"""fake-remux host scan — the ffprobe-backed twin of autofix.py's queue remediation.

The autofix-bridge sidecar is deliberately zero-privilege (stdlib only, cap_drop ALL, no docker), so
it can't run ffprobe. This host-plane script — a daily cron beside the disk-autoprune one, running as
the sys_user (in the docker group) — does the part the sidecar can't: it reads Sonarr's library, runs
jellyfin's ffprobe against each REMUX-quality file, and deletes any that are actually re-encodes
(long GOP or a consumer re-encoder ENCODER tag — see fake_remux_logic.py for the signal). It reports
health the same way the other host crons do: a {ts,ok,msg} state file that monitor-bridge reads over a
:ro bind mount and turns into the "Fake Remux Scan" Kuma monitor, plus a per-fake Discord line.

Runs under the host's /usr/bin/python3 (3.12 floor — keep 3.12-clean, see
ansible/tests/test_host_scripts_py312.py). Config comes from /etc/autofix-fake-remux/config.env
(0600, embeds SONARR_API_KEY + the Discord webhook). ffprobe uses jellyfin because it mounts the media
read-only at /data/media, so Sonarr's absolute path resolves unchanged (no translation) and a probe
can't write. FAKE_REMUX_DRY_RUN defaults true — this DELETES library files, so it ships report-only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_remux_logic as frl  # noqa: E402  (sibling module, resolved via the sys.path insert)
from host_lib import atomic_write, discord_post, parse_env_file  # noqa: E402

CONFIG_PATH = os.environ.get("FAKE_REMUX_CONFIG", "/etc/autofix-fake-remux/config.env")
USER_AGENT = "autofix-fake-remux"


def _dry_run_enabled(val) -> bool:
    """Fail-safe dry-run: enabled UNLESS explicitly disabled (0/false/no), matching autofix.py."""
    return str(val).strip().lower() not in ("0", "false", "no")


def load_config():
    cfg = dict(os.environ)
    if os.path.exists(CONFIG_PATH):
        cfg.update(parse_env_file(CONFIG_PATH))
    return cfg


def log(*args) -> None:
    print("[%s]" % time.strftime("%Y-%m-%dT%H:%M:%S"), *args, flush=True)


def resolve_ip(container: str) -> str:
    """First bridge IP of a container via docker inspect (resolved at run time — it changes on
    recreate). Mirrors scripts/probe.py's resolve_ip; the host can reach any of a container's IPs."""
    out = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
            container,
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(
            "docker inspect %s failed: %s" % (container, out.stderr.strip())
        )
    for tok in out.stdout.split():
        if tok:
            return tok
    raise RuntimeError("%s has no container IP (is it running?)" % container)


class Sonarr:
    def __init__(self, base: str, api_key: str, timeout: int):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, path: str, method: str = "GET", data=None):
        url = self.base + path
        body = json.dumps(data).encode() if data is not None else None
        headers = {"X-Api-Key": self.api_key, "User-Agent": USER_AGENT}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, headers=headers, data=body, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (internal URL)
            raw = resp.read()
            return json.loads(raw) if raw else None

    def series(self):
        return self._request("/api/v3/series") or []

    def episodefiles(self, series_id):
        return self._request("/api/v3/episodefile?seriesId=%s" % series_id) or []

    def delete_episodefile(self, file_id):
        self._request("/api/v3/episodefile/%s" % file_id, method="DELETE")

    def series_search(self, series_id):
        self._request(
            "/api/v3/command",
            method="POST",
            data={"name": "SeriesSearch", "seriesId": series_id},
        )


def ffprobe(jellyfin: str, ffprobe_bin: str, path: str, args, timeout: int) -> str:
    """Run ffprobe inside the jellyfin container against a library path. Returns stdout, or "" on any
    failure (a probe glitch / jellyfin down must SKIP a file, never flag it)."""
    try:
        out = subprocess.run(
            ["docker", "exec", jellyfin, ffprobe_bin, "-v", "error", *args, path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log("ffprobe exec failed for %s: %s" % (path, e))
        return ""
    if out.returncode != 0:
        log("ffprobe error for %s: %s" % (path, out.stderr.strip()[:200]))
        return ""
    return out.stdout


def probe_candidate(cand, jellyfin, ffprobe_bin, window_s, timeout):
    """Enrich one remux candidate with its ENCODER tag + keyframe times. Returns None when the file
    couldn't be probed (skipped, not flagged)."""
    stream_json = ffprobe(
        jellyfin,
        ffprobe_bin,
        cand["path"],
        [
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream_tags=ENCODER",
            "-of",
            "json",
        ],
        timeout,
    )
    if not stream_json:
        return None
    encoder = frl.parse_encoder_tag(stream_json)
    keyframes = []
    # Encoder tag alone is decisive; only pay for the keyframe read when the tag didn't already flag it.
    if not frl.encoder_is_reencoder(encoder, frl.DEFAULT_RE_ENCODER_MARKERS):
        kf_csv = ffprobe(
            jellyfin,
            ffprobe_bin,
            cand["path"],
            [
                "-select_streams",
                "v:0",
                "-read_intervals",
                "%%+%d" % window_s,
                "-show_entries",
                "frame=key_frame,pts_time",
                "-of",
                "csv=p=0",
            ],
            timeout,
        )
        keyframes = frl.parse_keyframe_csv(kf_csv)
    probed = dict(cand)
    probed["encoder"] = encoder
    probed["keyframes"] = keyframes
    return probed


def write_state(state_file, ok, msg):
    atomic_write(
        state_file, json.dumps({"ts": int(time.time()), "ok": bool(ok), "msg": msg})
    )


def scan(cfg):
    """One full scan. Returns (ok, summary) for the state file. Raises only on a Sonarr failure that
    prevents the scan running at all (main() records that as ok=false)."""
    api_key = cfg.get("SONARR_API_KEY", "")
    if not api_key:
        return True, "disabled (no Sonarr API key)"

    dry_run = _dry_run_enabled(cfg.get("FAKE_REMUX_DRY_RUN", "true"))
    gop_max_s = float(cfg.get("GOP_MAX_S", "5"))
    window_s = int(cfg.get("PROBE_WINDOW_S", "40"))
    max_per_scan = int(cfg.get("MAX_PER_SCAN", "5"))
    timeout = int(cfg.get("PROBE_TIMEOUT_S", "60"))
    jellyfin = cfg.get("JELLYFIN_CONTAINER", "jellyfin")
    ffprobe_bin = cfg.get("FFPROBE_BIN", "/usr/lib/jellyfin-ffmpeg/ffprobe")
    webhook = cfg.get("ARR_DISCORD_WEBHOOK_URL", "")

    ip = resolve_ip(cfg.get("SONARR_CONTAINER", "sonarr"))
    port = cfg.get("SONARR_PORT", "8989")
    sonarr = Sonarr(
        "http://%s:%s" % (ip, port), api_key, int(cfg.get("HTTP_TIMEOUT", "15"))
    )

    candidates = []
    for s in sonarr.series():
        candidates.extend(
            frl.remux_candidates(
                sonarr.episodefiles(s.get("id")), s.get("title") or "?"
            )
        )

    probed, skipped = [], 0
    for cand in candidates:
        p = probe_candidate(cand, jellyfin, ffprobe_bin, window_s, timeout)
        if p is None:
            skipped += 1
        else:
            probed.append(p)

    fakes = frl.select_fakes(
        probed, window_s, gop_max_s, frl.DEFAULT_RE_ENCODER_MARKERS
    )
    plan = frl.plan_fake_remux_actions(fakes, dry_run, max_per_scan)

    if plan["hold"]:
        discord_post(webhook, plan["summary"], USER_AGENT, log=log)
    for file_id in plan["deletes"]:
        sonarr.delete_episodefile(
            file_id
        )  # delete first so a failure propagates before its report
    for line in plan["lines"]:
        log(line)
        discord_post(webhook, line, USER_AGENT, log=log)
    for series_id in plan["searches"]:
        sonarr.series_search(series_id)

    summary = plan["summary"]
    if skipped:
        summary += " (%d candidate(s) unprobed — jellyfin unavailable?)" % skipped
    return plan["ok"], summary


def main() -> int:
    cfg = load_config()
    state_file = cfg.get("STATE_FILE", "/var/lib/autofix-fake-remux/state.json")
    log(
        "fake-remux scan starting (dry_run=%s)"
        % _dry_run_enabled(cfg.get("FAKE_REMUX_DRY_RUN", "true"))
    )
    try:
        ok, msg = scan(cfg)
    except (
        Exception
    ) as e:  # a scan that can't run at all is this check's own failure -> page
        ok, msg = False, "fake-remux scan error: %s" % e
    log("OK  " if ok else "DOWN", msg)
    write_state(state_file, ok, msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
