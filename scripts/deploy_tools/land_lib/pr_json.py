"""The fields the lander reads from a PR's JSON, typed once where `gh_json` returns them.

WHY. `gh_json` returns a `JsonValue`, and the lander read it as `dict[str, Any]`, so ty checked
nothing about a `.get` chain on it (#3704). Each reader here narrows one shape at the boundary:

- `parse_view`: `gh pr view --json <fields>`, which `Landing.view` returns.
- `parse_file` / `parse_review`: an entry of the REST `pulls/<n>/files` and `pulls/<n>/reviews`
  listings, which the landing policy reads.

Every key is optional, because a `gh pr view` answer carries only the fields asked for. A field
of the wrong type raises `ValueError`, which `Landing.view` reports as unparseable gh output. A
string field that is JSON `null` reads as `""`: every caller already treats the two alike.
A top-level view field `PrView` does not declare raises `KeyError`, because ty reports
`view["x"]` for an undeclared key but not `view.get("x")`, which would read `None` forever. A
new read therefore starts by adding its field here. Nested fields the lander does not read are
dropped.
"""

import sys as _sys
from pathlib import Path as _Path
from typing import TypedDict

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.json_types import JsonObject, JsonValue, as_object, as_object_list


class Login(TypedDict, total=False):
    login: str


class Commit(TypedDict, total=False):
    oid: str


class ChangedFile(TypedDict, total=False):
    path: str


class PrView(TypedDict, total=False):
    """The `gh pr view --json` fields the lander asks for."""

    author: Login | None
    autoMergeRequest: JsonObject | None
    baseRefName: str
    body: str
    changedFiles: int
    files: list[ChangedFile]
    headRefName: str
    headRefOid: str
    isCrossRepository: bool
    mergeCommit: Commit | None
    mergeStateStatus: str
    mergeable: str
    reviewDecision: str
    state: str
    title: str


class PrFile(TypedDict, total=False):
    """An entry of the REST `pulls/<n>/files` listing."""

    filename: str
    previous_filename: str


class PrReview(TypedDict, total=False):
    """An entry of the REST `pulls/<n>/reviews` listing."""

    user: Login | None
    state: str
    submitted_at: str
    commit_id: str


def _str(value: JsonValue, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{what}: expected a string, got {type(value).__name__}")
    return value


def _login(value: JsonValue, what: str) -> Login | None:
    if value is None:
        return None
    obj = as_object(value, what)
    return {"login": _str(obj.get("login"), f"{what}.login")}


def parse_view(obj: JsonObject) -> PrView:
    """The fields of a `gh pr view --json` answer the lander reads, each type-checked.

    Raises:
        KeyError: `obj` carries a field `PrView` does not declare, so a caller asked gh for a
            field no parser here reads.
        ValueError: a field has the wrong type.
    """
    unknown = sorted(set(obj) - PrView.__optional_keys__)
    if unknown:
        raise KeyError(
            f"PrView declares no field {', '.join(unknown)}; add it to pr_json.py"
        )
    view: PrView = {}
    if "author" in obj:
        view["author"] = _login(obj["author"], "author")
    if "autoMergeRequest" in obj:
        auto = obj["autoMergeRequest"]
        view["autoMergeRequest"] = (
            None if auto is None else as_object(auto, "autoMergeRequest")
        )
    if "changedFiles" in obj:
        count = obj["changedFiles"]
        if not isinstance(count, int) or isinstance(count, bool):
            raise ValueError(f"changedFiles: expected an integer, got {count!r}")
        view["changedFiles"] = count
    if "files" in obj:
        view["files"] = [
            {"path": _str(f.get("path"), f"files[{i}].path")}
            for i, f in enumerate(as_object_list(obj["files"], "files"))
        ]
    if "isCrossRepository" in obj:
        cross = obj["isCrossRepository"]
        if not isinstance(cross, bool):
            raise ValueError(f"isCrossRepository: expected a boolean, got {cross!r}")
        view["isCrossRepository"] = cross
    if "mergeCommit" in obj:
        commit = obj["mergeCommit"]
        view["mergeCommit"] = (
            None
            if commit is None
            else {
                "oid": _str(
                    as_object(commit, "mergeCommit").get("oid"), "mergeCommit.oid"
                )
            }
        )
    if "baseRefName" in obj:
        view["baseRefName"] = _str(obj["baseRefName"], "baseRefName")
    if "body" in obj:
        view["body"] = _str(obj["body"], "body")
    if "headRefName" in obj:
        view["headRefName"] = _str(obj["headRefName"], "headRefName")
    if "headRefOid" in obj:
        view["headRefOid"] = _str(obj["headRefOid"], "headRefOid")
    if "mergeStateStatus" in obj:
        view["mergeStateStatus"] = _str(obj["mergeStateStatus"], "mergeStateStatus")
    if "mergeable" in obj:
        view["mergeable"] = _str(obj["mergeable"], "mergeable")
    if "reviewDecision" in obj:
        view["reviewDecision"] = _str(obj["reviewDecision"], "reviewDecision")
    if "state" in obj:
        view["state"] = _str(obj["state"], "state")
    if "title" in obj:
        view["title"] = _str(obj["title"], "title")
    return view


def parse_file(obj: JsonObject) -> PrFile:
    """A REST changed-file entry: its path, and its old path when it was renamed."""
    entry: PrFile = {"filename": _str(obj.get("filename"), "filename")}
    if "previous_filename" in obj:
        entry["previous_filename"] = _str(obj["previous_filename"], "previous_filename")
    return entry


def parse_review(obj: JsonObject) -> PrReview:
    """A REST review entry: who submitted it, its verdict, when, and on which commit."""
    return {
        "user": _login(obj.get("user"), "user"),
        "state": _str(obj.get("state"), "state"),
        "submitted_at": _str(obj.get("submitted_at"), "submitted_at"),
        "commit_id": _str(obj.get("commit_id"), "commit_id"),
    }
