"""Prune archived Karakeep bookmarks by age, through Karakeep's REST API.

Two rules, both keyed on how long a bookmark has sat archived:

- after `--screenshot-days`, detach its `screenshot` asset. The bookmark, its text and its
  tags stay; Karakeep deletes the file as it detaches it (`Asset.detachAsset`).
- after `--delete-days`, delete the bookmark outright, unless something a person put on it
  protects it: favourited, a note, a highlight, or a tag a person attached.

Karakeep records no archive time. The age is `modifiedAt`, which the archive toggle sets
(the bookmarks table's `$onUpdate`) and later tag or note edits set again. It can only be
LATER than the archive moment, so this job can prune late but never early. Detaching an
asset leaves `modifiedAt` alone (it deletes an `assets` row, not a `bookmarks` row), so the
first rule does not restart the second rule's clock.

"A tag a person attached" is a tag whose `attachedBy` is `human`, minus `--auto-tag`. The
time-tagger and Karakeep's own rules engine both attach through paths that record `human`, so
their tags are listed as auto by name. The run log names the tags that protected each bookmark.

The run reads every page before it changes anything, so a deletion cannot move the cursor
under it, and it aborts before any change if a bookmark comes back without a field the rules
read: a missing `tags` would otherwise read as untagged, which is deletable.

Without `--apply` the run only logs what it would do.

Stdlib only, so the CronJob runs on a read-only root with nothing to install.
"""

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# The fields the rules read. A bookmark missing any of them aborts the run.
REQUIRED_FIELDS = ("id", "archived", "favourited", "modifiedAt", "tags", "assets")
PAGE_SIZE = 100


@dataclass(frozen=True)
class Action:
    kind: str  # "detach-screenshot" or "delete"
    bookmark_id: str
    title: str
    age_days: int
    asset_id: str | None = None


class Api:
    """A thin Bearer-token client. `opener` is the transport, so a test can record calls."""

    def __init__(self, base_url: str, api_key: str, opener=urllib.request.urlopen):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.opener = opener

    def request(self, method: str, path: str, params: dict | None = None):
        url = f"{self.base_url}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, method=method, headers={"Authorization": f"Bearer {self.api_key}"}
        )
        with self.opener(req, timeout=30) as resp:
            body = resp.read()
        return json.loads(body) if body else None


