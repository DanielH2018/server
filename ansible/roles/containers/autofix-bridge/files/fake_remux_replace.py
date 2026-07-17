#!/usr/bin/env python3
"""fake-remux reconciler — the I/O shell for fake_remux_replace_logic.py's pure state machine.

Where fake_remux_scan.py flags+deletes a mislabeled remux, this cron closes the loop: it searches
for a clean replacement, grabs it, waits for the download, ffprobes it the same way the scan does,
and only then deletes the fake + lets Sonarr import the verified genuine file. Runs as the same host
cron user beside fake_remux_scan.py, reading/writing a ledger (dict[episodeId -> record]) that
survives across ticks so a crash mid-flight just resumes on the next run.

All decisions (which candidate to grab, when a probe proves a file genuine, what state an episode
moves to) live in the pure fake_remux_replace_logic.py core (see test_fake_remux_replace_logic.py).
This file only talks to Sonarr/jellyfin/Discord and feeds their results in. `FAKE_REMUX_REPLACE_MODE`
gates blast radius: `off` skips entirely, `shadow` previews searches/grabs into outcomes.jsonl with
zero Sonarr mutations, `live` executes grabs/deletes/imports/blocklists.

Runs under the host's /usr/bin/python3 (3.12 floor — keep 3.12-clean, see
ansible/tests/test_host_scripts_py312.py). Config comes from /etc/autofix-fake-remux/replace.config.env
(0600, embeds SONARR_API_KEY + the Discord webhook) plus a FAKE_REMUX_POLICY JSON file the pure core
reads (release-group allow/deny, size band, attempt caps — see fake_remux_replace_logic.py).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_remux_logic as frl  # noqa: E402  (sibling module — probe parsing + sanitize)
import fake_remux_replace_logic as frl_replace  # noqa: E402  (sibling module — the decision core)
import fake_remux_scan as scan  # noqa: E402  (sibling shell — reused boilerplate, see class Sonarr below)
from host_lib import atomic_write, discord_post, parse_env_file  # noqa: E402

CONFIG_PATH = os.environ.get(
    "FAKE_REMUX_REPLACE_CONFIG", "/etc/autofix-fake-remux/config.env"
)
log = scan.log  # reuse verbatim — no reason for a second timestamp-prefixed printer


def load_config():
    cfg = dict(os.environ)
    if os.path.exists(CONFIG_PATH):
        cfg.update(parse_env_file(CONFIG_PATH))
    return cfg


class Sonarr(scan.Sonarr):
    """Extends the scan shell's Sonarr client with the endpoints the reconciler needs. `_request` and
    `delete_episodefile` are inherited unchanged from scan.Sonarr."""

    def release_search(self, episode_id):
        return self._request("/api/v3/release?episodeId=%s" % episode_id) or []

    def grab(self, guid, indexer_id):
        return self._request(
            "/api/v3/release",
            method="POST",
            data={"guid": guid, "indexerId": indexer_id},
        )

    def queue(self):
        return (
            self._request("/api/v3/queue?pageSize=200&includeEpisode=true") or {}
        ).get("records", [])

    def episodefile_by_episode(self, series_id):
        eps = self._request("/api/v3/episode?seriesId=%s" % series_id) or []
        return {str(e["id"]): (e.get("episodeFileId") or None) for e in eps}

    def process_downloads(self):
        self._request(
            "/api/v3/command", method="POST", data={"name": "ProcessMonitoredDownloads"}
        )

    def blocklist_queue_item(self, queue_id):
        self._request(
            "/api/v3/queue/%s?removeFromClient=true&blocklist=true" % queue_id,
            method="DELETE",
        )


def _make_sonarr(cfg):
    ip = scan.resolve_ip(cfg.get("SONARR_CONTAINER", "sonarr"))
    port = cfg.get("SONARR_PORT", "8989")
    timeout = int(cfg.get("HTTP_TIMEOUT", "15"))
    return Sonarr("http://%s:%s" % (ip, port), cfg.get("SONARR_API_KEY", ""), timeout)


def _load_policy(cfg):
    path = cfg.get("FAKE_REMUX_POLICY", "/etc/autofix-fake-remux/replace-policy.json")
    if not path or not os.path.exists(path):
        return {}
    with open(path) as fh:
        return json.load(fh)


def _load_json(path, default):
    if not path or not os.path.exists(path):
        return default
    with open(path) as fh:
        return json.load(fh)


def _save_json(path, obj):
    atomic_write(path, json.dumps(obj))


def _qname(cand):
    return ((cand.get("quality") or {}).get("quality") or {}).get("name")


def _queue_by_episode(records):
    """episodeId (str) -> queue record, first wins. A season-pack item lists several episodes under
    `episodes`, so it gets mapped under each of their ids as well as its own `episodeId`."""
    by_ep = {}
    for rec in records or []:
        eids = set()
        if rec.get("episodeId") is not None:
            eids.add(rec["episodeId"])
        for ep in rec.get("episodes") or []:
            if ep.get("id") is not None:
                eids.add(ep["id"])
        for eid in eids:
            by_ep.setdefault(str(eid), rec)
    return by_ep


def _mirrors(cands, chosen):
    """`chosen` first, then any other candidate offering the identical release (a mirror upload on a
    second indexer) — so a rejected/dead mirror doesn't waste the tick when another copy is grabbable."""
    others = [
        c
        for c in (cands or [])
        if c is not chosen and c.get("title") == chosen.get("title")
    ]
    return [chosen] + others


