#!/usr/bin/env python3
"""Find monitors that just went down and start one triage run per episode, as the agent user.

`claude-triage-poll.timer` runs this every two minutes. It reads two Prometheus vectors: Uptime
Kuma's `monitor_status == 0`, the Kuma path, and the firing `ALERTS`, the Alertmanager path.
It keeps per-key state in `<STATE_DIR>/poll-state.json` and opens an episode only on an
up→down transition that has held for `STREAK` polls. A key that stays down keeps one episode
until it recovers.

Three modes, from `MODE` in the root-owned config:

- `off`: read nothing and change nothing.
- `shadow`: log each run it would start, and append it to `<STATE_DIR>/shadow.jsonl`.
- `armed`: write the episode to the inbox and start `claude-triage@<episode>.service`.

The first poll after a state file is created is a baseline. It records what is already down and
starts nothing, so arming never fires on every known red tile at once. A failed read keeps the
previous state: series that vanish because Prometheus or Kuma died are unknown, not recovered.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone

CONFIG = "/etc/claude-triage/config.env"
KUMA_QUERY = 'monitor_status{job="uptime-kuma"} == 0'
# Watchdog always fires by design; shadow and drill alerts page nobody.
ALERTS_QUERY = (
    'ALERTS{alertstate="firing", alertname!="Watchdog", shadow!="true", drill!="true"}'
)
HOLD_ALERT = "DeployHoldActive"
EPISODE_MAX = 63


def log(msg: str) -> None:
    print(f"claude-triage-poll: {msg}", flush=True)


def parse_env_file(path: str) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and `#` comments skipped, one layer of quotes stripped."""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[key.strip()] = value
    return out


def prom_vector(base_url: str, query: str, timeout: float = 10) -> list[dict] | None:
    """The `result` list of an instant query, or None when the read failed."""
    url = f"{base_url.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode(
        {"query": query}
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.load(resp)
    except (OSError, ValueError) as exc:
        log(f"prometheus read failed: {exc}")
        return None
    if body.get("status") != "success":
        log(f"prometheus answered {body.get('status')!r}")
        return None
    return body.get("data", {}).get("result", [])


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def down_keys(kuma: list[dict], alerts: list[dict]) -> dict[str, dict]:
    """Every source that is down now, keyed `kuma:<monitor>` or `alert:<name>:<service>`."""
    found: dict[str, dict] = {}
    for series in kuma:
        name = series.get("metric", {}).get("monitor_name", "")
        if name:
            found[f"kuma:{name}"] = {"source": "kuma", "monitor": name}
    for series in alerts:
        m = series.get("metric", {})
        name = m.get("alertname", "")
        if not name or name == HOLD_ALERT:
            continue
        service = m.get("service", "")
        found[f"alert:{name}:{service}"] = {
            "source": "alertmanager",
            "monitor": name,
            "service": service,
            "severity": m.get("severity", ""),
        }
    return found


def episode_id(key: str, now: float) -> str:
    """`<yyyymmddThhmm>-<slug>`, held to the runner's EPISODE_ID shape."""
    stamp = time.strftime("%Y%m%dt%H%M", time.gmtime(now))
    body = slug(key.replace(":", " ")) or "episode"
    return f"{stamp}-{body}"[:EPISODE_MAX].rstrip("-")


def in_reboot_window(cfg: dict[str, str], now: float) -> bool:
    """The weekly reboot, from the shutdown delay before it to the boot grace after it."""
    try:
        weekday = int(cfg["REBOOT_WEEKDAY"])  # cron numbering: 0 is Sunday
        start_min = int(cfg["REBOOT_HOUR"]) * 60 + int(cfg["REBOOT_MINUTE"])
        before = int(cfg.get("REBOOT_BEFORE_MIN", "10"))
        after = int(cfg.get("REBOOT_GRACE_MIN", "30"))
    except KeyError, ValueError:
        return False
    t = datetime.fromtimestamp(now, timezone.utc)
    week_min = ((t.weekday() + 1) % 7) * 1440 + t.hour * 60 + t.minute
    target = weekday * 1440 + start_min
    delta = (week_min - target) % (7 * 1440)
    return delta <= after or delta >= 7 * 1440 - before