def wait_for_api(
    api: Api, budget_seconds: int, interval_seconds: int, sleep=time.sleep
):
    """Retry the first call until it answers.

    kube-router admits a new pod's source IP a moment after the pod starts, so a Job's first
    connections can be dropped.
    """
    attempts = max(1, budget_seconds // interval_seconds)
    for attempt in range(1, attempts + 1):
        try:
            return api.request("GET", "users/me")
        except (urllib.error.URLError, OSError) as e:
            if attempt == attempts:
                raise
            print(f"karakeep not reachable yet (attempt {attempt}/{attempts}): {e}")
            sleep(interval_seconds)
    return None


def list_archived(api: Api) -> list[dict]:
    bookmarks, cursor = [], None
    while True:
        params = {"archived": "true", "limit": PAGE_SIZE, "includeContent": "false"}
        if cursor:
            params["cursor"] = cursor
        page = api.request("GET", "bookmarks", params)
        bookmarks.extend(page["bookmarks"])
        cursor = page.get("nextCursor")
        if not cursor:
            return bookmarks


def list_tags(api: Api) -> list[dict]:
    tags, cursor = [], None
    while True:
        page = api.request("GET", "tags", {"cursor": cursor} if cursor else None)
        tags.extend(page["tags"])
        cursor = page.get("nextCursor")
        if not cursor:
            return tags


def report_manual_tags(tags: list[dict], auto_tags: frozenset[str]) -> None:
    """Print every tag a person attached anywhere in the library, archived or not.

    The pruner's own keep lines name only the tags that saved an old archived bookmark, so this
    is the list to read when deciding what `--auto-tag` should hold. A tag already listed as
    auto is marked, since Karakeep counts its attachments as `human` too.
    """
    manual = sorted(
        (t for t in tags if (t["numBookmarksByAttachedType"].get("human") or 0) > 0),
        key=lambda t: (-t["numBookmarksByAttachedType"]["human"], t["name"]),
    )
    print(f"tags with manual attachments, whole library: {len(manual)}")
    for t in manual:
        mark = "  (listed as auto)" if t["name"] in auto_tags else ""
        print(f"  {t['numBookmarksByAttachedType']['human']:5d}  {t['name']}{mark}")


def check_shape(bookmarks: list[dict]) -> None:
    for b in bookmarks:
        missing = [f for f in REQUIRED_FIELDS if f not in b]
        if missing:
            raise SystemExit(
                f"bookmark {b.get('id', '?')} lacks {missing}; refusing to prune on a "
                "response this script does not understand"
            )
        if b["archived"] is not True:
            raise SystemExit(
                f"bookmark {b['id']} came back from archived=true unarchived"
            )


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def manual_tags(bookmark: dict, auto_tags: frozenset[str]) -> list[str]:
    return sorted(
        t["name"]
        for t in bookmark["tags"]
        if t.get("attachedBy") != "ai" and t["name"] not in auto_tags
    )


def plan(
    bookmarks: list[dict],
    now: datetime,
    screenshot_days: int,
    delete_days: int,
    auto_tags: frozenset[str],
    has_highlights,
) -> tuple[list[Action], list[str]]:
    """Return the actions to take and one log line per bookmark a protection kept.

    `has_highlights(bookmark_id)` is called only for a bookmark otherwise due for deletion,
    since it costs one request each.
    """
    actions, kept = [], []
    for b in bookmarks:
        age = now - parse_ts(b["modifiedAt"])
        age_days = age.days
        title = (b.get("title") or "")[:80]
        if age >= timedelta(days=delete_days):
            reasons = []
            if b["favourited"]:
                reasons.append("favourited")
            if (b.get("note") or "").strip():
                reasons.append("has a note")
            tags = manual_tags(b, auto_tags)
            if tags:
                reasons.append(f"manual tags {tags}")
            if not reasons and has_highlights(b["id"]):
                reasons.append("has highlights")
            if not reasons:
                actions.append(Action("delete", b["id"], title, age_days))
                continue
            kept.append(f"keep {b['id']} ({age_days}d) {title!r}: {', '.join(reasons)}")
        if age >= timedelta(days=screenshot_days):
            for asset in b["assets"]:
                if asset.get("assetType") == "screenshot":
                    actions.append(
                        Action(
                            "detach-screenshot", b["id"], title, age_days, asset["id"]
                        )
                    )
    return actions, kept


def apply_actions(
    api: Api, actions: list[Action], apply: bool, max_deletes: int
) -> None:
    deletes = 0
    for a in actions:
        if a.kind == "delete":
            if deletes >= max_deletes:
                print(
                    f"skip delete {a.bookmark_id}: --max-deletes {max_deletes} reached"
                )
                continue
            deletes += 1
            path = f"bookmarks/{a.bookmark_id}"
        else:
            path = f"bookmarks/{a.bookmark_id}/assets/{a.asset_id}"
        verb = "" if apply else "would "
        print(f"{verb}{a.kind} {a.bookmark_id} ({a.age_days}d) {a.title!r}")
        if apply:
            api.request("DELETE", path)


def summarise(bookmarks: list[dict], now: datetime, actions: list[Action]) -> None:
    ages = sorted((now - parse_ts(b["modifiedAt"])).days for b in bookmarks)
    print(f"archived bookmarks: {len(bookmarks)}")
    if ages:
        print(
            f"days since modifiedAt: min {ages[0]}, median {statistics.median(ages)}, "
            f"max {ages[-1]}"
        )
    for kind in ("detach-screenshot", "delete"):
        print(f"{kind}: {sum(a.kind == kind for a in actions)}")


def main(argv=None, opener=urllib.request.urlopen, now=None, sleep=time.sleep) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--screenshot-days", type=int, required=True)
    p.add_argument("--delete-days", type=int, required=True)
    p.add_argument("--auto-tag", action="append", default=[])
    p.add_argument("--max-deletes", type=int, required=True)
    p.add_argument("--connect-budget-seconds", type=int, default=120)
    p.add_argument("--connect-interval-seconds", type=int, default=5)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args(argv)
    if args.delete_days < args.screenshot_days:
        p.error("--delete-days must not be shorter than --screenshot-days")

    api = Api(os.environ["KARAKEEP_API_URL"], os.environ["KARAKEEP_API_KEY"], opener)
    now = now or datetime.now(UTC)
    wait_for_api(api, args.connect_budget_seconds, args.connect_interval_seconds, sleep)

    bookmarks = list_archived(api)
    check_shape(bookmarks)

    def has_highlights(bookmark_id: str) -> bool:
        return bool(
            api.request("GET", f"bookmarks/{bookmark_id}/highlights")["highlights"]
        )

    actions, kept = plan(
        bookmarks,
        now,
        args.screenshot_days,
        args.delete_days,
        frozenset(args.auto_tag),
        has_highlights,
    )
    print("mode: " + ("apply" if args.apply else "dry run (no changes)"))
    summarise(bookmarks, now, actions)
    report_manual_tags(list_tags(api), frozenset(args.auto_tag))
    for line in kept:
        print(line)
    apply_actions(api, actions, args.apply, args.max_deletes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
