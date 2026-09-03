#!/usr/bin/env python3
"""Render the Uptime Kuma status page's group list from the AutoKuma declarations.

Runs as the middle stage of the status-page-sync CronJob: the kuma-cli stages either side
of it do all the talking to Kuma, and this stage decides whether there is anything to say.

Three inputs, one conditional output. `--monitors` and `--page` are what `kuma monitor list`
and `kuma status-page get <slug>` wrote; `--index` and `--rules` come from the ConfigMap
Ansible renders beside this file. `--out` is written ONLY when the computed group list
differs from the live one, and the apply stage runs `kuma status-page edit` only when that
file exists.

That conditional is the whole point of the stage. `saveStatusPage` writes Kuma's SQLite,
which lives on a Longhorn volume whose changed blocks ship to B2 nightly, so an
unconditional write every 15 minutes is the notification-rewrite loop this role already paid
for once (see the role's CLAUDE.md) at a slower rate.

The join goes id -> display name -> live numeric id. The AutoKuma id is the stable name and
the one the rules are written against, but it is invisible over Kuma's API: `monitor list`
returns display names and numeric ids, and the id-to-name map lives in AutoKuma's own SQLite
on an RWO PVC. `index.json` is Ansible's copy of that map, rendered from the same template
that declares the monitors.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def load_monitors(raw: object) -> dict[str, int]:
    """Map display name -> numeric id from `kuma monitor list` output.

    kuma-cli prints `MonitorList`, a `HashMap<String, Monitor>` keyed by the numeric id as a
    string. A list is accepted too so a future output shape does not silently produce an
    empty map: entries are read the same way either way, and the dict key is only a fallback
    for a monitor whose body carries no `id`.
    """
    entries: list[tuple[str | None, dict]] = []
    if isinstance(raw, dict):
        entries = [
            (key, value) for key, value in raw.items() if isinstance(value, dict)
        ]
    elif isinstance(raw, list):
        entries = [(None, value) for value in raw if isinstance(value, dict)]
    else:
        raise SystemExit(
            f"unreadable monitor list: expected object or array, got {type(raw).__name__}"
        )

    by_name: dict[str, int] = {}
    for key, monitor in entries:
        name = monitor.get("name")
        raw_id = monitor.get("id", key)
        if name is None or raw_id is None:
            continue
        by_name[name] = int(raw_id)

    if not by_name:
        raise SystemExit(
            "unreadable monitor list: no monitor carried both a name and an id"
        )
    return by_name


def bucket(index: dict[str, str], rules: list[dict]) -> list[tuple[str, list[str]]]:
    """Assign every declared AutoKuma id to the FIRST rule whose patterns match it.

    Declaration order inside a group is the order of `index.json`, which is the order the
    monitors appear in static-monitors.yaml.j2 — so the page reads like the template.
    """
    groups: list[tuple[str, list[str]]] = [(rule["name"], []) for rule in rules]
    compiled = [[re.compile(pattern) for pattern in rule["match"]] for rule in rules]

    for autokuma_id in index:
        for position, patterns in enumerate(compiled):
            if any(pattern.search(autokuma_id) for pattern in patterns):
                groups[position][1].append(autokuma_id)
                break
        else:
            raise SystemExit(
                f"{autokuma_id} matches no rule, and no catch-all rule is declared"
            )

    return groups


def build_group_list(
    groups: list[tuple[str, list[str]]],
    index: dict[str, str],
    live_ids: dict[str, int],
    live_page: dict,
) -> list[dict]:
    """Turn the buckets into a `publicGroupList`, keeping each existing group's id.

    A group id that survives keeps Kuma's own group row, so re-running the sync does not
    churn rows for a page whose membership only shifted.
    """
    existing_ids = {
        group.get("name"): group.get("id")
        for group in live_page.get("publicGroupList") or []
        if group.get("name") is not None
    }

    missing = sorted(
        name
        for name in (index[autokuma_id] for _, ids in groups for autokuma_id in ids)
        if name not in live_ids
    )
    if missing:
        raise SystemExit(
            "declared monitors have no live counterpart (AutoKuma may not have synced yet, "
            f"or a display name changed): {', '.join(missing)}"
        )

    public_group_list = []
    for position, (name, ids) in enumerate(groups, start=1):
        if not ids:
            continue
        group = {
            "name": name,
            "weight": position,
            "monitorList": [
                {"id": live_ids[index[autokuma_id]], "name": index[autokuma_id]}
                for autokuma_id in ids
            ],
        }
        if existing_ids.get(name) is not None:
            group["id"] = existing_ids[name]
        public_group_list.append(group)

    return public_group_list


def membership(
    public_group_list: list[dict] | None,
) -> list[tuple[str, tuple[int, ...]]]:
    """The part of a group list a change should be judged on.

    Group ids and the monitor `name`/`type`/`sendUrl` fields Kuma echoes back are excluded:
    they are either server-assigned or decorative, and including them would make every run
    look like a change.
    """
    return [
        (
            group.get("name") or "",
            tuple(
                monitor["id"]
                for monitor in group.get("monitorList") or []
                if monitor.get("id") is not None
            ),
        )
        for group in public_group_list or []
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--monitors", type=Path, required=True)
    parser.add_argument("--page", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--rules", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    index = json.loads(args.index.read_text())
    rules = json.loads(args.rules.read_text())
    live_page = json.loads(args.page.read_text())
    live_ids = load_monitors(json.loads(args.monitors.read_text()))

    public_group_list = build_group_list(
        bucket(index, rules), index, live_ids, live_page
    )

    if membership(public_group_list) == membership(live_page.get("publicGroupList")):
        print(
            f"status page is already grouped as declared ({len(public_group_list)} groups)"
        )
        return 0

    # Start from the LIVE page and replace one key. `saveStatusPage` writes the whole object,
    # so a hand-built document would blank description, theme, published and domainNameList.
    desired = dict(live_page)
    desired["publicGroupList"] = public_group_list
    args.out.write_text(json.dumps(desired, indent=2))

    counts = ", ".join(
        f"{group['name']}: {len(group['monitorList'])}" for group in public_group_list
    )
    print(f"status page group list changed -> {args.out} ({counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