def _try_grab(sonarr, cand):
    """POST the grab; None on a 4xx (e.g. "Getting release from indexer failed" — dead mirror, try the
    next one), re-raise anything else so a real outage surfaces as this run's failure."""
    try:
        return sonarr.grab(cand["guid"], cand["indexerId"])
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            log("grab failed for %s: %s" % (cand.get("title"), e))
            return None
        raise


def _probe_completed(cfg, ledger, q_by_episode, policy):
    """ffprobe every fully-downloaded queue item belonging to a grabbed/verifying ledger entry.
    Returns {episodeId: probe} for `advance()`; an entry with no probe stays in verifying and is
    re-checked next tick (download_stall_hours eventually holds it)."""
    jellyfin = cfg.get("JELLYFIN_CONTAINER", "jellyfin")
    ffprobe_bin = cfg.get("FFPROBE_BIN", "/usr/lib/jellyfin-ffmpeg/ffprobe")
    window_s = int(cfg.get("PROBE_WINDOW_S", "40"))
    timeout = int(cfg.get("PROBE_TIMEOUT_S", "60"))
    probes = {}
    for rec in ledger.values():
        if rec["state"] not in ("grabbed", "verifying"):
            continue
        epk = str(rec["episodeId"])
        chosen = rec.get("chosen") or {}
        item = q_by_episode.get(epk)
        if item is None or item.get("sizeleft", 1) != 0:
            continue
        path = item.get("outputPath")
        if not path:
            # Sonarr's queue record has no outputPath for this download client, so jellyfin has
            # nothing to probe here. Skip -> the entry holds after the stall timeout; jellyfin here
            # mounts /data, so this is rare.
            continue
        stream_json = scan.ffprobe(
            jellyfin,
            ffprobe_bin,
            path,
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
            continue  # probe glitch or jellyfin down — skip this tick, don't flag
        keyframe_csv = scan.ffprobe(
            jellyfin,
            ffprobe_bin,
            path,
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
        probes[epk] = {
            "quality": chosen.get("quality"),
            "encoder": frl.parse_encoder_tag(stream_json),
            "keyframes": frl.parse_keyframe_csv(keyframe_csv),
            "window_s": window_s,
            "gop_max_s": policy.get("gop_max_s", 5),
        }
    return probes


def _files_for_ledger(sonarr, ledger):
    """Current episodeId -> fileId for every series with an "importing" ledger entry — the only state
    advance() consults files_by_ep for (has the fake's fileId been replaced by Sonarr's import yet)."""
    series_ids = {
        rec["seriesId"] for rec in ledger.values() if rec["state"] == "importing"
    }
    files = {}
    for series_id in series_ids:
        files.update(sonarr.episodefile_by_episode(series_id))
    return files


def _execute(sonarr, actions, cfg, ledger):
    for act in actions:
        rec = ledger.get(str(act["episodeId"])) or {}
        if act["type"] == "delete_file":
            sonarr.delete_episodefile(
                act["fileId"]
            )  # delete first, like fake_remux_scan.py
            _outcome(cfg, "delete-fake", rec, "fileId=%s" % act["fileId"])
        elif act["type"] == "import":
            sonarr.process_downloads()
            _outcome(cfg, "import", rec, "ProcessMonitoredDownloads")
        elif act["type"] == "blocklist":
            queue_id = act.get("queueId")
            if queue_id is not None:
                sonarr.blocklist_queue_item(queue_id)
            _outcome(cfg, "blocklist", rec, "queueId=%s" % queue_id)


def _outcomes_path(cfg):
    base_dir = os.path.dirname(cfg.get("LEDGER_FILE", "")) or "."
    return cfg.get("OUTCOMES_FILE") or os.path.join(base_dir, "outcomes.jsonl")


def _outcome(cfg, kind, rec, detail):
    detail = frl.sanitize(detail)
    log(
        "%s %s %s — %s"
        % (kind, rec.get("series", "?"), rec.get("epLabel", "?"), detail)
    )
    entry = {
        "ts": int(time.time()),
        "kind": kind,
        "episodeId": rec.get("episodeId"),
        "series": rec.get("series"),
        "epLabel": rec.get("epLabel"),
        "detail": detail,
    }
    path = _outcomes_path(cfg)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def _alert_transitions(cfg, before_states, ledger):
    """advance() deliberately emits no 'alert' action (it's pure, Discord isn't). Alerting instead
    diffs ledger state: any episode that newly entered held or replaced this tick gets one line."""
    webhook = cfg.get("ARR_DISCORD_WEBHOOK_URL", "")
    for ep, rec in ledger.items():
        state = rec.get("state")
        if state not in ("held", "replaced"):
            continue
        if before_states.get(ep) == state:
            continue  # already alerted on a prior tick
        line = "%s [Sonarr] %s %s -> %s (%s)" % (
            "HELD" if state == "held" else "Replaced",
            frl.sanitize(rec.get("series")),
            frl.sanitize(rec.get("epLabel")),
            state,
            frl.sanitize(rec.get("reason") or ""),
        )
        log(line)
        discord_post(webhook, line, scan.USER_AGENT, log=log)


def _summarize(ledger):
    replaced = sum(1 for r in ledger.values() if r["state"] == "replaced")
    held = sum(1 for r in ledger.values() if r["state"] == "held")
    in_flight = len(ledger) - replaced - held
    msg = "%d replaced / %d in-flight / %d held" % (replaced, in_flight, held)
    return held == 0, msg


def reconcile_once(cfg, sonarr=None):
    """One full tick. Returns (ok, summary) for the state file. Raises only on a failure that
    prevents the tick running at all (main() records that as ok=false). `sonarr` is an injection
    seam for tests; production leaves it unset and gets the real client."""
    mode = cfg.get("FAKE_REMUX_REPLACE_MODE", "shadow").strip().lower()
    if mode == "off":
        return True, "replacer off"

    policy = _load_policy(cfg)
    ledger = _load_json(cfg["LEDGER_FILE"], default={})
    before_states = {ep: rec.get("state") for ep, rec in ledger.items()}
    sonarr = sonarr or _make_sonarr(cfg)

    # 1) search + grab detected entries (respect per-tick cap + spacing)
    for act in frl_replace.plan_searches(ledger, policy):
        cands = sonarr.release_search(act["episodeId"])
        rel, reason = frl_replace.select_replacement(cands, policy)
        rec = ledger[str(act["episodeId"])]
        if rel is None:
            ledger[str(act["episodeId"])] = {**rec, "state": "held", "reason": reason}
            _outcome(cfg, "no-candidate", rec, reason)
            continue
        _outcome(cfg, "would-grab" if mode != "live" else "grab", rec, rel.get("title"))
        if mode == "live":
            for cand in _mirrors(cands, rel):  # retry the same release across indexers
                resp = _try_grab(sonarr, cand)
                if resp is not None:
                    ledger[str(act["episodeId"])] = {
                        **rec,
                        "state": "grabbed",
                        "chosen": {
                            "guid": cand["guid"],
                            "indexerId": cand["indexerId"],
                            "title": cand["title"],
                            "quality": _qname(cand),
                        },
                        "lastAction": int(time.time()),
                    }
                    break
            time.sleep(int(policy.get("search_spacing_s", 20)))

    # 2) advance grabbed/verifying/importing (live only — the mutating half)
    if mode == "live":
        q = sonarr.queue()
        qbe = _queue_by_episode(q)
        probes = _probe_completed(cfg, ledger, qbe, policy)
        files = _files_for_ledger(sonarr, ledger)
        ledger, actions = frl_replace.advance(
            ledger, qbe, files, probes, policy, int(time.time())
        )
        _execute(sonarr, actions, cfg, ledger)

    _alert_transitions(cfg, before_states, ledger)
    _save_json(cfg["LEDGER_FILE"], ledger)
    return _summarize(ledger)


def main() -> int:
    cfg = load_config()
    state_file = cfg.get(
        "REPLACE_STATE_FILE", "/var/lib/autofix-fake-remux/replace_state.json"
    )
    cfg.setdefault("LEDGER_FILE", "/var/lib/autofix-fake-remux/replacements.json")
    log(
        "fake-remux reconcile starting (mode=%s)"
        % cfg.get("FAKE_REMUX_REPLACE_MODE", "shadow")
    )
    try:
        ok, msg = reconcile_once(cfg)
    except (
        Exception
    ) as e:  # a tick that can't run at all is this check's own failure -> page
        ok, msg = False, "fake-remux reconcile error: %s" % e
    log("OK  " if ok else "DOWN", msg)
    scan.write_state(state_file, ok, msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
