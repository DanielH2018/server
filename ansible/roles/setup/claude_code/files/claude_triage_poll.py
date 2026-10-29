#!/usr/bin/env python3
"""Find monitors that just went down and start one triage run per episode, as the agent user.

`claude-triage-poll.timer` runs this every two minutes. It reads two Prometheus vectors: Uptime
Kuma's `monitor_status == 0`, the Kuma path, and the firing `ALERTS`, the Alertmanager path.
It keeps per-key state in `<STATE_DIR>/poll-state.json` and opens an episode only on an
up→down transition that has held for `POLL_STREAK` polls. A key that stays down keeps one
episode until it recovers.

Three modes, from `POLL_MODE` in the root-owned config:

- `off`: read nothing and change nothing.
- `shadow`: log each episode it would queue, and append it to `<STATE_DIR>/shadow.jsonl`.
- `armed`: queue the episode in the inbox, and start the oldest queued episode whenever no
  run holds the runner's lock.

The first poll after a state file is created is a baseline. It records what is already down and
starts nothing, so arming never fires on every known red tile at once.

A series that vanished is unknown, not recovered, so a key keeps its previous state when its
source cannot be trusted. A failed read leaves every key as it was. A Kuma key recovers only
when its `monitor_status` reads 1: Kuma exports a monitor only after its first heartbeat since
it started, so a long-interval monitor has no series for hours after a Kuma restart. Kuma's
keys stay as they were while `up{job="uptime-kuma"}` is not 1. The alert keys stay as they were
for `ALERTS_SETTLE_S` after Prometheus starts, because a restarted Prometheus fires nothing
until each rule's `for:` delay, or the grace period of a restored one, has passed again.
"""

from __future__ import annotations

import argparse
import fcntl
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

# The poller runs from /opt/claude-triage, where tasks/triage.yml installs host_lib.py beside it.
from host_lib import WARNING, atomic_write, log, parse_env_file

CONFIG = "/etc/claude-triage/config.env"
KUMA_QUERY = 'monitor_status{job="uptime-kuma"}'
KUMA_UP_QUERY = 'up{job="uptime-kuma"}'
# Watchdog always fires by design; shadow and drill alerts page nobody.
ALERTS_QUERY = (
    'ALERTS{alertstate="firing", alertname!="Watchdog", shadow!="true", drill!="true"}'
)
PROM_AGE_QUERY = 'time() - process_start_time_seconds{job="prometheus"}'
# Rules with `for:` up to 10m refire within it; a longer one is restored with Prometheus's
# 10-minute --rules.alert.for-grace-period. Twenty minutes covers both with room.
ALERTS_SETTLE_S = 1200
HOLD_ALERT = "DeployHoldActive"
EPISODE_MAX = 63
# The prefix on every journal line, as `log` writes it.
TAG = "claude-triage-poll"


def prom_vector(
    base_url: str,
    query: str,
    timeout: float = 10,
    urlopen: Callable = urllib.request.urlopen,
) -> list[dict] | None:
    """The `result` list of an instant query, or None when the read failed.

    A failure is logged at warning priority with the URL and the cause. `read_all` falls back
    to the next URL, so without the line a dead loopback Prometheus left no trace while the
    ClusterIP answered. `urlopen` is the seam a test replaces.
    """
    url = f"{base_url.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode(
        {"query": query}
    )
    try:
        with urlopen(url, timeout=timeout) as resp:
            body = json.load(resp)
    except (OSError, ValueError) as exc:
        log(
            f"Prometheus read failed: {exc}",
            WARNING,
            tag=TAG,
            url=base_url,
            query=query,
        )
        return None
    if body.get("status") != "success":
        log(
            "Prometheus answered without success",
            WARNING,
            tag=TAG,
            url=base_url,
            query=query,
            status=body.get("status"),
        )
        return None
    return body.get("data", {}).get("result", [])


def read_all(
    urls: list[str], read: Callable[[str, str], list[dict] | None]
) -> dict[str, list[dict]] | None:
    """Every query from the first URL that answers them all, or None when none does.

    Prometheus publishes loopback only on the node it runs on, so the pinned ClusterIP after
    it reaches Prometheus from either node.
    """
    queries = {
        "kuma": KUMA_QUERY,
        "kuma_up": KUMA_UP_QUERY,
        "alerts": ALERTS_QUERY,
        "prom_age": PROM_AGE_QUERY,
    }
    for base in urls:
        found = {}
        for name, query in queries.items():
            result = read(base, query)
            if result is None:
                break
            found[name] = result
        else:
            return found
    return None


def value(series: dict) -> float:
    return float(series.get("value", [0, "nan"])[1])


def unknown_sources(found: dict[str, list[dict]]) -> set[str]:
    """The key prefixes whose emptiness this poll does not prove recovered."""
    unknown = set()
    if not any(value(s) == 1 for s in found["kuma_up"]):
        unknown.add("kuma")
    ages = [value(s) for s in found["prom_age"]]
    if not ages or min(ages) < ALERTS_SETTLE_S:
        unknown.add("alert")
    return unknown


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def kuma_up_keys(kuma: list[dict]) -> set[str]:
    """The Kuma keys whose monitor reads up now; only these count as recovered."""
    return {
        f"kuma:{s['metric']['monitor_name']}"
        for s in kuma
        if s.get("metric", {}).get("monitor_name") and value(s) == 1
    }