def step(
    state: dict,
    down: dict[str, dict] | None,
    hold: bool,
    muted: bool,
    now: float,
    streak_needed: int,
    storm_size: int,
) -> tuple[dict, list[dict]]:
    """Advance the per-key state by one poll; return (new state, episodes to start).

    `down` is None when a read failed: the state stays as it was. The first poll (no
    `baseline` in the state) records what is down and starts nothing.
    """
    if down is None:
        return state, []
    keys = dict(state.get("keys", {}))
    baseline = not state.get("baseline")
    ready: list[str] = []
    for key in down:
        entry = dict(keys.get(key, {}))
        if baseline:
            entry.update(streak=streak_needed, open=True, since=now)
        elif not entry.get("open"):
            entry["streak"] = entry.get("streak", 0) + 1
            if entry["streak"] >= streak_needed:
                ready.append(key)
        keys[key] = entry
    for key in list(keys):
        if key not in down:
            del keys[key]  # recovered: the next fall is a new episode
    episodes: list[dict] = []
    if ready and not (hold or muted):
        if len(ready) > storm_size:
            episodes.append(
                {
                    "id": episode_id("storm", now),
                    "storm": True,
                    "down": [down[k] for k in sorted(ready)],
                }
            )
        else:
            episodes += [{"id": episode_id(k, now), **down[k]} for k in sorted(ready)]
        for key in ready:
            keys[key].update(open=True, since=now)
    # While held or muted, a ready key keeps its streak and opens once the mute lifts.
    return {"baseline": True, "keys": keys}, episodes


def start_run(state_dir: str, episode: dict) -> None:
    inbox = os.path.join(state_dir, "inbox", f"{episode['id']}.json")
    with open(inbox, "w", encoding="utf-8") as f:
        json.dump(episode, f, indent=2, sort_keys=True)
    unit = f"claude-triage@{episode['id']}.service"
    rc = subprocess.run(
        ["systemctl", "start", "--no-block", unit], check=False
    ).returncode
    if rc != 0:
        log(f"could not start {unit} (exit {rc})")


def main(
    argv: list[str],
    config_path: str = CONFIG,
    read: Callable[[str, str], list[dict] | None] = prom_vector,
    start: Callable[[str, dict], None] = start_run,
    now: Callable[[], float] = time.time,
) -> int:
    """One poll; `read`, `start` and `now` are the seams a test replaces."""
    argparse.ArgumentParser(
        prog="claude_triage_poll.py",
        description="Poll Kuma and Alertmanager once and start triage for new episodes.",
    ).parse_args(argv[1:])
    cfg = parse_env_file(config_path)
    mode = cfg.get("POLL_MODE", "off")
    if mode == "off":
        return 0
    state_dir = cfg.get("STATE_DIR", "/var/lib/claude/triage")
    state_path = os.path.join(state_dir, "poll-state.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except OSError, ValueError:
        state = {}
    t = now()
    kuma = alerts = None
    # Prometheus publishes loopback only on the node it runs on, so the pinned ClusterIP
    # follows it to the other node.
    for base in cfg.get("PROM_URLS", "http://127.0.0.1:9090").split():
        kuma = read(base, KUMA_QUERY)
        alerts = read(base, ALERTS_QUERY) if kuma is not None else None
        if kuma is not None and alerts is not None:
            break
    down = None if kuma is None or alerts is None else down_keys(kuma, alerts)
    hold = any(a.get("metric", {}).get("alertname") == HOLD_ALERT for a in alerts or [])
    muted = in_reboot_window(cfg, t)
    state, episodes = step(
        state,
        down,
        hold,
        muted,
        t,
        int(cfg.get("POLL_STREAK", "2")),
        int(cfg.get("POLL_STORM_SIZE", "3")),
    )
    tmp = f"{state_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, state_path)
    for episode in episodes:
        if mode == "armed":
            log(f"starting {episode['id']}")
            start(state_dir, episode)
        else:
            log(
                f"shadow: would start {episode['id']}: {json.dumps(episode, sort_keys=True)}"
            )
            with open(
                os.path.join(state_dir, "shadow.jsonl"), "a", encoding="utf-8"
            ) as f:
                f.write(json.dumps({"at": t, **episode}, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