def down_keys(kuma: list[dict], alerts: list[dict]) -> dict[str, dict]:
    """Every source that is down now, keyed `kuma:<monitor>` or `alert:<name>:<service>`."""
    found: dict[str, dict] = {}
    for series in kuma:
        name = series.get("metric", {}).get("monitor_name", "")
        if name and value(series) == 0:
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
    """The weekly reboot, from the mute's lead before it to the grace after it."""
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
    unknown: set[str] | frozenset[str] = frozenset(),
    kuma_up: set[str] | frozenset[str] = frozenset(),
) -> tuple[dict, list[dict]]:
    """Advance the per-key state by one poll; return (new state, episodes to open).

    `down` is None when the read failed, and `unknown` names the sources (`kuma`, `alert`)
    whose keys this poll cannot judge: either way those keys keep their state. The baseline
    waits for a poll that can judge every source, so a source that comes back later cannot
    read its standing reds as new falls. A Kuma key recovers only when it is in `kuma_up`; an
    alert key recovers when it stops firing.
    """
    if down is None:
        return state, []
    baseline = not state.get("baseline")
    if baseline and unknown:
        return state, []
    keys = dict(state.get("keys", {}))
    ready: list[str] = []
    for key in down:
        if key.split(":", 1)[0] in unknown:
            continue
        entry = dict(keys.get(key, {}))
        if baseline:
            entry.update(streak=streak_needed, open=True, since=now)
        elif not entry.get("open"):
            entry["streak"] = entry.get("streak", 0) + 1
            if entry["streak"] >= streak_needed:
                ready.append(key)
        keys[key] = entry
    for key in list(keys):
        source = key.split(":", 1)[0]
        if key in down or source in unknown:
            continue
        if source == "alert" or key in kuma_up:
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
    # While held or muted, a ready key keeps its streak and opens once the gate lifts.
    return {**state, "baseline": True, "keys": keys}, episodes


def lock_free(path: str) -> bool:
    """True when no run holds the runner's `flock`; probing takes and drops it at once."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def queued(inbox: str) -> list[str]:
    """Episode ids waiting in the inbox, oldest first."""
    try:
        names = [n for n in os.listdir(inbox) if n.endswith(".json")]
    except OSError:
        return []
    names.sort(key=lambda n: (os.path.getmtime(os.path.join(inbox, n)), n))
    return [n[: -len(".json")] for n in names]


def start_run(episode: str) -> None:
    unit = f"claude-triage@{episode}.service"
    rc = subprocess.run(
        ["systemctl", "start", "--no-block", unit], check=False
    ).returncode
    if rc != 0:
        log(f"could not start {unit} (exit {rc})", WARNING, tag=TAG)


def drain(state: dict, state_dir: str, start: Callable[[str], None]) -> dict:
    """Start the oldest queued episode when no run holds the lock; return the new state.

    The runner deletes an episode's inbox file when it finishes, so a file this poller already
    started that is still queued once the lock is free belongs to a run that crashed. That run
    paged through OnFailure=; starting it again would crash and page on every poll.
    """
    inbox = os.path.join(state_dir, "inbox")
    waiting = queued(inbox)
    started = [e for e in state.get("started", []) if e in waiting]
    pending = [e for e in waiting if e not in started]
    if pending and lock_free(os.path.join(state_dir, "run.lock")):
        log(f"starting {pending[0]}", tag=TAG)
        start(pending[0])
        started.append(pending[0])
    return {**state, "started": started}


def main(
    argv: list[str],
    config_path: str = CONFIG,
    read: Callable[[str, str], list[dict] | None] = prom_vector,
    start: Callable[[str], None] = start_run,
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
    urls = cfg.get("PROM_URLS", "http://127.0.0.1:9090").split()
    found = read_all(urls, read)
    if found is None:
        log(
            f"no Prometheus answered every query; tried {' '.join(urls)}",
            WARNING,
            tag=TAG,
        )
        down, hold, unknown, up = None, False, set(), set()
    else:
        down = down_keys(found["kuma"], found["alerts"])
        up = kuma_up_keys(found["kuma"])
        hold = any(
            a.get("metric", {}).get("alertname") == HOLD_ALERT for a in found["alerts"]
        )
        unknown = unknown_sources(found)
    state, episodes = step(
        state,
        down,
        hold,
        in_reboot_window(cfg, t),
        t,
        int(cfg.get("POLL_STREAK", "2")),
        int(cfg.get("POLL_STORM_SIZE", "3")),
        unknown,
        up,
    )
    for episode in episodes:
        if mode == "armed":
            path = os.path.join(state_dir, "inbox", f"{episode['id']}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(episode, f, indent=2, sort_keys=True)
            log(f"queued {episode['id']}", tag=TAG)
        else:
            log(
                f"shadow: would queue {episode['id']}: {json.dumps(episode, sort_keys=True)}",
                tag=TAG,
            )
            with open(
                os.path.join(state_dir, "shadow.jsonl"), "a", encoding="utf-8"
            ) as f:
                f.write(json.dumps({"at": t, **episode}, sort_keys=True) + "\n")
    if mode == "armed":
        state = drain(state, state_dir, start)
    atomic_write(state_path, json.dumps(state, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
